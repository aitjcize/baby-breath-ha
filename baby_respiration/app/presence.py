"""Event-driven crib presence tracking — no ML, no appearance models.

The core safety property: a baby cannot enter or leave the crib without a
caregiver, i.e. without a large sustained motion event ("disturbance").
Apnea does not look like a pickup, so a sudden loss of breathing WITHOUT a
preceding disturbance can never be classified as absence — the tracker stays
PRESENT and the no-breathing alert path keeps working.

Transitions:

- Confirmed breathing signal        → PRESENT (strongest evidence, any time)
- Disturbance ends, signal missing  → CHECKING: the service runs full-frame
  breathing scans; found anywhere → PRESENT, two clean not-found scans in a
  quiet scene → ABSENT
- ABSENT + new disturbance          → CHECKING again (baby may be back)
- CHECKING stuck (stream down etc.) → UNKNOWN after a timeout

With ``enabled=False`` the tracker always reports PRESENT, which makes every
consumer behave exactly as before the feature existed.
"""

from __future__ import annotations

import logging
from enum import Enum

LOGGER = logging.getLogger(__name__)


class PresenceState(str, Enum):
    UNKNOWN = "UNKNOWN"
    PRESENT = "PRESENT"
    CHECKING = "CHECKING"
    ABSENT = "ABSENT"


class PresenceTracker:
    def __init__(
        self,
        enabled: bool = True,
        minimum_disturbance: float = 1.0,
        quiet_after: float = 3.0,
        required_failed_scans: int = 2,
        scan_cooldown: float = 45.0,
        checking_timeout: float = 600.0,
        breathing_confirm_duration: float = 8.0,
    ) -> None:
        self.enabled = enabled
        self.minimum_disturbance = minimum_disturbance
        self.quiet_after = quiet_after
        self.required_failed_scans = required_failed_scans
        self.scan_cooldown = scan_cooldown
        self.checking_timeout = checking_timeout
        self.breathing_confirm_duration = breathing_confirm_duration
        self._state = PresenceState.UNKNOWN
        self._reason = "startup"
        self._disturbance_start: float | None = None
        self._last_excessive: float | None = None
        self._failed_scans = 0
        self._scan_wanted = False
        self._last_scan_finished: float | None = None
        self._checking_since: float | None = None
        self._breathing_since: float | None = None

    # ------------------------------------------------------------------ input

    def observe(self, now: float, excessive: bool) -> None:
        """Feed per-frame motion; detects pickup-shaped disturbances."""
        if not self.enabled:
            return
        if excessive:
            if self._disturbance_start is None:
                self._disturbance_start = now
            self._last_excessive = now
            return
        if self._disturbance_start is None or self._last_excessive is None:
            return
        if now - self._last_excessive >= self.quiet_after:
            duration = self._last_excessive - self._disturbance_start
            self._disturbance_start = None
            self._last_excessive = None
            if duration >= self.minimum_disturbance:
                self._on_disturbance_ended(now, duration)

    def _on_disturbance_ended(self, now: float, duration: float) -> None:
        if self._state == PresenceState.UNKNOWN:
            # Nothing established yet (fresh start / mid-onboarding): breathing
            # confirmation will set PRESENT on its own; don't burn scans.
            return
        LOGGER.info("disturbance of %.1fs ended; verifying crib occupancy", duration)
        self._set(PresenceState.CHECKING, "verifying_after_disturbance")
        self._checking_since = now
        self._failed_scans = 0
        self._scan_wanted = True

    def update(self, now: float, breathing: bool) -> PresenceState:
        """Advance with the once-per-second estimate result.

        A single breathing blip must not mark the crib occupied: on an empty
        bed there is no pickup disturbance to ever trigger re-verification, so
        a false PRESENT would stick forever. Only sustained breathing counts.
        """
        if not self.enabled:
            return PresenceState.PRESENT
        if breathing:
            if self._breathing_since is None:
                self._breathing_since = now
            if now - self._breathing_since >= self.breathing_confirm_duration:
                if self._state != PresenceState.PRESENT:
                    self._set(PresenceState.PRESENT, "sustained_breathing_confirmed")
                self._scan_wanted = False
                self._checking_since = None
                self._failed_scans = 0
        else:
            self._breathing_since = None
        if (
            self._state == PresenceState.CHECKING
            and self._checking_since is not None
            and now - self._checking_since > self.checking_timeout
        ):
            self._set(PresenceState.UNKNOWN, "verification_timed_out")
            self._scan_wanted = False
            self._checking_since = None
        return self._state

    # ------------------------------------------------------------- scan hooks

    def wants_scan(self, now: float) -> bool:
        if not self.enabled or not self._scan_wanted:
            return False
        if self._last_scan_finished is not None and now - self._last_scan_finished < self.scan_cooldown:
            return False
        return True

    def on_scan_started(self) -> None:
        self._scan_wanted = False

    def on_scan_completed(self, found: bool | None, now: float) -> None:
        """found=None means the scan was inconclusive (e.g. motion during it)."""
        if not self.enabled:
            return
        self._last_scan_finished = now
        if self._state != PresenceState.CHECKING:
            return
        if found is True:
            self._set(PresenceState.PRESENT, "breathing_found_in_scan")
            self._checking_since = None
        elif found is False:
            self._failed_scans += 1
            if self._failed_scans >= self.required_failed_scans:
                self._set(PresenceState.ABSENT, "no_breathing_found_after_disturbance")
                self._checking_since = None
            else:
                self._scan_wanted = True
        else:  # inconclusive: retry without counting toward absence
            self._scan_wanted = True

    # ----------------------------------------------------------------- output

    @property
    def state(self) -> PresenceState:
        return self._state if self.enabled else PresenceState.PRESENT

    @property
    def reason(self) -> str:
        return self._reason if self.enabled else "presence_detection_disabled"

    def _set(self, state: PresenceState, reason: str) -> None:
        if state != self._state:
            LOGGER.info("presence: %s -> %s (%s)", self._state.value, state.value, reason)
        self._state = state
        self._reason = reason
