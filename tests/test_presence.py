from __future__ import annotations

from app.classifier import ConservativeClassifier, DetectorState
from app.config import SignalConfig
from app.estimator import RespirationEstimate
from app.presence import PresenceState, PresenceTracker


def make_tracker(**kwargs) -> PresenceTracker:
    defaults = dict(minimum_disturbance=1.0, quiet_after=2.0, scan_cooldown=0.0)
    defaults.update(kwargs)
    return PresenceTracker(**defaults)


def pickup(tracker: PresenceTracker, start: float, duration: float = 5.0) -> float:
    """Simulate a caregiver disturbance; returns the time after quiet settles."""
    t = start
    while t < start + duration:
        tracker.observe(t, excessive=True)
        t += 0.2
    settle = t + 2.5
    tracker.observe(settle, excessive=False)
    return settle


def estimate(*, breathing: bool, observable: bool = True) -> RespirationEstimate:
    return RespirationEstimate(technical_valid=True, signal_observable=observable, breathing_signal=breathing, reason="test")


def test_breathing_confirms_present() -> None:
    tracker = make_tracker()
    assert tracker.state == PresenceState.UNKNOWN
    assert tracker.update(1.0, breathing=True) == PresenceState.PRESENT


def test_signal_loss_without_disturbance_stays_present() -> None:
    """Apnea does not look like a pickup: no disturbance means no absence."""
    tracker = make_tracker()
    tracker.update(1.0, breathing=True)
    for t in range(2, 120):
        assert tracker.update(float(t), breathing=False) == PresenceState.PRESENT
    assert not tracker.wants_scan(120.0)


def test_pickup_then_failed_scans_confirms_absent_and_return_recovers() -> None:
    tracker = make_tracker()
    tracker.update(1.0, breathing=True)
    settle = pickup(tracker, 10.0)
    assert tracker.update(settle, breathing=False) == PresenceState.CHECKING

    assert tracker.wants_scan(settle)
    tracker.on_scan_started()
    tracker.on_scan_completed(False, settle + 30)
    assert tracker.state == PresenceState.CHECKING  # one miss is not enough
    assert tracker.wants_scan(settle + 31)
    tracker.on_scan_started()
    tracker.on_scan_completed(False, settle + 61)
    assert tracker.state == PresenceState.ABSENT

    # Baby returns: disturbance, then the scan finds breathing again.
    settle2 = pickup(tracker, settle + 100)
    assert tracker.update(settle2, breathing=False) == PresenceState.CHECKING
    tracker.on_scan_started()
    tracker.on_scan_completed(True, settle2 + 30)
    assert tracker.state == PresenceState.PRESENT


def test_inconclusive_scan_does_not_count_toward_absence() -> None:
    tracker = make_tracker()
    tracker.update(1.0, breathing=True)
    settle = pickup(tracker, 10.0)
    tracker.update(settle, breathing=False)
    tracker.on_scan_started()
    tracker.on_scan_completed(None, settle + 30)  # motion during scan
    assert tracker.state == PresenceState.CHECKING
    assert tracker.wants_scan(settle + 31)


def test_short_twitch_is_not_a_disturbance() -> None:
    tracker = make_tracker()
    tracker.update(1.0, breathing=True)
    tracker.observe(2.0, excessive=True)  # single excessive frame
    tracker.observe(2.2, excessive=True)
    tracker.observe(5.0, excessive=False)
    assert tracker.update(5.0, breathing=False) == PresenceState.PRESENT


def test_disabled_tracker_always_present() -> None:
    tracker = PresenceTracker(enabled=False)
    tracker.observe(1.0, excessive=True)
    assert tracker.update(2.0, breathing=False) == PresenceState.PRESENT
    assert not tracker.wants_scan(3.0)


def test_classifier_gates_alarm_on_presence() -> None:
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=3)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)  # calibrated

    # Presence being verified: alarm state is withheld even past the timeout.
    classifier.update(estimate(breathing=False), 10, PresenceState.CHECKING)
    pending = classifier.update(estimate(breathing=False), 14, PresenceState.CHECKING)
    assert pending.state == DetectorState.MEASUREMENT_INVALID
    assert pending.reason == "signal_missing_presence_unverified"

    # Present: the alarm fires as before.
    absent_signal = classifier.update(estimate(breathing=False), 20, PresenceState.PRESENT)
    assert absent_signal.state == DetectorState.NO_BREATHING_SIGNAL

    # Confirmed empty crib: dedicated state, calibration dropped.
    empty = classifier.update(estimate(breathing=False), 30, PresenceState.ABSENT)
    assert empty.state == DetectorState.CRIB_EMPTY
    assert empty.measurement_valid is False
    assert empty.breathing_detected is None
    assert empty.calibrated is False
