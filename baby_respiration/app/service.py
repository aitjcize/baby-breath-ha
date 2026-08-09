from __future__ import annotations

import logging
import threading
import time
from typing import Any

import cv2

from app.capture import DisabledFrameSource, RTSPFrameSource
from app.classifier import Classification, ConservativeClassifier, DetectorState
from app.config import AppConfig
from app.debug_server import DebugServer, DebugState
from app.estimator import RespirationEstimate, RespirationEstimator
from app.motion import DenseOpticalFlowExtractor, MotionObservation
from app.mqtt import MQTTPublisher

LOGGER = logging.getLogger(__name__)


class BabyRespirationService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        configured_url = config.camera.rtsp_url.strip()
        if not configured_url or "$" in configured_url:
            self.source = DisabledFrameSource()
        else:
            self.source = RTSPFrameSource(
                configured_url,
                reconnect_interval=config.camera.reconnect_interval,
                open_timeout_ms=config.camera.open_timeout_ms,
                read_timeout_ms=config.camera.read_timeout_ms,
            )
        self.extractor = DenseOpticalFlowExtractor(config.camera, config.signal)
        self.estimator = RespirationEstimator(config.camera, config.signal)
        self.classifier = ConservativeClassifier(config.signal)
        self.mqtt = MQTTPublisher(config.mqtt)
        self.debug_state = DebugState()
        self.debug_server: DebugServer | None = None
        self._stop_event = threading.Event()
        self.latest_status: dict[str, Any] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self, run_seconds: float | None = None) -> None:
        started = time.monotonic()
        if self.config.debug.enabled:
            try:
                self.debug_server = DebugServer(self.config.debug.host, self.config.debug.port, self.debug_state)
                self.debug_server.start()
            except OSError as exc:
                LOGGER.error("debug UI could not start: %s", exc)
        self.mqtt.start()
        self.source.start()
        interval = 1.0 / self.config.camera.processing_fps
        next_tick = time.monotonic()
        last_sequence = -1
        last_publish = 0.0
        latest_overlay = None

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if run_seconds is not None and now - started >= run_seconds:
                    break
                snapshot = self.source.snapshot()
                observation_added = False

                if snapshot.frame is not None and snapshot.timestamp is not None and snapshot.sequence != last_sequence:
                    last_sequence = snapshot.sequence
                    try:
                        observation, latest_overlay = self.extractor.process(snapshot.frame, snapshot.timestamp)
                        self.estimator.add(observation)
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
                    jpeg = self._encode_overlay(latest_overlay, status)
                    self.latest_status = status
                    self.debug_state.update(status, jpeg)
                    self.mqtt.publish(status)
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
            if self.debug_server:
                self.debug_server.stop()
            LOGGER.info("service stopped cleanly")

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

    @staticmethod
    def _make_status(
        estimate: RespirationEstimate,
        classification: Classification,
        stream_status: str,
        reconnect_count: int,
    ) -> dict[str, Any]:
        bpm = estimate.bpm if classification.state == DetectorState.BREATHING else None
        reason = classification.reason if stream_status == "connected" else f"stream_{stream_status}"
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
