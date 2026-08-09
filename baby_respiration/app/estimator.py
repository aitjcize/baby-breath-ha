from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
from scipy import signal as scipy_signal

from app.config import CameraConfig, SignalConfig
from app.motion import MotionObservation


@dataclass(frozen=True)
class RespirationEstimate:
    bpm: float | None = None
    confidence: float = 0.0
    technical_valid: bool = False
    signal_observable: bool = False
    breathing_signal: bool = False
    signal_rms: float = 0.0
    snr_db: float | None = None
    peak_concentration: float = 0.0
    data_completeness: float = 0.0
    motion_stability: float = 0.0
    estimated_fps: float = 0.0
    window_seconds: float = 0.0
    excessive_motion: bool = False
    reason: str = "insufficient_data"
    waveform_t: list[float] = field(default_factory=list)
    waveform_y: list[float] = field(default_factory=list)


class RespirationEstimator:
    def __init__(self, camera: CameraConfig, config: SignalConfig) -> None:
        self.camera = camera
        self.config = config
        self._history: deque[MotionObservation] = deque()

    def clear(self) -> None:
        self._history.clear()

    def add(self, observation: MotionObservation) -> None:
        self._history.append(observation)
        cutoff = observation.timestamp - max(60.0, self.config.analysis_window_duration * 1.5)
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

    def estimate(self, now: float | None = None) -> RespirationEstimate:
        if not self._history:
            return RespirationEstimate()
        if now is None:
            now = self._history[-1].timestamp
        cutoff = now - self.config.analysis_window_duration
        observations = [item for item in self._history if item.timestamp >= cutoff]
        if len(observations) < 3:
            return RespirationEstimate(reason="insufficient_samples")

        timestamps = np.asarray([item.timestamp for item in observations], dtype=np.float64)
        duration = float(timestamps[-1] - timestamps[0])
        frame_timestamps = np.asarray(
            [item.timestamp for item in observations if item.value is not None],
            dtype=np.float64,
        )
        positive_dt = np.diff(frame_timestamps)
        positive_dt = positive_dt[positive_dt > 0]
        estimated_fps = float(1.0 / np.median(positive_dt)) if positive_dt.size else 0.0
        excessive_fraction = float(np.mean([item.excessive_motion for item in observations]))
        stability = max(0.0, 1.0 - excessive_fraction)
        base = dict(
            estimated_fps=estimated_fps,
            window_seconds=duration,
            excessive_motion=observations[-1].excessive_motion,
            motion_stability=stability,
        )
        if duration < self.config.minimum_valid_window_duration:
            return RespirationEstimate(reason="window_too_short", **base)
        minimum_sampling_rate = 2.0 * self.config.max_bpm / 60.0
        if estimated_fps <= minimum_sampling_rate:
            return RespirationEstimate(reason="video_fps_too_low", **base)

        valid = [item for item in observations if item.valid and item.value is not None]
        valid_fraction = len(valid) / len(observations)
        if len(valid) < 8:
            return RespirationEstimate(
                data_completeness=valid_fraction,
                reason="too_few_valid_motion_samples",
                **base,
            )

        valid_t = np.asarray([item.timestamp for item in valid], dtype=np.float64)
        valid_y = np.asarray([item.value for item in valid], dtype=np.float64)
        sample_rate = min(self.camera.processing_fps, estimated_fps)
        grid = np.arange(timestamps[0], timestamps[-1], 1.0 / sample_rate)
        if grid.size < 16:
            return RespirationEstimate(reason="too_few_resampled_samples", **base)

        interpolated = np.interp(grid, valid_t, valid_y)
        insertion = np.searchsorted(valid_t, grid)
        left = np.clip(insertion - 1, 0, len(valid_t) - 1)
        right = np.clip(insertion, 0, len(valid_t) - 1)
        nearest_distance = np.minimum(np.abs(grid - valid_t[left]), np.abs(grid - valid_t[right]))
        gap_coverage = float(np.mean(nearest_distance <= self.config.maximum_interpolation_gap / 2.0))
        completeness = valid_fraction * gap_coverage

        technical_valid = completeness >= 0.75 and excessive_fraction <= 0.15
        if not technical_valid:
            reason = "excessive_motion" if excessive_fraction > 0.15 else "incomplete_motion_data"
            return RespirationEstimate(
                technical_valid=False,
                data_completeness=completeness,
                reason=reason,
                **base,
            )

        detrended = scipy_signal.detrend(interpolated, type="linear")
        low_hz = self.config.min_bpm / 60.0
        high_hz = self.config.max_bpm / 60.0
        sos = scipy_signal.butter(3, [low_hz, high_hz], btype="bandpass", fs=sample_rate, output="sos")
        try:
            filtered = scipy_signal.sosfiltfilt(sos, detrended)
        except ValueError:
            return RespirationEstimate(
                technical_valid=True,
                data_completeness=completeness,
                reason="filter_window_too_short",
                **base,
            )

        frequencies, psd = scipy_signal.welch(
            filtered,
            fs=sample_rate,
            window="hann",
            nperseg=len(filtered),
            detrend=False,
            scaling="density",
        )
        band_mask = (frequencies >= low_hz) & (frequencies <= high_hz)
        band_indices = np.flatnonzero(band_mask)
        if band_indices.size < 3 or not np.any(psd[band_mask] > 0):
            return RespirationEstimate(
                technical_valid=True,
                data_completeness=completeness,
                reason="no_spectral_energy",
                **base,
            )

        peak_index = int(band_indices[np.argmax(psd[band_mask])])
        peak_hz = float(frequencies[peak_index])
        if 0 < peak_index < len(psd) - 1:
            y0, y1, y2 = np.log(np.maximum(psd[peak_index - 1:peak_index + 2], 1e-20))
            denominator = y0 - 2.0 * y1 + y2
            if abs(denominator) > 1e-12:
                offset = float(np.clip(0.5 * (y0 - y2) / denominator, -0.5, 0.5))
                peak_hz += offset * float(frequencies[1] - frequencies[0])

        peak_mask = band_mask & (np.abs(frequencies - peak_hz) <= 0.12)
        band_power = float(np.sum(psd[band_mask]))
        peak_power = float(np.sum(psd[peak_mask]))
        concentration = peak_power / max(band_power, 1e-20)
        noise_values = psd[band_mask & ~peak_mask]
        noise_floor = float(np.median(noise_values)) if noise_values.size else 1e-20
        peak_density = float(np.mean(psd[peak_mask]))
        snr_db = float(10.0 * np.log10(max(peak_density, 1e-20) / max(noise_floor, 1e-20)))
        rms = float(np.sqrt(np.mean(np.square(filtered))))

        snr_score = float(np.clip((snr_db - self.config.minimum_snr_db) / 15.0, 0.0, 1.0))
        amplitude_score = float(np.clip(rms / max(self.config.minimum_signal_rms * 2.0, 1e-9), 0.0, 1.0))
        confidence = 100.0 * (
            0.50 * np.clip(concentration, 0.0, 1.0)
            + 0.25 * snr_score
            + 0.10 * amplitude_score
            + 0.10 * np.clip(completeness, 0.0, 1.0)
            + 0.05 * stability
        )
        confidence = float(np.clip(confidence, 0.0, 100.0))
        signal_observable = (
            rms >= self.config.minimum_signal_rms
            and snr_db >= self.config.minimum_snr_db
        )
        breathing_signal = signal_observable and confidence >= self.config.minimum_confidence
        reason = "breathing_signal" if breathing_signal else (
            "signal_snr_too_low" if not signal_observable else "periodicity_confidence_low"
        )

        waveform_stride = max(1, len(filtered) // 300)
        waveform_t = (grid[::waveform_stride] - grid[-1]).round(3).tolist()
        waveform_y = filtered[::waveform_stride].round(6).tolist()
        return RespirationEstimate(
            bpm=round(peak_hz * 60.0, 2),
            confidence=round(confidence, 1),
            technical_valid=True,
            signal_observable=signal_observable,
            breathing_signal=breathing_signal,
            signal_rms=rms,
            snr_db=round(snr_db, 2),
            peak_concentration=concentration,
            data_completeness=completeness,
            reason=reason,
            waveform_t=waveform_t,
            waveform_y=waveform_y,
            **base,
        )
