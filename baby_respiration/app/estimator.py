from __future__ import annotations

from collections import Counter, deque
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
    selected_block: int | None = None
    invalid_breakdown: dict[str, int] = field(default_factory=dict)
    waveform_t: list[float] = field(default_factory=list)
    waveform_y: list[float] = field(default_factory=list)


WARM_START_WINDOW = 120.0  # seconds of relaxed floors after a warm restart


class RespirationEstimator:
    def __init__(self, camera: CameraConfig, config: SignalConfig) -> None:
        self.camera = camera
        self.config = config
        self._history: deque[MotionObservation] = deque()
        self._last_breathing_at: float | None = None
        self._selected_block: int | None = None
        self._last_peak_hz: float | None = None
        self._warm_pending_hz: float | None = None
        self._warm_deadline: float | None = None

    def warm_start(self, peak_hz: float | None) -> None:
        """Restore pre-restart lock context: the service was locked moments
        ago, so re-locking may use the relaxed (recently-breathing) floors
        and the rate-continuity escape for a bounded window. An empty bed
        was never locked, so the cold-start noise defenses are unaffected."""
        if peak_hz and peak_hz > 0:
            self._warm_pending_hz = float(peak_hz)

    def clear(self) -> None:
        self._history.clear()
        self._last_breathing_at = None
        self._selected_block = None
        self._last_peak_hz = None
        self._warm_pending_hz = None
        self._warm_deadline = None

    @staticmethod
    def _block_matrix(valid: list[MotionObservation]) -> tuple[np.ndarray, tuple[int, int]] | None:
        if not valid or any(item.block_values is None or item.block_grid is None for item in valid):
            return None
        shapes = {(len(item.block_values), item.block_grid) for item in valid}  # type: ignore[arg-type]
        if len(shapes) != 1:
            return None
        count, grid_shape = shapes.pop()
        if count <= 1:
            return None
        return np.asarray([item.block_values for item in valid], dtype=np.float64), grid_shape

    # Real breathing concentrates its variance in the band (white noise puts
    # only ~half there), moves a chest-sized area coherently, and is a spatial
    # HOT SPOT: the chest block moves far more than the background blocks.
    # Optical-flow noise correlates neighbours by construction (overlapping
    # estimation windows) and camera-wide artifacts are spatially uniform, so
    # localization contrast is the check noise cannot fake.
    MINIMUM_BLOCK_PERIODICITY = 0.6
    NEIGHBOR_CORRELATION = 0.5
    NEIGHBOR_AMPLITUDE_FRACTION = 0.2
    BACKGROUND_CONTRAST = 3.0
    CONTRAST_MINIMUM_BLOCKS = 12

    def _select_block(
        self,
        matrix: np.ndarray,
        grid_shape: tuple[int, int],
        valid_t: np.ndarray,
        grid: np.ndarray,
        sample_rate: float,
        was_breathing: bool = False,
    ) -> int | None:
        """Pick the ROI block with the strongest breathing-band periodicity.

        Requires an adjacent block moving in phase (spatial coherence) so the
        maximum-over-blocks search cannot fabricate breathing from noise or
        single-block environmental flutter, and prefers the previously
        selected block so the measurement doesn't hop between neighbours.
        """
        low_hz = self.config.min_bpm / 60.0
        high_hz = min(self.config.max_bpm / 60.0, 0.45 * sample_rate)
        if high_hz <= low_hz:
            return None
        try:
            sos = scipy_signal.butter(3, [low_hz, high_hz], btype="bandpass", fs=sample_rate, output="sos")
            uniform = np.empty((grid.size, matrix.shape[1]), dtype=np.float64)
            for index in range(matrix.shape[1]):
                uniform[:, index] = np.interp(grid, valid_t, matrix[:, index])
            detrended = scipy_signal.detrend(uniform, axis=0, type="linear")
            filtered = scipy_signal.sosfiltfilt(sos, detrended, axis=0)
        except ValueError:
            return None
        total_std = detrended.std(axis=0)
        band_std = filtered.std(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            periodicity = np.where(total_std > 1e-9, (band_std / np.maximum(total_std, 1e-12)) ** 2, 0.0)
        scores = periodicity * band_std

        background = float(np.median(band_std))
        # Shallow breathing (position/covering dependent) hovers at the
        # hardening floors; while recently locked, relax them Schmitt-style.
        # Cold lock-on keeps full hardening: an empty bed is never
        # "recently breathing", so the noise defenses are unchanged there.
        min_periodicity = 0.5 if was_breathing else self.MINIMUM_BLOCK_PERIODICITY
        min_contrast = 2.0 if was_breathing else self.BACKGROUND_CONTRAST
        min_correlation = 0.35 if was_breathing else self.NEIGHBOR_CORRELATION

        def supported(index: int) -> bool:
            if periodicity[index] < min_periodicity:
                return False
            # Localization: a chest is a hot spot against the background;
            # uniform flow noise / AGC / codec pulses are not.
            if matrix.shape[1] >= self.CONTRAST_MINIMUM_BLOCKS and band_std[index] < min_contrast * background:
                return False
            rows, cols = grid_shape
            row, col = index // cols, index % cols
            reference = filtered[:, index]
            reference_std = band_std[index]
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = row + dr, col + dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        continue
                    neighbor = nr * cols + nc
                    if band_std[neighbor] < self.NEIGHBOR_AMPLITUDE_FRACTION * reference_std:
                        continue
                    correlation = float(np.corrcoef(reference, filtered[:, neighbor])[0, 1])
                    if correlation >= min_correlation:
                        return True
            return False

        order = np.argsort(scores)[::-1]
        best: int | None = None
        for candidate in order[:5]:  # the strongest few; beyond them it is noise
            if scores[candidate] <= 0:
                break
            if supported(int(candidate)):
                best = int(candidate)
                break
        if best is None:
            self._selected_block = None
            return None
        previous = self._selected_block
        if (
            previous is not None
            and previous < scores.size
            and previous != best
            and scores[previous] >= 0.75 * scores[best]
            and supported(previous)
        ):
            best = previous
        self._selected_block = best
        return best

    def _apply_gates(
        self,
        rms: float,
        snr_db: float,
        confidence: float,
        was_breathing: bool,
    ) -> tuple[bool, bool]:
        """Schmitt-trigger gating: a marginal dip does not unlock a held signal.

        The amplitude floor always applies — 24 s noise spectra fluke
        concentrated peaks often enough that spectral evidence alone cannot be
        allowed to waive it. Block-adaptive measurement removed the dilution
        problem the old override compensated for: real breathing now measures
        at full block amplitude, comfortably above any sane floor.
        """
        rms_floor = self.config.minimum_signal_rms
        snr_floor = self.config.minimum_snr_db
        confidence_floor = self.config.minimum_confidence
        if was_breathing:
            rms_floor *= 0.7
            snr_floor -= 1.5
            confidence_floor -= 8.0
        observable = rms >= rms_floor and snr_db >= snr_floor
        breathing = observable and confidence >= confidence_floor
        return observable, breathing

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
        # "Recently breathing" is a grace window aligned with the reporting
        # hold: one rough second must not force the next windows back to cold
        # lock-on thresholds while the physiology is still settling.
        grace = max(self.config.detection_hold_seconds, 0.0)
        if self._warm_pending_hz is not None:
            self._warm_deadline = now + WARM_START_WINDOW
            self._last_peak_hz = self._warm_pending_hz
            self._warm_pending_hz = None
        was_breathing = (
            self._last_breathing_at is not None and now - self._last_breathing_at <= grace
        ) or (self._warm_deadline is not None and now <= self._warm_deadline)
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
        breakdown: Counter[str] = Counter()
        for item in observations:
            if not item.valid:
                for part in item.reason.split(","):
                    breakdown[part] += 1
        base = dict(
            estimated_fps=estimated_fps,
            window_seconds=duration,
            excessive_motion=observations[-1].excessive_motion,
            motion_stability=stability,
            invalid_breakdown=dict(breakdown),
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
        sample_rate = min(self.camera.processing_fps, estimated_fps)
        grid = np.arange(timestamps[0], timestamps[-1], 1.0 / sample_rate)
        if grid.size < 16:
            return RespirationEstimate(reason="too_few_resampled_samples", **base)

        # Block-adaptive measurement: the user's box is a search area. Score
        # every ROI block for breathing-band periodicity and measure from the
        # one that carries the rhythm — a large box drawn to allow the baby to
        # move no longer dilutes the signal with static bedding.
        selected_block: int | None = None
        blocks = self._block_matrix(valid)
        if blocks is not None:
            block_matrix, block_grid_shape = blocks
            selected_block = self._select_block(block_matrix, block_grid_shape, valid_t, grid, sample_rate, was_breathing)
        if selected_block is not None and blocks is not None:
            valid_y = blocks[0][:, selected_block].astype(np.float64)
        else:
            valid_y = np.asarray([item.value for item in valid], dtype=np.float64)
        # When per-block data exists but no block shows coherent breathing, the
        # blocks have jointly ruled it out — a chance spectral fluke in their
        # whole-box mixture must not be reported as breathing.
        coherence_veto = blocks is not None and selected_block is None

        # Transient rejection: twitches and brushed limbs are far larger than
        # breathing but below the excessive-motion gate, and one spike poisons
        # the whole spectral window. Mask samples deviating too many MADs from
        # the window median; the surrounding clean breathing carries on.
        transients_rejected = 0
        if self.config.transient_mad_threshold > 0 and valid_y.size:
            median_value = float(np.median(valid_y))
            mad_std = 1.4826 * float(np.median(np.abs(valid_y - median_value)))
            limit = self.config.transient_mad_threshold * max(mad_std, 1e-6)
            keep = np.abs(valid_y - median_value) <= limit
            transients_rejected = int(valid_y.size - np.count_nonzero(keep))
            if transients_rejected:
                valid_t = valid_t[keep]
                valid_y = valid_y[keep]
                valid_fraction = valid_y.size / len(observations)
                if valid_y.size < 8:
                    return RespirationEstimate(
                        data_completeness=valid_fraction,
                        reason="too_few_valid_motion_samples",
                        **base,
                    )

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

        # Split-half stability: real breathing holds one rate; a noise fluke
        # peaks at a different random frequency in each independent half.
        half = filtered.size // 2
        rhythm_stable = True
        if half >= 16:
            window = np.hanning(half)
            half_freqs = np.fft.rfftfreq(half, d=1.0 / sample_rate)
            half_band = (half_freqs >= low_hz) & (half_freqs <= high_hz)
            if np.any(half_band):
                peaks = []
                for segment in (filtered[:half], filtered[-half:]):
                    spectrum = np.abs(np.fft.rfft(segment * window))
                    peaks.append(float(half_freqs[np.flatnonzero(half_band)[np.argmax(spectrum[half_band])]]))
                tolerance = max(0.25 * max(peaks), 1.5 * (half_freqs[1] - half_freqs[0]))
                rhythm_stable = abs(peaks[0] - peaks[1]) <= tolerance
                if not rhythm_stable and was_breathing and self._last_peak_hz:
                    # A locked rate drifting (REM dynamics) is stable rhythm:
                    # the halves disagree, but the peak tracks the known rate.
                    rhythm_stable = abs(peak_hz - self._last_peak_hz) <= 0.15 * self._last_peak_hz

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
        if coherence_veto:
            signal_observable = breathing_signal = False
            reason = "no_coherent_breathing_region"
        elif not rhythm_stable:
            signal_observable = breathing_signal = False
            reason = "rhythm_not_stable"
        else:
            signal_observable, breathing_signal = self._apply_gates(rms, snr_db, confidence, was_breathing)
            reason = "breathing_signal" if breathing_signal else (
                "signal_snr_too_low" if not signal_observable else "periodicity_confidence_low"
            )
        if breathing_signal:
            self._last_peak_hz = peak_hz
            self._last_breathing_at = now
            self._warm_deadline = None  # genuinely locked; warm window done

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
            selected_block=selected_block,
            waveform_t=waveform_t,
            waveform_y=waveform_y,
            **base,
        )
