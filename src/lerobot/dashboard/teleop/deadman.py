"""Deadman switch state machine.

Four states — server owns the ground truth; every ``action`` frame is
dropped unless :meth:`DeadmanStateMachine.accepts_action` returns ``True``.

::

                +---------+  press()         +-----------+
                |  IDLE   |----------------->|  ARMING   |
                +---------+                  +-----------+
                     ^                              |
                     | release()                    | tick() after
                     |                              | ARMING_DELAY_MS
                     |                              v
                +---------+  release()       +-----------+
                | LOCKOUT |<-----------------|  ENGAGED  |
                +---------+   fault()        +-----------+
                     |
                     | release()
                     v
                  IDLE

Key rules (enforced by unit tests):

* Entering ``ENGAGED`` requires a settle delay after ``press()`` so an
  accidental tap cannot drive the robot instantly.
* Heartbeat timeout while ``ENGAGED`` latches ``LOCKOUT`` — the only way
  out is ``release() → press()``, re-establishing operator presence.
* ``LOCKOUT`` is sticky under continued ``press()``; only ``release()``
  clears it. That forces a deliberate operator acknowledgement before the
  robot can move again.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ARMING_DELAY_MS: float = 200.0
HEARTBEAT_TIMEOUT_MS: float = 300.0


class DeadmanState(str, enum.Enum):
    IDLE = "idle"
    ARMING = "arming"
    ENGAGED = "engaged"
    LOCKOUT = "lockout"


@dataclass
class DeadmanStateMachine:
    """Pure-Python deadman state machine; callers pass monotonic ms timestamps."""

    arming_delay_ms: float = ARMING_DELAY_MS
    heartbeat_timeout_ms: float = HEARTBEAT_TIMEOUT_MS

    state: DeadmanState = DeadmanState.IDLE
    _arming_started_at_ms: float | None = field(default=None, repr=False)
    _last_heartbeat_ms: float | None = field(default=None, repr=False)

    def press(self, now_ms: float) -> DeadmanState:
        """Operator presses the deadman button."""
        if self.state is DeadmanState.IDLE:
            self.state = DeadmanState.ARMING
            self._arming_started_at_ms = now_ms
            logger.debug("deadman: IDLE -> ARMING at %.1f ms", now_ms)
        # ARMING / ENGAGED: already pressed, no change.
        # LOCKOUT: stays latched until release() acknowledges.
        return self.tick(now_ms)

    def release(self, now_ms: float) -> DeadmanState:
        """Operator releases the deadman button (or disconnects)."""
        if self.state in {DeadmanState.ARMING, DeadmanState.ENGAGED, DeadmanState.LOCKOUT}:
            prev = self.state
            self.state = DeadmanState.IDLE
            self._arming_started_at_ms = None
            logger.debug("deadman: %s -> IDLE on release at %.1f ms", prev.value, now_ms)
        return self.state

    def heartbeat(self, now_ms: float) -> DeadmanState:
        """Client liveness beacon; refreshes the watchdog."""
        self._last_heartbeat_ms = now_ms
        return self.tick(now_ms)

    def fault(self, reason: str, now_ms: float) -> DeadmanState:
        """Hard fault: jump to LOCKOUT regardless of current state."""
        if self.state is not DeadmanState.LOCKOUT:
            logger.warning("deadman: %s -> LOCKOUT (%s)", self.state.value, reason)
        self.state = DeadmanState.LOCKOUT
        self._arming_started_at_ms = None
        return self.state

    def tick(self, now_ms: float) -> DeadmanState:
        """Advance derived transitions (arming-delay expiry, heartbeat timeout)."""
        if self.state is DeadmanState.ENGAGED and self._last_heartbeat_ms is not None:
            if now_ms - self._last_heartbeat_ms > self.heartbeat_timeout_ms:
                return self.fault("heartbeat_timeout", now_ms)
        if self.state is DeadmanState.ARMING and self._arming_started_at_ms is not None:
            if now_ms - self._arming_started_at_ms >= self.arming_delay_ms:
                self.state = DeadmanState.ENGAGED
                # Prime the heartbeat clock on transition so a short-lived
                # engagement can't be cleared by a stale watchdog.
                self._last_heartbeat_ms = now_ms
                logger.debug("deadman: ARMING -> ENGAGED at %.1f ms", now_ms)
        return self.state

    def accepts_action(self) -> bool:
        """Only accept action frames when fully engaged."""
        return self.state is DeadmanState.ENGAGED
