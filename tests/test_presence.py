from __future__ import annotations

from app.classifier import ConservativeClassifier, DetectorState
from app.config import SignalConfig
from app.estimator import RespirationEstimate
from app.presence import PresenceState, PresenceTracker


def make_tracker(**kwargs) -> PresenceTracker:
    defaults = dict(minimum_disturbance=1.0, quiet_after=2.0, scan_cooldown=0.0)
    defaults.update(kwargs)
    return PresenceTracker(**defaults)


def confirm_breathing(tracker: PresenceTracker, start: float, seconds: float = 10.0) -> float:
    t = start
    while t <= start + seconds:
        tracker.update(t, breathing=True)
        t += 1.0
    return t


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


def test_only_sustained_breathing_confirms_present() -> None:
    tracker = make_tracker()
    assert tracker.state == PresenceState.UNKNOWN
    # A blip must not mark the crib occupied (empty-bed false positives stick
    # forever, since an empty bed never produces a pickup disturbance).
    tracker.update(1.0, breathing=True)
    assert tracker.state == PresenceState.UNKNOWN
    tracker.update(2.0, breathing=False)
    tracker.update(3.0, breathing=True)
    assert tracker.state == PresenceState.UNKNOWN
    confirm_breathing(tracker, 4.0)
    assert tracker.state == PresenceState.PRESENT


def test_signal_loss_without_disturbance_stays_present() -> None:
    """Apnea does not look like a pickup: no disturbance means no absence."""
    tracker = make_tracker()
    confirm_breathing(tracker, 1.0)
    for t in range(20, 140):
        assert tracker.update(float(t), breathing=False) == PresenceState.PRESENT
    assert not tracker.wants_scan(140.0)


def test_pickup_then_failed_scans_confirms_absent_and_return_recovers() -> None:
    tracker = make_tracker()
    confirm_breathing(tracker, 1.0)
    settle = pickup(tracker, 20.0)
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
    confirm_breathing(tracker, 1.0)
    settle = pickup(tracker, 20.0)
    tracker.update(settle, breathing=False)
    tracker.on_scan_started()
    tracker.on_scan_completed(None, settle + 30)  # motion during scan
    assert tracker.state == PresenceState.CHECKING
    assert tracker.wants_scan(settle + 31)


def test_short_twitch_is_not_a_disturbance() -> None:
    tracker = make_tracker()
    confirm_breathing(tracker, 1.0)
    tracker.observe(20.0, excessive=True)  # single excessive frame
    tracker.observe(20.2, excessive=True)
    tracker.observe(23.0, excessive=False)
    assert tracker.update(23.0, breathing=False) == PresenceState.PRESENT


def test_disabled_tracker_always_present() -> None:
    tracker = PresenceTracker(enabled=False)
    tracker.observe(1.0, excessive=True)
    assert tracker.update(2.0, breathing=False) == PresenceState.PRESENT
    assert not tracker.wants_scan(3.0)


def test_detection_hold_smooths_brief_interruptions() -> None:
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=30, detection_hold_seconds=10)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)  # calibrated

    # A brief quality dropout is held as BREATHING (with the hold reason).
    held = classifier.update(RespirationEstimate(reason="stream_reconnecting"), 5)
    assert held.state == DetectorState.BREATHING
    assert held.breathing_detected is True
    assert held.reason.startswith("holding_through_interruption")

    # Recovery within the hold: seamless.
    assert classifier.update(estimate(breathing=True), 8).state == DetectorState.BREATHING

    # A dropout longer than the hold surfaces honestly.
    classifier.update(RespirationEstimate(reason="stream_reconnecting"), 20)
    late = classifier.update(RespirationEstimate(reason="stream_reconnecting"), 31)
    assert late.state == DetectorState.MEASUREMENT_INVALID


def test_movement_refreshes_the_hold_beyond_its_duration() -> None:
    """Motion-caused measurement failure is vitality evidence: the reported
    state rides through a long stir, while quiet losses still expire."""
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=300, detection_hold_seconds=15)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)

    moving = RespirationEstimate(reason="incomplete_motion_data", excessive_motion=True, motion_stability=0.4)
    for t in range(5, 65, 5):  # 60 s of stirring, far beyond the 15 s hold
        held = classifier.update(moving, float(t))
        assert held.state == DetectorState.BREATHING, f"dropped at t={t}"
        assert held.reason.startswith("holding_through_movement")

    # Movement ends into a quiet loss: the base hold takes over, then the
    # recovery hold covers one analysis window (24 s + margin) past the last
    # disturbance, then reporting goes honest.
    quiet = RespirationEstimate(reason="stream_reconnecting")
    assert classifier.update(quiet, 70.0).state == DetectorState.BREATHING  # base hold
    recovering = classifier.update(quiet, 85.0)
    assert recovering.state == DetectorState.BREATHING
    assert recovering.reason.startswith("holding_through_recovery")
    assert classifier.update(quiet, 91.0).state == DetectorState.MEASUREMENT_INVALID


def test_recovery_hold_bridges_post_movement_window_refill() -> None:
    """A stir corrupts the analysis window and the estimator needs up to a
    full window of clean samples afterwards. The reported state bridges that
    expected outage instead of blipping INVALID right before the re-lock."""
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=300, detection_hold_seconds=15)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)

    moving = RespirationEstimate(reason="excessive_motion", excessive_motion=True, motion_stability=0.4)
    classifier.update(moving, 5.0)
    # Window refill runs well past the 15 s base hold before re-certifying.
    refill = RespirationEstimate(reason="incomplete_motion_data")
    for t in (10.0, 15.0, 21.0, 27.0):
        held = classifier.update(refill, t)
        assert held.state == DetectorState.BREATHING, f"blipped at t={t}"
    hunting = RespirationEstimate(reason="rhythm_not_stable")
    held = classifier.update(hunting, 30.0)
    assert held.state == DetectorState.BREATHING
    assert held.reason.startswith("holding_through_recovery")
    assert classifier.update(estimate(breathing=True), 31.0).state == DetectorState.BREATHING


def test_certification_failures_do_not_extend_the_hold() -> None:
    """Only missing data arms the recovery hold: a scene that has data but
    keeps failing certification surfaces honestly once the base hold ends."""
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=300, detection_hold_seconds=10)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)
    hunting = RespirationEstimate(reason="no_coherent_breathing_region")
    assert classifier.update(hunting, 8.0).state == DetectorState.BREATHING
    assert classifier.update(hunting, 13.0).state == DetectorState.MEASUREMENT_INVALID


def test_movement_hold_is_bounded() -> None:
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=1000, detection_hold_seconds=15)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)
    moving = RespirationEstimate(reason="excessive_motion", excessive_motion=True, motion_stability=0.2)
    t, state = 5.0, None
    while t < 400:
        state = classifier.update(moving, t).state
        t += 5.0
    # The BREATHING overlay is capped at 5 min; with movement still present
    # the honest state is the benign MOVING, not a measurement problem.
    assert state == DetectorState.MOVING


def test_unmeasurable_with_movement_reports_moving() -> None:
    """Cannot measure + visible movement = benign MOVING; the same failure
    with a still region = MEASUREMENT_INVALID (the alert-worthy kind)."""
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=1000, detection_hold_seconds=0)
    classifier = ConservativeClassifier(config)
    moving = RespirationEstimate(reason="excessive_motion", excessive_motion=True, motion_stability=0.3)
    active = classifier.update(moving, 1.0)
    assert active.state == DetectorState.MOVING  # no prior calibration needed
    assert active.measurement_valid is False
    assert active.breathing_detected is None
    still = classifier.update(RespirationEstimate(reason="signal_snr_too_low"), 2.0)
    assert still.state == DetectorState.MEASUREMENT_INVALID


def test_hold_never_delays_the_alarm() -> None:
    """Internal timers run from the true loss: NO_BREATHING_SIGNAL fires at
    the same absolute time and punches through an active hold."""
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=12, detection_hold_seconds=60)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)  # calibrated

    # Signal missing from t=10; hold (60 s) far exceeds the 12 s timeout.
    assert classifier.update(estimate(breathing=False), 10).state == DetectorState.BREATHING  # held
    assert classifier.update(estimate(breathing=False), 20).state == DetectorState.BREATHING  # held
    fired = classifier.update(estimate(breathing=False), 22.5)  # 12.5 s after loss
    assert fired.state == DetectorState.NO_BREATHING_SIGNAL
    assert fired.breathing_detected is False

    # Uncalibrated locks are held too (post-drop aftershock blips): a raw
    # lock already passed contrast/coherence/stability, so it is trustworthy.
    fresh = ConservativeClassifier(config)
    fresh.update(estimate(breathing=True), 0)
    dropped = fresh.update(RespirationEstimate(reason="noise"), 1)
    assert dropped.state == DetectorState.BREATHING
    assert dropped.reason.startswith("holding_through_interruption")


def test_crib_empty_punches_through_hold() -> None:
    config = SignalConfig(baseline_required_duration=2, detection_hold_seconds=60)
    classifier = ConservativeClassifier(config)
    classifier.update(estimate(breathing=True), 0)
    classifier.update(estimate(breathing=True), 2.1)
    empty = classifier.update(estimate(breathing=False), 5, PresenceState.ABSENT)
    assert empty.state == DetectorState.CRIB_EMPTY


def test_classifier_gates_alarm_on_presence() -> None:
    config = SignalConfig(baseline_required_duration=2, no_breath_timeout=3, detection_hold_seconds=0)
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
