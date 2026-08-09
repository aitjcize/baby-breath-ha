from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Any

import cv2

from app import __version__
from app.capture import DisabledFrameSource, RTSPFrameSource, SyntheticFrameSource, probe_stream
from app.classifier import Classification, ConservativeClassifier, DetectorState
from app.config import AppConfig, CameraConfig, normalize_roi
from app.estimator import RespirationEstimate, RespirationEstimator
from app.motion import DenseOpticalFlowExtractor, MotionObservation
from app.mqtt import MQTTPublisher
from app.roi_scan import BreathingRegionScanner
from app.settings import SettingsStore
from app.web import WebServer, WebState

LOGGER = logging.getLogger(__name__)

# Fields that only matter to the web UI; kept out of the MQTT state payload.
_WEB_ONLY_STATUS_FIELDS = ("waveform_t", "waveform_y", "scan", "roi")

PROBE_PREVIEW_WIDTH = 640


class BabyRespirationService:
    """Owns the processing loop and exposes a thread-safe API for the web UI."""

    def __init__(self, config: AppConfig, settings_store: SettingsStore | None = None) -> None:
        self.config = config
        self.settings = settings_store if settings_store is not None else SettingsStore()
        stored = self.settings.get()
        self._camera = self._effective_camera(config.camera, stored.rtsp_url, stored.roi)
        self.source = self._build_source(self._camera.rtsp_url)
        self.extractor = DenseOpticalFlowExtractor(self._camera, config.signal)
        self.estimator = RespirationEstimator(self._camera, config.signal)
        self.classifier = ConservativeClassifier(config.signal)
        self.scanner = BreathingRegionScanner(config.signal)
        self.mqtt = MQTTPublisher(config.mqtt)
        self.web_state = WebState(self._camera.roi)
        self.web_server: WebServer | None = None
        self.latest_status: dict[str, Any] = {}
        self._stop_event = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, Any] = {}
        self._probe_lock = threading.Lock()
        self._probe_preview: bytes | None = None
        self._preview_lock = threading.Lock()
        self._last_sequence = -1
        self._latest_overlay = None

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _clean_url(url: str) -> str:
        url = url.strip()
        # An unexpanded ${VAR} placeholder means "not configured", but a literal
        # $ inside credentials is a valid URL character.
        return "" if "${" in url else url

    def _effective_camera(
        self,
        base: CameraConfig,
        stored_url: str,
        stored_roi: tuple[float, float, float, float] | None,
    ) -> CameraConfig:
        url = self._clean_url(stored_url) or self._clean_url(base.rtsp_url)
        roi = stored_roi if stored_roi is not None else base.roi
        return replace(base, rtsp_url=url, roi=roi)

    def _build_source(self, url: str) -> RTSPFrameSource | DisabledFrameSource | SyntheticFrameSource:
        if not url:
            return DisabledFrameSource()
        if url.startswith("demo:"):
            return SyntheticFrameSource()
        return RTSPFrameSource(
            url,
            reconnect_interval=self._camera.reconnect_interval,
            open_timeout_ms=self._camera.open_timeout_ms,
            read_timeout_ms=self._camera.read_timeout_ms,
        )

    # ------------------------------------------- web-thread API (thread-safe)

    @property
    def camera_configured(self) -> bool:
        return bool(self._camera.rtsp_url)

    def probe(self, url: str) -> dict[str, Any]:
        """Test a candidate RTSP URL and cache a preview frame for the wizard."""
        if not self._probe_lock.acquire(blocking=False):
            return {"ok": False, "message": "Another connection test is already running."}
        try:
            if url.strip().startswith("demo:"):
                demo = SyntheticFrameSource()
                frame = demo.render(0.0)
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                with self._preview_lock:
                    self._probe_preview = encoded.tobytes() if ok else None
                return {"ok": True, "message": "Demo camera ready: synthetic breathing scene.", "width": demo.WIDTH, "height": demo.HEIGHT}
            result = probe_stream(
                url,
                open_timeout_ms=self._camera.open_timeout_ms,
                read_timeout_ms=self._camera.read_timeout_ms,
            )
            payload: dict[str, Any] = {"ok": result.ok, "message": result.message}
            if result.ok and result.frame is not None:
                frame = result.frame
                height, width = frame.shape[:2]
                if width > PROBE_PREVIEW_WIDTH:
                    scale = PROBE_PREVIEW_WIDTH / width
                    frame = cv2.resize(frame, (PROBE_PREVIEW_WIDTH, max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                with self._preview_lock:
                    self._probe_preview = encoded.tobytes() if ok else None
                payload.update({"width": width, "height": height})
            return payload
        finally:
            self._probe_lock.release()

    def probe_preview(self) -> bytes | None:
        with self._preview_lock:
            return self._probe_preview

    def apply_settings(self, rtsp_url: str | None = None, roi: Any = None) -> dict[str, Any]:
        """Persist wizard-entered settings and queue them for the main loop."""
        update: dict[str, Any] = {}
        if rtsp_url is not None:
            update["rtsp_url"] = rtsp_url.strip()
        if roi is not None:
            update["roi"] = normalize_roi(roi)
        if not update:
            raise ValueError("nothing to update")
        self.settings.update(**update)
        with self._pending_lock:
            self._pending.update(update)
        if "roi" in update:
            self.web_state.set_roi(update["roi"])
        return {
            "applied": True,
            "rtsp_url_configured": bool(update.get("rtsp_url", self._camera.rtsp_url)),
            "roi": list(update.get("roi", self._camera.roi)),
        }

    def start_scan(self) -> tuple[bool, str]:
        snapshot = self.source.snapshot()
        if snapshot.status != "connected":
            return False, "Camera stream is not connected yet."
        return self.scanner.start()

    def cancel_scan(self) -> None:
        self.scanner.cancel()

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------- main loop

    def run(self, run_seconds: float | None = None) -> None:
        started = time.monotonic()
        if self.config.debug.enabled:
            try:
                self.web_server = WebServer(self.config.debug.host, self.config.debug.port, self.web_state, self)
                self.web_server.start()
            except OSError as exc:
                LOGGER.error("web UI could not start: %s", exc)
        self.mqtt.start()
        self.source.start()
        interval = 1.0 / self._camera.processing_fps
        next_tick = time.monotonic()
        last_publish = 0.0

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if run_seconds is not None and now - started >= run_seconds:
                    break
                self._apply_pending()
                snapshot = self.source.snapshot()
                observation_added = False

                if snapshot.frame is not None and snapshot.timestamp is not None and snapshot.sequence != self._last_sequence:
                    self._last_sequence = snapshot.sequence
                    try:
                        observation, self._latest_overlay = self.extractor.process(snapshot.frame, snapshot.timestamp)
                        self.estimator.add(observation)
                        self.scanner.add(self.extractor.last_residual_dy, snapshot.timestamp, observation.excessive_motion)
                        observation_added = True
                    except Exception:
                        LOGGER.exception("motion extraction failed")
                        self.extractor.reset()
                        self.estimator.add(self._invalid_observation(now, "motion_extraction_error"))
                        observation_added = True

                stale_after = max(2.0, interval * 4)
                stream_stale = snapshot.timestamp is None or now - snapshot.timestamp > stale_after
                if not observation_added and (snapshot.status != "connected" or stream_stale):
                    self.estimator.add(self._invalid_observation(now, f"stream_{snapshot.status}"))

                if now - last_publish >= 1.0:
                    estimate = self.estimator.estimate(now)
                    classification = self.classifier.update(estimate, now)
                    status = self._make_status(estimate, classification, snapshot.status, snapshot.reconnect_count)
                    jpeg = self._encode_overlay(self._latest_overlay, status)
                    self.latest_status = status
                    self.web_state.update(status, jpeg)
                    self.mqtt.publish({key: value for key, value in status.items() if key not in _WEB_ONLY_STATUS_FIELDS})
                    LOGGER.info(
                        "state=%s valid=%s bpm=%s confidence=%.1f stream=%s reason=%s",
                        classification.state.value,
                        classification.measurement_valid,
                        status["bpm"],
                        estimate.confidence,
                        snapshot.status,
                        classification.reason,
                    )
                    last_publish = now

                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    self._stop_event.wait(delay)
                else:
                    next_tick = time.monotonic()
        finally:
            self.source.stop()
            self.mqtt.stop()
            if self.web_server:
                self.web_server.stop()
            LOGGER.info("service stopped cleanly")

    def _apply_pending(self) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return
        new_url = self._clean_url(pending.get("rtsp_url", self._camera.rtsp_url))
        new_roi = tuple(pending.get("roi", self._camera.roi))
        url_changed = new_url != self._camera.rtsp_url
        roi_changed = new_roi != tuple(self._camera.roi)
        if not url_changed and not roi_changed:
            return
        LOGGER.info("applying new settings: url_changed=%s roi_changed=%s", url_changed, roi_changed)
        self._camera = replace(self._camera, rtsp_url=new_url, roi=new_roi)
        if url_changed:
            self.scanner.cancel()
            self.source.stop()
            self.source = self._build_source(new_url)
            self.source.start()
            self._last_sequence = -1
            self._latest_overlay = None
        self.extractor = DenseOpticalFlowExtractor(self._camera, self.config.signal)
        self.estimator = RespirationEstimator(self._camera, self.config.signal)
        self.classifier.reset()

    # ---------------------------------------------------------------- output

    @staticmethod
    def _invalid_observation(timestamp: float, reason: str) -> MotionObservation:
        return MotionObservation(
            timestamp=timestamp,
            value=None,
            valid=False,
            excessive_motion=False,
            global_motion=0.0,
            local_motion=0.0,
            image_contrast=0.0,
            sharpness=0.0,
            brightness=0.0,
            frame_change=0.0,
            reason=reason,
        )

    def _make_status(
        self,
        estimate: RespirationEstimate,
        classification: Classification,
        stream_status: str,
        reconnect_count: int,
    ) -> dict[str, Any]:
        bpm = estimate.bpm if classification.state == DetectorState.BREATHING else None
        if not self.camera_configured:
            reason = "awaiting_camera_setup"
        elif stream_status != "connected":
            reason = f"stream_{stream_status}"
        else:
            reason = classification.reason
        return {
            "state": classification.state.value,
            "bpm": bpm,
            "confidence": estimate.confidence,
            "measurement_valid": classification.measurement_valid,
            "breathing_detected": classification.breathing_detected,
            "calibrated": classification.calibrated,
            "reason": reason,
            "stream_status": stream_status,
            "reconnect_count": reconnect_count,
            "excessive_motion": estimate.excessive_motion,
            "signal_rms": round(estimate.signal_rms, 6),
            "snr_db": estimate.snr_db,
            "peak_concentration": round(estimate.peak_concentration, 4),
            "estimated_fps": round(estimate.estimated_fps, 2),
            "window_seconds": round(estimate.window_seconds, 1),
            "data_completeness": round(estimate.data_completeness, 3),
            "camera_configured": self.camera_configured,
            "version": __version__,
            "roi": list(self._camera.roi),
            "scan": self.scanner.snapshot(),
            "waveform_t": estimate.waveform_t,
            "waveform_y": estimate.waveform_y,
        }

    @staticmethod
    def _encode_overlay(frame: Any, status: dict[str, Any]) -> bytes | None:
        if frame is None:
            return None
        annotated = frame.copy()
        lines = [
            f"{status['state']}  valid={status['measurement_valid']}",
            f"BPM={status['bpm']}  confidence={status['confidence']:.1f}%  SNR={status['snr_db']}",
        ]
        for index, line in enumerate(lines):
            cv2.putText(annotated, line, (8, 20 + index * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1, cv2.LINE_AA)
        ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return encoded.tobytes() if ok else None
