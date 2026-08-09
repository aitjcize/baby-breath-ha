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

