from __future__ import annotations

from enum import Enum
from typing import Optional

from traffic_control.models import ControllerMode, ControllerState, PhaseDecision


class FeedbackStatus(str, Enum):
    """Result of checking switch execution feedback."""

    IDLE = "idle"  # no switch awaiting confirmation
    PENDING = "pending"  # switch issued, controller still transitioning
    CONFIRMED = "confirmed"  # controller reached the target phase
    FAILURE = "failure"  # timeout / controller fault / unexpected phase


class FeedbackMonitor:
    """Track switch execution feedback (design §6 state E).

    A switch is complete only when the controller reports the target phase as
    its current phase. A timeout, a controller fault, or the controller
    landing on an unexpected phase is an execution failure, which the
    decision loop must escalate to the fault path (fixed-time / flash yellow).
    """

    def __init__(self, confirm_timeout_s: float) -> None:
        if confirm_timeout_s <= 0:
            raise ValueError("confirm_timeout_s must be positive")
        self._confirm_timeout_s = confirm_timeout_s
        self._pending_target: Optional[str] = None
        self._from_phase: Optional[str] = None
        self._proposed_at: Optional[float] = None

    def observe(
        self, decision: PhaseDecision, state: ControllerState, now: float
    ) -> FeedbackStatus:
        """Record a switch proposal and check its confirmation status.

        A non-null ``decision.selected_phase_id`` (re)arms the monitor with
        the new target; otherwise the previous pending switch is re-checked
        against the actual controller state.
        """
        if decision.selected_phase_id is not None:
            self._pending_target = decision.selected_phase_id
            self._from_phase = state.current_phase_id
            self._proposed_at = now

        if self._pending_target is None:
            return FeedbackStatus.IDLE

        if state.mode == ControllerMode.FAULT:
            self._clear()
            return FeedbackStatus.FAILURE

        if state.current_phase_id == self._pending_target:
            self._clear()
            return FeedbackStatus.CONFIRMED

        transitioning = state.current_phase_id == self._from_phase or (
            state.mode in (ControllerMode.CLEARANCE, ControllerMode.ALL_RED)
        )
        if not transitioning:
            # The controller ended up on a phase we did not request.
            self._clear()
            return FeedbackStatus.FAILURE

        if now - self._proposed_at > self._confirm_timeout_s:
            self._clear()
            return FeedbackStatus.FAILURE

        return FeedbackStatus.PENDING

    def _clear(self) -> None:
        self._pending_target = None
        self._from_phase = None
        self._proposed_at = None
