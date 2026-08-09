from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import cv2
import numpy as np

LOGGER = logging.getLogger(__name__)


class CaptureLike(Protocol):
    def isOpened(self) -> bool: ...

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def release(self) -> None: ...


@dataclass(frozen=True)
class FrameSnapshot:
    frame: np.ndarray | None
    timestamp: float | None
    sequence: int
    status: str
    last_error: str | None
    reconnect_count: int


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    frame: np.ndarray | None = None


def probe_stream(url: str, open_timeout_ms: int = 8000, read_timeout_ms: int = 8000) -> ProbeResult:
    """Synchronously test an RTSP URL and grab one representative frame.

    Blocks up to roughly the configured timeouts; callers should run it off
    the main processing loop. Reads a few frames because the first decoded
    frame of a fresh RTSP session is often partial.
    """
    if not url.strip():
        return ProbeResult(False, "Stream URL is empty.")
    capture = cv2.VideoCapture()
    parameters: list[int] = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        parameters.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_timeout_ms])
    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        parameters.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, read_timeout_ms])
    try:
        opened = capture.open(url, cv2.CAP_FFMPEG, parameters)
        if not opened or not capture.isOpened():
            return ProbeResult(False, "Could not connect. Check the URL, credentials, and that the camera allows another RTSP client.")
        frame = None
        for _ in range(4):
            ok, candidate = capture.read()
            if ok and candidate is not None and candidate.size > 0:
                frame = candidate
        if frame is None:
            return ProbeResult(False, "Connected, but no video frames arrived. The stream path may be wrong.")
        height, width = frame.shape[:2]
        return ProbeResult(True, f"Connected: {width}x{height} video.", frame)
    except cv2.error as exc:
        return ProbeResult(False, f"OpenCV could not read the stream: {exc}")
    finally:
        capture.release()


class RTSPFrameSource:
    """Continuously drains an RTSP stream and exposes only the newest frame."""

    def __init__(
        self,
        url: str,
        reconnect_interval: float = 5.0,
        open_timeout_ms: int = 8000,
        read_timeout_ms: int = 8000,
        capture_factory: Callable[[str], CaptureLike] | None = None,
    ) -> None:
        self.url = url
        self.reconnect_interval = reconnect_interval
        self.open_timeout_ms = open_timeout_ms
        self.read_timeout_ms = read_timeout_ms
        self._capture_factory = capture_factory or self._open_capture
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame: np.ndarray | None = None
        self._timestamp: float | None = None
        self._sequence = 0
        self._status = "stopped"
        self._last_error: str | None = None
        self._reconnect_count = 0

    def _open_capture(self, url: str) -> CaptureLike:
        capture = cv2.VideoCapture()
        parameters: list[int] = []
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            parameters.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms])
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            parameters.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms])
        capture.open(url, cv2.CAP_FFMPEG, parameters)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._set_status("connecting")
        self._thread = threading.Thread(target=self._run, name="rtsp-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._set_status("stopped")

    def snapshot(self, copy_frame: bool = False) -> FrameSnapshot:
        with self._lock:
            frame = self._frame.copy() if copy_frame and self._frame is not None else self._frame
            return FrameSnapshot(
                frame=frame,
                timestamp=self._timestamp,
                sequence=self._sequence,
                status=self._status,
                last_error=self._last_error,
                reconnect_count=self._reconnect_count,
            )

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._last_error = error

    def _run(self) -> None:
        while not self._stop_event.is_set():
            capture: CaptureLike | None = None
            try:
                self._set_status("connecting")
                capture = self._capture_factory(self.url)
                if not capture.isOpened():
                    raise ConnectionError("could not open RTSP stream")
                self._set_status("connected")
                LOGGER.info("RTSP stream connected")

                while not self._stop_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None or frame.size == 0:
                        raise ConnectionError("RTSP frame read failed")
                    with self._lock:
                        self._frame = frame
                        self._timestamp = time.monotonic()
                        self._sequence += 1
                        self._status = "connected"
                        self._last_error = None
            except Exception as exc:  # capture backends raise several exception types
                with self._lock:
                    self._status = "reconnecting"
                    self._last_error = str(exc)
                    self._reconnect_count += 1
                LOGGER.warning("RTSP unavailable (%s); reconnecting in %.1fs", exc, self.reconnect_interval)
            finally:
                if capture is not None:
                    capture.release()

            self._stop_event.wait(self.reconnect_interval)


class SyntheticFrameSource:
    """Demo camera (``demo://breathing``): a textured scene with a breathing patch.

    Lets users explore the onboarding flow, region scan, and dashboard without
    a real camera, and gives tests a full end-to-end signal path. The patch
    moves vertically by under a pixel at a configurable rate, mimicking chest
    motion seen by a fixed camera.
    """

    WIDTH = 320
    HEIGHT = 240
    PATCH = (150, 96, 250, 168)  # x0, y0, x1, y1 of the "chest"

    def __init__(self, bpm: float = 40.0, fps: float = 10.0, amplitude: float = 0.6) -> None:
        self.bpm = bpm
        self.fps = fps
        self.amplitude = amplitude
        rng = np.random.default_rng(20260809)
        base = rng.integers(40, 200, size=(self.HEIGHT, self.WIDTH), dtype=np.uint8)
        base = cv2.GaussianBlur(base, (0, 0), 1.2)
        self._base = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
        self._rng = np.random.default_rng(1)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame: np.ndarray | None = None
        self._timestamp: float | None = None
        self._sequence = 0
        self._started = 0.0

    def render(self, elapsed: float) -> np.ndarray:
        frame = self._base.copy()
        x0, y0, x1, y1 = self.PATCH
        shift = self.amplitude * np.sin(2.0 * np.pi * (self.bpm / 60.0) * elapsed)
        transform = np.float32([[1, 0, 0], [0, 1, shift]])
        patch = frame[y0:y1, x0:x1]
        frame[y0:y1, x0:x1] = cv2.warpAffine(patch, transform, (x1 - x0, y1 - y0), borderMode=cv2.BORDER_REFLECT)
        noise = self._rng.normal(0.0, 1.5, size=frame.shape)
        return np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        LOGGER.warning("demo camera active — synthetic breathing scene, not a real stream")
        self._stop_event.clear()
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="demo-camera", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            frame = self.render(now - self._started)
            with self._lock:
                self._frame = frame
                self._timestamp = now
                self._sequence += 1
            self._stop_event.wait(1.0 / self.fps)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def snapshot(self, copy_frame: bool = False) -> FrameSnapshot:
        with self._lock:
            frame = self._frame.copy() if copy_frame and self._frame is not None else self._frame
            return FrameSnapshot(frame, self._timestamp, self._sequence, "connected" if frame is not None else "connecting", None, 0)


class DisabledFrameSource:
    """Safe source used when no camera URL has been configured."""

    def start(self) -> None:
        LOGGER.warning("camera.rtsp_url is empty; running with stream unavailable")

    def stop(self, timeout: float = 0) -> None:
        del timeout

    def snapshot(self, copy_frame: bool = False) -> FrameSnapshot:
        del copy_frame
        return FrameSnapshot(None, None, 0, "not_configured", "camera.rtsp_url is empty", 0)
