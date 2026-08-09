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


class DisabledFrameSource:
    """Safe source used when no camera URL has been configured."""

    def start(self) -> None:
        LOGGER.warning("camera.rtsp_url is empty; running with stream unavailable")

    def stop(self, timeout: float = 0) -> None:
        del timeout

    def snapshot(self, copy_frame: bool = False) -> FrameSnapshot:
        del copy_frame
        return FrameSnapshot(None, None, 0, "not_configured", "camera.rtsp_url is empty", 0)
