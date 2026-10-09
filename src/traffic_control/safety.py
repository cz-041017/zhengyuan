from __future__ import annotations

from dataclasses import dataclass

from traffic_control.models import (
    ControllerMode,
    ControllerState,
    SignalPhase,
)


@dataclass(frozen=True)
class SafetyCheck:
    """Result of checking whether a phase transition is allowed."""

    allowed: bool
    reasons: tuple[str, ...] = ()


class SafetyGuard:
    """Apply basic logical safety constraints to phase selection.

    This is a simulation-level guard, not a certified traffic-signal
    controller or a substitute for jurisdiction-specific requirements.
    """

    def __init__(self, phases: dict[str, SignalPhase]) -> None:
        if not phases:
            raise ValueError("at least one signal phase must be configured")

        for phase_id, phase in phases.items():
            if phase_id != phase.phase_id:
                raise ValueError(
                    f"phase dictionary key {phase_id!r} does not match "
                    f"phase.phase_id {phase.phase_id!r}"
                )

        self.phases = dict(phases)

    def check_candidate(
        self,
        candidate_phase_id: str,
        state: ControllerState,
        now: float,
        conflicting_phase_ids: set[str] | None = None,
    ) -> SafetyCheck:
        """Check basic conditions for selecting a candidate phase."""
        reasons: list[str] = []

        if now < 0:
            reasons.append("invalid_time")

        candidate = self.phases.get(candidate_phase_id)
        if candidate is None:
            return SafetyCheck(
                allowed=False,
                reasons=("unknown_candidate_phase",),
            )

        if state.mode == ControllerMode.FAULT:
            reasons.append("controller_in_fault_mode")

        if state.mode == ControllerMode.CLEARANCE:
            reasons.append("clearance_in_progress")

        if state.mode == ControllerMode.ALL_RED:
            reasons.append("all_red_in_progress")

        if state.current_phase_id is not None:
            current = self.phases.get(state.current_phase_id)
            if current is None:
                reasons.append("unknown_current_phase")
            elif candidate_phase_id != state.current_phase_id:
                elapsed = now - state.phase_started_at

                if elapsed < 0:
                    reasons.append("invalid_phase_start_time")
                elif elapsed < current.min_green_s:
                    reasons.append("minimum_green_not_met")

                conflicting_ids = conflicting_phase_ids or set()
                if candidate_phase_id in conflicting_ids:
                    reasons.append("candidate_conflicts_with_current_phase")

        return SafetyCheck(
            allowed=not reasons,
            reasons=tuple(reasons),
        )