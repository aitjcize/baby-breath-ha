from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.config import SignalConfig
from app.estimator import RespirationEstimate
from app.presence import PresenceState


class DetectorState(str, Enum):
    BREATHING = "BREATHING"
    NO_BREATHING_SIGNAL = "NO_BREATHING_SIGNAL"
    MEASUREMENT_INVALID = "MEASUREMENT_INVALID"
    CRIB_EMPTY = "CRIB_EMPTY"


@dataclass(frozen=True)
class Classification:
    state: DetectorState
    measurement_valid: bool
    breathing_detected: bool | None
    reason: str
    calibrated: bool


class ConservativeClassifier:
    """State machine that never interprets unobservable video as absent breathing."""

    def __init__(self, config: SignalConfig) -> None:
        self.config = config
        self._breathing_since: float | None = None
        self._no_signal_since: float | None = None
        self._invalid_since: float | None = None
        self._calibrated = False
        self._held: Classification | None = None
        self._held_at: float | None = None

    def reset(self) -> None:
        self._breathing_since = None
        self._no_signal_since = None
        self._invalid_since = None
        self._calibrated = False
        self._held = None
        self._held_at = None

    def update(
        self,
        estimate: RespirationEstimate,
        now: float,
        presence: PresenceState = PresenceState.PRESENT,
    ) -> Classification:
        """Classify, then smooth reporting through brief interruptions.

        The hold is an overlay on the REPORTED state only: every internal
        timer (no-breathing countdown, calibration decay) runs from the true
        moment of loss, so NO_BREATHING_SIGNAL fires at the same absolute time
        with or without the hold, and alarms punch through it immediately.
        """
        raw = self._classify(estimate, now, presence)
        hold = self.config.detection_hold_seconds
        if raw.state == DetectorState.BREATHING:
            self._held = raw
            self._held_at = now
            return raw
        if raw.state in (DetectorState.NO_BREATHING_SIGNAL, DetectorState.CRIB_EMPTY):
            self._held = None
            self._held_at = None
            return raw
        if (
            hold > 0
            and self._held is not None
            and self._held_at is not None
            and now - self._held_at <= hold
        ):
            return Classification(
                DetectorState.BREATHING,
                True,
                True,
                f"holding_through_interruption:{raw.reason}",
                self._held.calibrated,
            )
        return raw

    def _classify(
        self,
        estimate: RespirationEstimate,
        now: float,
        presence: PresenceState = PresenceState.PRESENT,
    ) -> Classification:
        if presence == PresenceState.ABSENT:
            # Confirmed empty crib: nothing to measure, and the next occupancy
            # must recalibrate from scratch.
            self._breathing_since = None
            self._no_signal_since = None
            self._invalid_since = None
            self._calibrated = False
            return Classification(DetectorState.CRIB_EMPTY, False, None, "crib_empty", False)

        quality_valid = estimate.technical_valid and estimate.signal_observable
        if not quality_valid:
            self._breathing_since = None
            self._no_signal_since = None
            if self._invalid_since is None:
                self._invalid_since = now
            if now - self._invalid_since >= self.config.measurement_invalid_timeout:
                self._calibrated = False
            return Classification(
                DetectorState.MEASUREMENT_INVALID,
                False,
                None,
                estimate.reason,
                self._calibrated,
            )

        self._invalid_since = None
        if estimate.breathing_signal:
            self._no_signal_since = None
            if self._breathing_since is None:
                self._breathing_since = now
            if now - self._breathing_since >= self.config.baseline_required_duration:
                self._calibrated = True
            return Classification(
                DetectorState.BREATHING,
                True,
                True,
                "periodic_respiration_signal_present",
                self._calibrated,
            )

        self._breathing_since = None
        if not self._calibrated:
            return Classification(
                DetectorState.MEASUREMENT_INVALID,
                False,
                None,
                "low_confidence_without_prior_calibration",
                False,
            )
        if self._no_signal_since is None:
            self._no_signal_since = now
        elapsed = now - self._no_signal_since
        if elapsed < self.config.no_breath_timeout:
            return Classification(
                DetectorState.MEASUREMENT_INVALID,
                False,
                None,
                f"respiration_signal_missing_pending_timeout:{elapsed:.1f}s",
                True,
            )
        if presence != PresenceState.PRESENT:
            # Signal gone right after a pickup-shaped disturbance: presence is
            # being verified (or is unknown), so withhold the alarm state.
            return Classification(
                DetectorState.MEASUREMENT_INVALID,
                False,
                None,
                "signal_missing_presence_unverified",
                True,
            )
        return Classification(
            DetectorState.NO_BREATHING_SIGNAL,
            True,
            False,
            "observable_periodic_respiration_signal_absent_after_timeout",
            True,
        )
