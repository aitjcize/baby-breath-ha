"""Bed-exit watch: fire fast when the baby moves itself out of the region.

The presence machinery answers "is anyone here" on a scans-and-minutes
timescale; this answers "did the baby just crawl out of the monitored box"
in seconds, using the full-frame motion field the extractor already
computes. The discriminator is the trail's ORIGIN: a crawl starts inside
the user's box; a caregiver's reach (or a co-sleeping adult stirring)
originates outside it. Only inside-origin trails that cross the boundary
and stay out fire the event.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

# Gross locomotion is orders of magnitude above breathing (~0.01 px) and
# twitches (~0.2 px); the area floor plus duration filters brief limb flicks.
MAGNITUDE_THRESHOLD = 0.8
MIN_AREA_FRACTION = 0.012
ORIGIN_WINDOW = 1.0  # first second of a trail defines its origin
CONFIRM_SECONDS = 1.5  # centroid must stay outside the box this long
TRAIL_GAP_RESET = 2.0  # motion pause longer than this ends the trail
BOX_MARGIN = 0.02


class RegionExitWatch:
    def __init__(self, roi: tuple[float, float, float, float]) -> None:
        self.roi = roi
        self._trail_start: float | None = None
        self._last_motion: float | None = None
        self._origin: tuple[float, float] | None = None
        self._origin_samples: list[tuple[float, float]] = []
        self._outside_since: float | None = None
        self._fired = False
        self._fired_at: float | None = None
        self._exit_position: tuple[float, float] | None = None

    def _inside(self, x: float, y: float, margin: float = BOX_MARGIN) -> bool:
        rx, ry, rw, rh = self.roi
        return (rx - margin) <= x <= (rx + rw + margin) and (ry - margin) <= y <= (ry + rh + margin)

    def observe(self, now: float, magnitude: np.ndarray | None) -> None:
        """Feed the full-frame residual motion magnitude for one frame."""
        if magnitude is None:
            return
        mask = magnitude > MAGNITUDE_THRESHOLD
        area = float(np.count_nonzero(mask)) / mask.size
        if area < MIN_AREA_FRACTION:
            if self._last_motion is not None and now - self._last_motion > TRAIL_GAP_RESET:
                self._end_trail()
            return

        rows, cols = np.nonzero(mask)
        weights = magnitude[rows, cols]
        cy = float(np.average(rows, weights=weights)) / magnitude.shape[0]
        cx = float(np.average(cols, weights=weights)) / magnitude.shape[1]
        self._last_motion = now

        if self._trail_start is None:
            self._trail_start = now
            self._origin = None
            self._origin_samples = []
            self._outside_since = None
        if self._origin is None:
            self._origin_samples.append((cx, cy))
            if now - self._trail_start >= ORIGIN_WINDOW:
                xs = [p[0] for p in self._origin_samples]
                ys = [p[1] for p in self._origin_samples]
                self._origin = (sum(xs) / len(xs), sum(ys) / len(ys))
            return

        origin_inside = self._inside(*self._origin)
        if not origin_inside or self._fired:
            return
        if self._inside(cx, cy):
            self._outside_since = None
            return
        if self._outside_since is None:
            self._outside_since = now
        elif now - self._outside_since >= CONFIRM_SECONDS:
            self._fired = True
            self._fired_at = now
            self._exit_position = (round(cx, 3), round(cy, 3))
            LOGGER.warning(
                "baby appears to have left the monitored region (motion trail from "
                "inside the box crossed out at x=%.2f y=%.2f)",
                cx,
                cy,
            )

    def _end_trail(self) -> None:
        self._trail_start = None
        self._origin = None
        self._origin_samples = []
        self._outside_since = None
        self._last_motion = None

    def clear(self) -> None:
        """Breathing re-locked inside the box (or region changed)."""
        if self._fired:
            LOGGER.info("region-exit event cleared")
        self._fired = False
        self._fired_at = None
        self._exit_position = None
        self._end_trail()

    @property
    def fired(self) -> bool:
        return self._fired

    def snapshot(self) -> dict[str, Any] | None:
        if not self._fired:
            return None
        return {"exit_position": list(self._exit_position or ()), "since": self._fired_at}
