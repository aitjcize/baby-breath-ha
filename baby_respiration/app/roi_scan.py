"""Breathing-region auto-detection.

Instead of detecting a baby-shaped object (fragile under IR night vision and
blankets), this scanner looks for the signal we actually want: image blocks
whose vertical motion is periodic inside the configured breathing band. The
service feeds it the residual vertical flow the extractor already computes,
so a scan adds almost no per-frame cost.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import signal as scipy_signal

from app.config import SignalConfig, normalize_roi

LOGGER = logging.getLogger(__name__)

MINIMUM_SAMPLES_PER_SECOND = 2.5
MAXIMUM_EXCESSIVE_FRACTION = 0.3
# Blocks whose overall motion std exceeds this (pixels) are dominated by
# non-breathing movement (caregiver, pets, fans) and are excluded.
MAXIMUM_BLOCK_MOTION_STD = 0.6
CLUSTER_SCORE_FRACTION = 0.35
# White noise puts roughly the band's share of the spectrum (~0.5 here) into
# the breathing band; genuine breathing concentrates well above that.
MINIMUM_PERIODICITY = 0.6


@dataclass(frozen=True)
class ScanResult:
    roi: tuple[float, float, float, float]
    bpm: float
    quality: float  # 0-100 heuristic, fraction of variance in the breathing band
    heatmap: list[list[float]]  # normalized block scores for UI overlay


class BreathingRegionScanner:
    """Accumulates block-wise vertical flow and suggests the most periodic region."""

    def __init__(self, signal: SignalConfig, block_size: int = 20, duration: float = 30.0) -> None:
        self.signal = signal
        self.block_size = block_size
        self.default_duration = duration
        self._lock = threading.Lock()
        self._state = "idle"  # idle | running | done | failed
        self._reason = ""
        self._result: ScanResult | None = None
        self._duration = duration
        self._started_at: float | None = None
        self._grid_shape: tuple[int, int] | None = None
        self._samples: list[np.ndarray] = []
        self._timestamps: list[float] = []
        self._excessive_count = 0
        self._purpose = "user"  # "user" (wizard) | "presence" (occupancy check)
        self._scan_id = 0

    def start(self, duration: float | None = None, purpose: str = "user") -> tuple[bool, str]:
        with self._lock:
            if self._state == "running":
                return False, "A scan is already running."
            self._state = "running"
            self._reason = ""
            self._result = None
            self._duration = float(duration or self.default_duration)
            self._started_at = None
            self._grid_shape = None
            self._samples = []
            self._timestamps = []
            self._excessive_count = 0
            self._purpose = purpose
            self._scan_id += 1
            return True, "Scan started."

    def cancel(self) -> None:
        with self._lock:
            if self._state == "running":
                self._state = "idle"
                self._samples = []
                self._timestamps = []

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == "running"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            progress = 0.0
            if self._state == "running" and self._started_at is not None and self._timestamps:
                progress = min(1.0, (self._timestamps[-1] - self._started_at) / self._duration)
            payload: dict[str, Any] = {
                "state": self._state,
                "progress": round(progress, 3),
                "purpose": self._purpose,
                "id": self._scan_id,
            }
            if self._state == "failed":
                payload["reason"] = self._reason
            if self._result is not None:
                payload["result"] = {
                    "roi": list(self._result.roi),
                    "bpm": round(self._result.bpm, 1),
                    "quality": round(self._result.quality, 1),
                    "heatmap": self._result.heatmap,
                }
            return payload

    def add(self, residual_dy: np.ndarray | None, timestamp: float, excessive: bool) -> None:
        """Feed one frame of residual vertical flow; finalizes when the window fills."""
        with self._lock:
            if self._state != "running" or residual_dy is None:
                return
            height, width = residual_dy.shape
            blocks_y = height // self.block_size
            blocks_x = width // self.block_size
            if blocks_y < 2 or blocks_x < 2:
                self._state = "failed"
                self._reason = "frame_too_small_for_scan"
                return
            if self._grid_shape is None:
                self._grid_shape = (blocks_y, blocks_x)
            elif self._grid_shape != (blocks_y, blocks_x):
                # Stream resolution changed mid-scan; start over silently.
                self._samples = []
                self._timestamps = []
                self._started_at = None
                self._grid_shape = (blocks_y, blocks_x)
            if self._started_at is None:
                self._started_at = timestamp

            if excessive:
                self._excessive_count += 1
            else:
                cropped = residual_dy[: blocks_y * self.block_size, : blocks_x * self.block_size]
                blocked = cropped.reshape(blocks_y, self.block_size, blocks_x, self.block_size)
                medians = np.median(blocked, axis=(1, 3))
                self._samples.append(medians.astype(np.float32))
                self._timestamps.append(timestamp)

            if timestamp - self._started_at >= self._duration:
                self._finalize()

    def _finalize(self) -> None:
        total_frames = len(self._samples) + self._excessive_count
        if total_frames and self._excessive_count / total_frames > MAXIMUM_EXCESSIVE_FRACTION:
            self._state = "failed"
            self._reason = "excessive_motion_during_scan"
            return
        if len(self._samples) < self._duration * MINIMUM_SAMPLES_PER_SECOND:
            self._state = "failed"
            self._reason = "too_few_frames"
            return
        try:
            result = self._analyze()
        except Exception:
            LOGGER.exception("breathing-region scan analysis failed")
            self._state = "failed"
            self._reason = "analysis_error"
            return
        if result is None:
            self._state = "failed"
            self._reason = "no_periodic_motion_found"
        else:
            self._state = "done"
            self._result = result
        self._samples = []
        self._timestamps = []

    def _analyze(self) -> ScanResult | None:
        assert self._grid_shape is not None
        blocks_y, blocks_x = self._grid_shape
        timestamps = np.asarray(self._timestamps, dtype=np.float64)
        series = np.stack(self._samples)  # (T, blocks_y, blocks_x)
        dt = np.diff(timestamps)
        dt = dt[dt > 0]
        if dt.size == 0:
            return None
        sample_rate = float(1.0 / np.median(dt))
        grid_t = np.arange(timestamps[0], timestamps[-1], 1.0 / sample_rate)
        if grid_t.size < 32:
            return None

        low_hz = self.signal.min_bpm / 60.0
        high_hz = min(self.signal.max_bpm / 60.0, 0.45 * sample_rate)
        if high_hz <= low_hz:
            return None
        sos = scipy_signal.butter(3, [low_hz, high_hz], btype="bandpass", fs=sample_rate, output="sos")

        flat = series.reshape(series.shape[0], -1)  # (T, N)
        uniform = np.empty((grid_t.size, flat.shape[1]), dtype=np.float64)
        for index in range(flat.shape[1]):
            uniform[:, index] = np.interp(grid_t, timestamps, flat[:, index])
        detrended = scipy_signal.detrend(uniform, axis=0, type="linear")
        filtered = scipy_signal.sosfiltfilt(sos, detrended, axis=0)

        total_std = np.std(detrended, axis=0)
        band_std = np.std(filtered, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            periodicity = np.where(total_std > 1e-9, (band_std / np.maximum(total_std, 1e-12)) ** 2, 0.0)
        moving = (total_std >= self.signal.minimum_signal_rms) & (total_std <= MAXIMUM_BLOCK_MOTION_STD)
        scores = np.where(moving, periodicity * band_std, 0.0)

        # Dominant in-band frequency per block via FFT of the filtered series.
        spectrum = np.abs(np.fft.rfft(filtered * np.hanning(filtered.shape[0])[:, None], axis=0))
        frequencies = np.fft.rfftfreq(filtered.shape[0], d=1.0 / sample_rate)
        band = (frequencies >= low_hz) & (frequencies <= high_hz)
        if not np.any(band):
            return None
        band_indices = np.flatnonzero(band)
        peak_freqs = frequencies[band_indices[np.argmax(spectrum[band], axis=0)]]

        best = int(np.argmax(scores))
        if scores[best] <= 0 or periodicity[best] < MINIMUM_PERIODICITY:
            return None
        best_freq = float(peak_freqs[best])
        scores_grid = scores.reshape(blocks_y, blocks_x)
        freqs_grid = peak_freqs.reshape(blocks_y, blocks_x)
        cluster = self._grow_cluster(scores_grid, freqs_grid, best, float(scores[best]), best_freq)

        rows, cols = np.nonzero(cluster)
        pixel_h = blocks_y * self.block_size
        pixel_w = blocks_x * self.block_size
        pad = 0.5
        x0 = max(0.0, (cols.min() - pad) * self.block_size / pixel_w)
        y0 = max(0.0, (rows.min() - pad) * self.block_size / pixel_h)
        x1 = min(1.0, (cols.max() + 1 + pad) * self.block_size / pixel_w)
        y1 = min(1.0, (rows.max() + 1 + pad) * self.block_size / pixel_h)
        roi = normalize_roi(self._ensure_minimum_size((x0, y0, x1 - x0, y1 - y0)))

        max_score = float(scores_grid.max())
        heatmap = (scores_grid / max_score).round(3).tolist() if max_score > 0 else scores_grid.tolist()
        quality = float(np.clip(periodicity[best] * 100.0, 0.0, 100.0))
        return ScanResult(roi=roi, bpm=best_freq * 60.0, quality=quality, heatmap=heatmap)

    @staticmethod
    def _grow_cluster(
        scores: np.ndarray,
        freqs: np.ndarray,
        best_flat_index: int,
        best_score: float,
        best_freq: float,
    ) -> np.ndarray:
        blocks_y, blocks_x = scores.shape
        threshold = best_score * CLUSTER_SCORE_FRACTION
        tolerance = max(0.15 * best_freq, 0.05)
        eligible = (scores >= threshold) & (np.abs(freqs - best_freq) <= tolerance)
        cluster = np.zeros_like(eligible, dtype=bool)
        stack = [(best_flat_index // blocks_x, best_flat_index % blocks_x)]
        while stack:
            row, col = stack.pop()
            if not (0 <= row < blocks_y and 0 <= col < blocks_x):
                continue
            if cluster[row, col] or not eligible[row, col]:
                continue
            cluster[row, col] = True
            stack.extend([(row + 1, col), (row - 1, col), (row, col + 1), (row, col - 1)])
        return cluster

    @staticmethod
    def _ensure_minimum_size(roi: tuple[float, float, float, float], minimum: float = 0.08) -> tuple[float, float, float, float]:
        x, y, width, height = roi
        if width < minimum:
            x = max(0.0, min(1.0 - minimum, x - (minimum - width) / 2))
            width = minimum
        if height < minimum:
            y = max(0.0, min(1.0 - minimum, y - (minimum - height) / 2))
            height = minimum
        return x, y, width, height
