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

