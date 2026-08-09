from __future__ import annotations

from app.classifier import ConservativeClassifier, DetectorState
from app.config import SignalConfig
from app.estimator import RespirationEstimate


def estimate(*, breathing: bool, observable: bool = True) -> RespirationEstimate:
    return RespirationEstimate(technical_valid=True, signal_observable=observable, breathing_signal=breathing, reason="test")


def test_no_signal_requires_calibration_quality_and_timeout() -> None:
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=3, measurement_invalid_timeout=2)
    classifier = ConservativeClassifier(config)
    assert classifier.update(estimate(breathing=True), 0).state == DetectorState.BREATHING
    assert classifier.update(estimate(breathing=True), 2.1).calibrated
    pending = classifier.update(estimate(breathing=False), 3)
    assert pending.state == DetectorState.MEASUREMENT_INVALID
    assert pending.breathing_detected is None
    absent = classifier.update(estimate(breathing=False), 6.1)
    assert absent.state == DetectorState.NO_BREATHING_SIGNAL
    assert absent.measurement_valid
    assert absent.breathing_detected is False


def test_low_snr_is_invalid_not_no_breathing() -> None:
    config = SignalConfig(baseline_required_duration=0, no_breath_timeout=0)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    result = classifier.update(estimate(breathing=False, observable=False), 10)
    assert result.state == DetectorState.MEASUREMENT_INVALID
    assert result.breathing_detected is None
