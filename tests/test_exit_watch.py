from __future__ import annotations

import numpy as np

from app.exit_watch import RegionExitWatch

SHAPE = (180, 320)
ROI = (0.3, 0.3, 0.4, 0.4)


def blob(cx: float, cy: float, radius: int = 16, magnitude: float = 2.0) -> np.ndarray:
    field = np.zeros(SHAPE, dtype=np.float32)
    y, x = int(cy * SHAPE[0]), int(cx * SHAPE[1])
    yy, xx = np.ogrid[: SHAPE[0], : SHAPE[1]]
    field[(yy - y) ** 2 + (xx - x) ** 2 <= radius**2] = magnitude
    return field


def test_crawl_from_inside_to_outside_fires() -> None:
    watch = RegionExitWatch(ROI)
    t = 0.0
    for _ in range(8):  # origin establishes inside the box
        watch.observe(t, blob(0.5, 0.5))
        t += 0.2
    for step in range(15):  # crawl to the right edge and beyond
        watch.observe(t, blob(0.5 + 0.03 * step, 0.5))
        t += 0.2
    for _ in range(12):  # linger outside
        watch.observe(t, blob(0.92, 0.5))
        t += 0.2
    assert watch.fired
    assert watch.snapshot()["exit_position"][0] > 0.7


def test_caregiver_reach_from_outside_never_fires() -> None:
    watch = RegionExitWatch(ROI)
    t = 0.0
    for _ in range(8):  # origin outside the box (frame edge)
        watch.observe(t, blob(0.95, 0.5))
        t += 0.2
    for step in range(10):  # reach into the box…
        watch.observe(t, blob(0.95 - 0.05 * step, 0.5))
        t += 0.2
    for _ in range(12):  # …and back out
        watch.observe(t, blob(0.95, 0.5))
        t += 0.2
    assert not watch.fired


def test_stirring_in_place_never_fires() -> None:
    watch = RegionExitWatch(ROI)
    t = 0.0
    for _ in range(60):
        watch.observe(t, blob(0.5, 0.5))
        t += 0.2
    assert not watch.fired


def test_small_twitch_ignored_and_clear_resets() -> None:
    watch = RegionExitWatch(ROI)
    t = 0.0
    for _ in range(20):  # below the area floor: no trail at all
        watch.observe(t, blob(0.5, 0.5, radius=6))
        t += 0.2
    assert watch._trail_start is None

    # force-fire then clear
    for _ in range(8):
        watch.observe(t, blob(0.5, 0.5))
        t += 0.2
    for _ in range(20):
        watch.observe(t, blob(0.92, 0.5))
        t += 0.2
    assert watch.fired
    watch.clear()
    assert not watch.fired and watch.snapshot() is None
