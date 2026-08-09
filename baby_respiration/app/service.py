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
from app.presence import PresenceState, PresenceTracker
from app.roi_scan import BreathingRegionScanner
from app.settings import SettingsStore
from app.web import WebServer, WebState

LOGGER = logging.getLogger(__name__)

# Fields that only matter to the web UI; kept out of the MQTT state payload.
_WEB_ONLY_STATUS_FIELDS = ("waveform_t", "waveform_y", "scan", "roi", "roi_suggestion", "mqtt", "active_block")

PROBE_PREVIEW_WIDTH = 640


class BabyRespirationService:
    """Owns the processing loop and exposes a thread-safe API for the web UI."""

    def __init__(self, config: AppConfig, settings_store: SettingsStore | None = None) -> None:
        self.config = config
        self.settings = settings_store if settings_store is not None else SettingsStore()
        stored = self.settings.get()
        self._camera = self._effective_camera(config.camera, stored.rtsp_url, stored.roi)
        self._monitoring = stored.monitoring_enabled
        self.source = self._build_source(self._camera.rtsp_url)
        self.extractor = DenseOpticalFlowExtractor(self._camera, config.signal)
        self.estimator = RespirationEstimator(self._camera, config.signal)
        self.classifier = ConservativeClassifier(config.signal)
        self.scanner = BreathingRegionScanner(config.signal)
        self.presence = PresenceTracker(enabled=config.signal.presence_enabled)
        self._handled_scan_id = 0
        self._roi_suggestion: dict[str, Any] | None = None
        self.mqtt = MQTTPublisher(self._effective_mqtt(), on_monitoring_command=self._on_monitoring_command)
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
        self._rate_low = False
        threshold = config.signal.low_rate_threshold_bpm
        if 0 < threshold <= config.signal.min_bpm:
            LOGGER.warning(
                "low_rate_threshold_bpm (%.0f) is at or below min_bpm (%.0f); rates that low are "
                "unmeasurable, so the low-rate flag will never trigger",
                threshold,
                config.signal.min_bpm,
            )

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

    def _effective_mqtt(self):
        """Broker resolution: UI choice first, then whatever the config offers.

        "auto" keeps the broker the environment provides — the Supervisor's
        Mosquitto service in add-on mode, or the static config in standalone
        mode. "custom" points at a user-entered broker anywhere on the
        network; "disabled" turns publishing off.
        """
        stored = self.settings.get()
        base = self.config.mqtt
        if stored.mqtt_mode == "custom":
            return replace(
                base,
                enabled=bool(stored.mqtt_host),
                host=stored.mqtt_host or base.host,
                port=stored.mqtt_port,
                username=stored.mqtt_username,
                password=stored.mqtt_password,
            )
        if stored.mqtt_mode == "disabled":
            return replace(base, enabled=False)
        return base

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

    def apply_settings(self, rtsp_url: str | None = None, roi: Any = None, mqtt: dict[str, Any] | None = None, monitoring: bool | None = None) -> dict[str, Any]:
        """Persist wizard-entered settings and queue them for the main loop."""
        update: dict[str, Any] = {}
        if rtsp_url is not None:
            update["rtsp_url"] = rtsp_url.strip()
        if roi is not None:
            update["roi"] = normalize_roi(roi)
            area = update["roi"][2] * update["roi"][3]
            if area > 0.25:
                LOGGER.warning(
                    "configured region covers %.0f%% of the frame; large regions dilute "
                    "the breathing signal and may capture a co-sleeping adult",
                    area * 100,
                )
        if monitoring is not None:
            update["monitoring_enabled"] = bool(monitoring)
        if mqtt is not None:
            if not isinstance(mqtt, dict):
                raise ValueError("mqtt must be an object")
            allowed = {"mqtt_mode": "mode", "mqtt_host": "host", "mqtt_port": "port", "mqtt_username": "username", "mqtt_password": "password"}
            for store_key, payload_key in allowed.items():
                if payload_key in mqtt:
                    update[store_key] = mqtt[payload_key]
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

    def _on_monitoring_command(self, enabled: bool) -> None:
        LOGGER.info("monitoring %s via MQTT command", "enabled" if enabled else "disabled")
        try:
            self.apply_settings(monitoring=enabled)
        except ValueError as exc:
            LOGGER.warning("could not apply monitoring command: %s", exc)

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

                if not self._monitoring:
                    if now - last_publish >= 1.0:
                        if snapshot.frame is not None and snapshot.sequence != self._last_sequence:
                            self._last_sequence = snapshot.sequence
                            display, _ = self.extractor._prepare(snapshot.frame)
                            self._latest_overlay = display
                        status = self._make_off_status(snapshot.status, snapshot.reconnect_count)
                        jpeg = self._encode_overlay(self._latest_overlay, status)
                        self.latest_status = status
                        self.web_state.update(status, jpeg)
                        self.mqtt.publish({key: value for key, value in status.items() if key not in _WEB_ONLY_STATUS_FIELDS})
                        last_publish = now
                    next_tick += interval
                    delay = next_tick - time.monotonic()
                    if delay > 0:
                        self._stop_event.wait(delay)
                    else:
                        next_tick = time.monotonic()
                    continue

                if snapshot.frame is not None and snapshot.timestamp is not None and snapshot.sequence != self._last_sequence:
                    self._last_sequence = snapshot.sequence
                    try:
                        observation, self._latest_overlay = self.extractor.process(snapshot.frame, snapshot.timestamp)
                        self.estimator.add(observation)
                        self.scanner.add(self.extractor.last_residual_dy, snapshot.timestamp, observation.excessive_motion)
                        self.presence.observe(snapshot.timestamp, observation.excessive_motion)
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
                    if estimate.breathing_signal:
                        self._roi_suggestion = None  # the configured region works again
                    presence_state = self.presence.update(now, estimate.breathing_signal)
                    self._handle_presence_scans(now, snapshot.status)
                    classification = self.classifier.update(estimate, now, presence_state)
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

    def _handle_presence_scans(self, now: float, stream_status: str) -> None:
        """Consume finished presence scans and start requested ones."""
        scan = self.scanner.snapshot()
        if scan["purpose"] == "presence" and scan["id"] > self._handled_scan_id and scan["state"] in ("done", "failed"):
            self._handled_scan_id = scan["id"]
            if scan["state"] == "done" and "result" in scan:
                self.presence.on_scan_completed(True, now)
                self._maybe_suggest_roi(scan["result"])
            elif scan.get("reason") == "no_periodic_motion_found":
                self.presence.on_scan_completed(False, now)
            else:  # motion during scan, too few frames, …: inconclusive
                self.presence.on_scan_completed(None, now)
        if (
            self.presence.wants_scan(now)
            and stream_status == "connected"
            and scan["state"] != "running"
        ):
            ok, _ = self.scanner.start(purpose="presence")
            if ok:
                self.presence.on_scan_started()

    def _maybe_suggest_roi(self, result: dict[str, Any]) -> None:
        """Presence scan found breathing; flag it when outside the set region."""
        sx, sy, sw, sh = result["roi"]
        cx, cy, cw, ch = self._camera.roi
        inter_w = max(0.0, min(sx + sw, cx + cw) - max(sx, cx))
        inter_h = max(0.0, min(sy + sh, cy + ch) - max(sy, cy))
        overlap = (inter_w * inter_h) / max(sw * sh, 1e-9)
        if overlap < 0.25:
            self._roi_suggestion = {"roi": result["roi"], "bpm": result["bpm"]}
            LOGGER.info(
                "breathing found outside the configured region (overlap %.0f%%) at ~%s BPM",
                overlap * 100,
                result["bpm"],
            )

    def _apply_pending(self) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return
        if "monitoring_enabled" in pending:
            enabled = bool(pending["monitoring_enabled"])
            if enabled != self._monitoring:
                self._monitoring = enabled
                LOGGER.info("monitoring %s", "enabled" if enabled else "paused")
                self.scanner.cancel()
                self.extractor.reset()
                self.estimator.clear()
                self.classifier.reset()
                self.presence.reset()
                self._roi_suggestion = None
                self._rate_low = False
        if any(key.startswith("mqtt_") for key in pending):
            new_config = self._effective_mqtt()
            if new_config != self.mqtt.config:
                LOGGER.info("applying new MQTT settings (enabled=%s host=%s)", new_config.enabled, new_config.host)
                self.mqtt.stop()
                self.mqtt = MQTTPublisher(new_config, on_monitoring_command=self._on_monitoring_command)
                self.mqtt.start()
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
        self._roi_suggestion = None

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

    def _update_rate_low(self, bpm: float | None) -> bool:
        """Low-rate flag with hysteresis: on below threshold, off at +2 BPM."""
        threshold = self.config.signal.low_rate_threshold_bpm
        if bpm is None or threshold <= 0:
            self._rate_low = False
        elif self._rate_low:
            self._rate_low = bpm < threshold + 2.0
        else:
            self._rate_low = bpm < threshold
        return self._rate_low

    def _make_status(
        self,
        estimate: RespirationEstimate,
        classification: Classification,
        stream_status: str,
        reconnect_count: int,
    ) -> dict[str, Any]:
        bpm = estimate.bpm if classification.state == DetectorState.BREATHING else None
        active_block = None
        rects = self.extractor.block_rects
        if (
            estimate.selected_block is not None
            and rects
            and estimate.selected_block < len(rects)
            and classification.state == DetectorState.BREATHING
        ):
            active_block = list(rects[estimate.selected_block])
        if not self.camera_configured:
            reason = "awaiting_camera_setup"
        elif stream_status != "connected":
            reason = f"stream_{stream_status}"
        else:
            reason = classification.reason
        return {
            "state": classification.state.value,
            "bpm": bpm,
            "rate_low": self._update_rate_low(bpm),
            "low_rate_threshold": self.config.signal.low_rate_threshold_bpm,
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
            "monitoring": True,
            "presence": self.presence.state.value,
            "presence_reason": self.presence.reason,
            "roi": list(self._camera.roi),
            "roi_suggestion": self._roi_suggestion,
            "active_block": active_block,
            "mqtt": {**self.mqtt.state(), "mode": self.settings.get().mqtt_mode},
            "scan": self.scanner.snapshot(),
            "waveform_t": estimate.waveform_t,
            "waveform_y": estimate.waveform_y,
        }

    def _make_off_status(self, stream_status: str, reconnect_count: int) -> dict[str, Any]:
        return {
            "state": "MONITORING_OFF",
            "bpm": None,
            "rate_low": False,
            "low_rate_threshold": self.config.signal.low_rate_threshold_bpm,
            "confidence": 0.0,
            "measurement_valid": False,
            "breathing_detected": None,
            "calibrated": False,
            "reason": "monitoring_disabled",
            "stream_status": stream_status,
            "reconnect_count": reconnect_count,
            "excessive_motion": False,
            "signal_rms": 0.0,
            "snr_db": None,
            "peak_concentration": 0.0,
            "estimated_fps": 0.0,
            "window_seconds": 0.0,
            "data_completeness": 0.0,
            "camera_configured": self.camera_configured,
            "version": __version__,
            "monitoring": False,
            "presence": "UNKNOWN",
            "presence_reason": "monitoring_disabled",
            "roi": list(self._camera.roi),
            "roi_suggestion": None,
            "active_block": None,
            "mqtt": {**self.mqtt.state(), "mode": self.settings.get().mqtt_mode},
            "scan": self.scanner.snapshot(),
            "waveform_t": [],
            "waveform_y": [],
        }

    @staticmethod
    def _encode_overlay(frame: Any, status: dict[str, Any]) -> bytes | None:
        del status  # the web client renders state; the frame stays clean
        if frame is None:
            return None
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return encoded.tobytes() if ok else None
