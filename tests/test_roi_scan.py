from __future__ import annotations

import numpy as np

from app.config import SignalConfig
from app.roi_scan import BreathingRegionScanner

HEIGHT, WIDTH = 240, 320
PATCH = (slice(100, 160), slice(160, 260))  # rows, cols in pixels


def feed_scanner(scanner: BreathingRegionScanner, *, bpm: float | None, seconds: float, fps: float = 5.0) -> None:
    rng = np.random.default_rng(42)
    timestamp = 100.0
    for _ in range(int(seconds * fps) + 2):
        timestamp += 1.0 / fps
        residual = rng.normal(0.0, 0.01, size=(HEIGHT, WIDTH))
        if bpm is not None:
            residual[PATCH] += 0.15 * np.sin(2 * np.pi * (bpm / 60.0) * (timestamp - 100.0))
        scanner.add(residual, timestamp, excessive=False)


def test_scan_finds_breathing_patch() -> None:
    scanner = BreathingRegionScanner(SignalConfig(), duration=20.0)
    ok, _ = scanner.start()
    assert ok
    feed_scanner(scanner, bpm=42.0, seconds=21.0)

    snapshot = scanner.snapshot()
    assert snapshot["state"] == "done"
    result = snapshot["result"]
    assert abs(result["bpm"] - 42.0) < 4.0
    x, y, width, height = result["roi"]
    # Suggested region must overlap the true patch center.
    cx, cy = 210 / WIDTH, 130 / HEIGHT
    assert x <= cx <= x + width
    assert y <= cy <= y + height
    assert result["quality"] > 60


def test_scan_rejects_pure_noise() -> None:
    scanner = BreathingRegionScanner(SignalConfig(), duration=20.0)
    scanner.start()
    feed_scanner(scanner, bpm=None, seconds=21.0)
    snapshot = scanner.snapshot()
    assert snapshot["state"] == "failed"
    assert snapshot["reason"] == "no_periodic_motion_found"


def test_scan_fails_on_excessive_motion() -> None:
    scanner = BreathingRegionScanner(SignalConfig(), duration=10.0)
    scanner.start()
    rng = np.random.default_rng(1)
    timestamp = 0.0
    for index in range(60):
        timestamp += 0.2
        scanner.add(rng.normal(0, 0.01, size=(HEIGHT, WIDTH)), timestamp, excessive=index % 2 == 0)
    assert scanner.snapshot()["state"] == "failed"
    assert scanner.snapshot()["reason"] == "excessive_motion_during_scan"
