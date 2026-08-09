from __future__ import annotations

import time

import numpy as np

from app.capture import RTSPFrameSource


class FakeCapture:
    def __init__(self, opened: bool, frames: list[np.ndarray | None]) -> None:
        self.opened = opened
        self.frames = frames
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self.frames:
            time.sleep(0.002)
            return False, None
        frame = self.frames.pop(0)
        return frame is not None, frame

    def release(self) -> None:
        self.released = True


def test_rtsp_reconnects_after_failed_open() -> None:
    expected = np.ones((8, 8, 3), dtype=np.uint8)
    captures = [FakeCapture(False, []), FakeCapture(True, [expected] * 30)]
    calls = 0

    def factory(url: str) -> FakeCapture:
        nonlocal calls
        assert url == "rtsp://test.invalid/stream"
        index = min(calls, len(captures) - 1)
        calls += 1
        return captures[index]

    source = RTSPFrameSource("rtsp://test.invalid/stream", reconnect_interval=0.01, capture_factory=factory)
    source.start()
    deadline = time.monotonic() + 1.0
    snapshot = source.snapshot()
    while snapshot.sequence == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
        snapshot = source.snapshot()
    source.stop()

    assert calls >= 2
    assert snapshot.sequence > 0
    assert snapshot.frame is not None
    assert snapshot.reconnect_count >= 1
    assert captures[0].released

