from __future__ import annotations

import numpy as np

from app.config import CameraConfig, SignalConfig
from app.estimator import RespirationEstimator
from app.motion import MotionObservation


def test_recovers_42_bpm_from_irregular_noisy_samples() -> None:
    rng = np.random.default_rng(20260808)
    camera = CameraConfig(processing_fps=8.0)
    config = SignalConfig(
        analysis_window_duration=26.0,
        minimum_valid_window_duration=15.0,
        minimum_signal_rms=0.003,
        minimum_confidence=50.0,
    )
    estimator = RespirationEstimator(camera, config)
    timestamp = 1000.0
    for _ in range(30 * 8):
        timestamp += max(0.05, 1.0 / 8.0 + rng.normal(0, 0.012))
        elapsed = timestamp - 1000.0
        motion = 0.035 * np.sin(2 * np.pi * (42.0 / 60.0) * elapsed) + rng.normal(0, 0.006)
        estimator.add(MotionObservation(timestamp, float(motion), True, False, 0.02, 0.04, 30.0, 20.0, 100.0, 1.0, "ok"))

    result = estimator.estimate(timestamp)
    assert result.technical_valid
    assert result.signal_observable
    assert result.breathing_signal
    assert result.bpm is not None
    assert abs(result.bpm - 42.0) < 3.0
    assert result.confidence >= 50.0


def test_twitch_transient_does_not_poison_the_window() -> None:
    """A 2 s limb twitch at 20x breathing amplitude must not break detection."""
    rng = np.random.default_rng(7)
    camera = CameraConfig(processing_fps=5.0)
    config = SignalConfig(minimum_confidence=50.0)
    estimator = RespirationEstimator(camera, config)
    timestamp = 0.0
    for index in range(30 * 5):
        timestamp = index / 5.0
        motion = 0.01 * np.sin(2 * np.pi * (42.0 / 60.0) * timestamp) + rng.normal(0, 0.0015)
        if 20.0 <= timestamp < 22.0:  # twitch burst mid-window
            motion += 0.2 * rng.choice([-1.0, 1.0])
        estimator.add(MotionObservation(timestamp, float(motion), True, False, 0.02, 0.04, 30.0, 20.0, 100.0, 1.0, "ok"))

    result = estimator.estimate(timestamp)
    assert result.breathing_signal, result.reason
    assert result.bpm is not None and abs(result.bpm - 42.0) < 4.0

    # Regression guard: with rejection disabled the same data must fail,
    # proving the masking is what saves the window.
    unprotected = RespirationEstimator(camera, SignalConfig(minimum_confidence=50.0, transient_mad_threshold=0.0))
    unprotected._history = estimator._history
    assert not unprotected.estimate(timestamp).breathing_signal


def test_hysteresis_holds_marginal_signal() -> None:
    camera = CameraConfig(processing_fps=5.0)
    config = SignalConfig(minimum_snr_db=3.0, minimum_confidence=55.0, minimum_signal_rms=0.001)
    estimator = RespirationEstimator(camera, config)

    # Locked: dips within the hysteresis margin stay on.
    assert estimator._apply_gates(rms=0.002, snr_db=5.0, confidence=60.0, was_breathing=False) == (True, True)
    assert estimator._apply_gates(rms=0.0008, snr_db=2.0, confidence=49.0, was_breathing=True) == (True, True)
    # The same marginal values from cold do not lock on.
    assert estimator._apply_gates(rms=0.0008, snr_db=2.0, confidence=49.0, was_breathing=False) == (False, False)
    # Dips beyond the margin release even a held lock.
    assert estimator._apply_gates(rms=0.002, snr_db=1.0, confidence=49.0, was_breathing=True) == (False, False)


def test_amplitude_floor_holds_regardless_of_spectral_confidence() -> None:
    """24 s noise spectra fluke concentrated peaks; spectral evidence alone
    must not waive the noise floor (empty-bed lesson)."""
    camera = CameraConfig(processing_fps=5.0)
    config = SignalConfig(minimum_snr_db=3.0, minimum_confidence=55.0, minimum_signal_rms=0.001)
    estimator = RespirationEstimator(camera, config)

    assert estimator._apply_gates(rms=0.00058, snr_db=9.3, confidence=59.0, was_breathing=False) == (False, False)
    assert estimator._apply_gates(rms=0.002, snr_db=9.3, confidence=59.0, was_breathing=False) == (True, True)

def make_block_observation(timestamp: float, blocks: list[float], grid: tuple[int, int] = (3, 4)) -> MotionObservation:
    return MotionObservation(
        timestamp, float(np.median(blocks)), True, False, 0.02, 0.04, 30.0, 20.0, 100.0, 1.0, "ok",
        block_values=tuple(blocks), block_grid=grid,
    )


def test_block_selection_finds_breathing_in_large_box() -> None:
    """A big box mostly full of static bedding must still detect: the
    estimator measures from the block that carries the rhythm."""
    rng = np.random.default_rng(11)
    camera = CameraConfig(processing_fps=5.0)
    config = SignalConfig(minimum_confidence=50.0)
    estimator = RespirationEstimator(camera, config)
    active = 5  # the "chest": blocks 5 and 6 are adjacent in the 3x4 grid
    timestamp = 0.0
    for index in range(30 * 5):
        timestamp = index / 5.0
        blocks = list(rng.normal(0, 0.0004, size=12))  # static bedding noise
        breath = 0.012 * np.sin(2 * np.pi * (42.0 / 60.0) * timestamp)
        blocks[active] += breath + rng.normal(0, 0.002)
        blocks[active + 1] += 0.6 * breath + rng.normal(0, 0.002)
        estimator.add(make_block_observation(timestamp, blocks))

    result = estimator.estimate(timestamp)
    assert result.breathing_signal, result.reason
    assert result.selected_block == active
    assert result.bpm is not None and abs(result.bpm - 42.0) < 4.0

    # The whole-box median of the same data is diluted into failure,
    # proving block selection is what saves the large box.
    plain = RespirationEstimator(camera, SignalConfig(minimum_confidence=50.0))
    for item in estimator._history:
        plain.add(MotionObservation(
            item.timestamp, item.value, item.valid, item.excessive_motion, 0.02, 0.04,
            30.0, 20.0, 100.0, 1.0, "ok", block_values=None,
        ))
    assert not plain.estimate(timestamp).breathing_signal


def test_block_selection_follows_moving_baby() -> None:
    rng = np.random.default_rng(13)
    camera = CameraConfig(processing_fps=5.0)
    config = SignalConfig(minimum_confidence=50.0)
    estimator = RespirationEstimator(camera, config)

    def feed(start: float, seconds: float, active: int) -> float:
        timestamp = start
        for index in range(int(seconds * 5)):
            timestamp = start + index / 5.0
            blocks = list(rng.normal(0, 0.0004, size=12))
            breath = 0.012 * np.sin(2 * np.pi * (40.0 / 60.0) * timestamp)
            blocks[active] += breath + rng.normal(0, 0.002)
            blocks[active + 1] += 0.6 * breath + rng.normal(0, 0.002)
            estimator.add(make_block_observation(timestamp, blocks))
        return timestamp

    end = feed(0.0, 30.0, active=1)
    assert estimator.estimate(end).selected_block == 1
    # Baby moves: rhythm relocates to block 9; the next full window follows.
    end = feed(end + 0.2, 30.0, active=9)
    result = estimator.estimate(end)
    assert result.selected_block == 9
    assert result.breathing_signal, result.reason


def test_isolated_flutter_block_is_rejected() -> None:
    """Environmental flutter in one lone block (no coherent neighbour) must
    not become a breathing detection — the empty-bed false-positive case."""
    rng = np.random.default_rng(17)
    camera = CameraConfig(processing_fps=5.0)
    estimator = RespirationEstimator(camera, SignalConfig(minimum_confidence=50.0))
    timestamp = 0.0
    for index in range(30 * 5):
        timestamp = index / 5.0
        blocks = list(rng.normal(0, 0.0004, size=12))
        # a fluttering blanket corner: rhythmic, but spatially isolated
        blocks[5] += 0.008 * np.sin(2 * np.pi * (44.0 / 60.0) * timestamp) + rng.normal(0, 0.001)
        estimator.add(make_block_observation(timestamp, blocks))
    result = estimator.estimate(timestamp)
    assert result.selected_block is None
    assert not result.breathing_signal


def test_pure_noise_blocks_select_nothing() -> None:
    rng = np.random.default_rng(19)
    camera = CameraConfig(processing_fps=5.0)
    estimator = RespirationEstimator(camera, SignalConfig(minimum_confidence=50.0))
    timestamp = 0.0
    for index in range(30 * 5):
        timestamp = index / 5.0
        estimator.add(make_block_observation(timestamp, list(rng.normal(0, 0.003, size=12))))
    result = estimator.estimate(timestamp)
    assert result.selected_block is None
    assert not result.breathing_signal


def test_spatially_correlated_camera_noise_never_detects() -> None:
    """Optical-flow noise correlates neighbouring blocks by construction
    (overlapping estimation windows), so neighbour correlation alone cannot
    reject it — reproduces the empty-bed false lock. Localization contrast
    and split-half rhythm stability must hold the line across seeds."""
    camera = CameraConfig(processing_fps=5.0)
    for seed in range(25):
        rng = np.random.default_rng(seed)
        estimator = RespirationEstimator(camera, SignalConfig(minimum_confidence=50.0))
        timestamp = 0.0
        for index in range(35 * 5):
            timestamp = index / 5.0
            iid = rng.normal(0, 0.003, size=(3, 4))
            # neighbour-correlated field: each block mixes with its row/col
            # neighbours, mimicking shared flow-window support
            smooth = iid.copy()
            smooth[:, 1:] += 0.7 * iid[:, :-1]
            smooth[1:, :] += 0.7 * iid[:-1, :]
            estimator.add(make_block_observation(timestamp, list(smooth.ravel())))
            if index >= 25 * 5:
                result = estimator.estimate(timestamp)
                assert not result.breathing_signal, (
                    f"seed {seed} false-detected at t={timestamp}: {result.reason} "
                    f"conf={result.confidence} snr={result.snr_db}"
                )


def test_invalid_samples_fail_invalid() -> None:
    camera = CameraConfig(processing_fps=5.0)
    estimator = RespirationEstimator(camera, SignalConfig())
    for index in range(130):
        timestamp = index / 5
        valid = index % 3 == 0
        estimator.add(MotionObservation(timestamp, 0.01 if valid else None, valid, not valid, 2.0, 2.0, 10.0, 10.0, 100.0, 1.0, "test"))
    result = estimator.estimate(129 / 5)
    assert not result.technical_valid
    assert not result.breathing_signal

