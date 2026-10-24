from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from traffic_control.models import (
    ControllerMode,
    ControllerState,
    PhaseDecision,
    SignalPhase,
)
from traffic_control.scoring import Scorer


class MachineStage(str, Enum):
    """Decision-layer switch lifecycle stage."""

    GREEN = "green"
    COUNTDOWN = "countdown"


@dataclass(frozen=True)
class MachineConfig:
    """Switch-gating parameters for the decision state machine."""

    score_hysteresis: float = 0.1
    switch_debounce_s: float = 3.0
    countdown_duration_s: float = 15.0

    def __post_init__(self) -> None:
        if self.score_hysteresis < 0:
            raise ValueError("score_hysteresis must be non-negative")
        if self.switch_debounce_s < 0:
            raise ValueError("switch_debounce_s must be non-negative")
        if self.countdown_duration_s <= 0:
            raise ValueError("countdown_duration_s must be positive")


@dataclass(frozen=True)
class PhaseDemand:
    """Aggregated per-phase demand snapshot fed into the decision layer."""

    vehicle_count: int = 0
    oldest_wait_s: float = 0.0
    downstream_blocked: bool = False
    green_wave_active: bool = False

    def __post_init__(self) -> None:
        if self.vehicle_count < 0:
            raise ValueError("vehicle_count must be non-negative")
        if self.oldest_wait_s < 0:
            raise ValueError("oldest_wait_s must be non-negative")


class PhaseStateMachine:
    """Sequence phase switches per the decision algorithm (§5/§6).

    Responsibilities:
      * enforce minimum green as a hard floor and maximum green as a hard cap;
      * force-schedule a direction whose wait reaches the P0 deadline, where
        the deadline accounts for the current phase's remaining minimum green
        plus yellow/all-red clearance, bypassing both score and countdown;
      * report an infeasible decision on a P0–P1 conflict (a deadline
        direction whose downstream is congested);
      * run the 15 s countdown only for score-driven (optional) switches,
        with per-tick re-verification and cancellation.
    """

    def __init__(
        self,
        phases: Mapping[str, SignalPhase],
        scorer: Scorer,
        config: MachineConfig | None = None,
    ) -> None:
        if not phases:
            raise ValueError("at least one signal phase must be configured")
        for phase_id, phase in phases.items():
            if phase_id != phase.phase_id:
                raise ValueError(
                    f"phase dictionary key {phase_id!r} does not match "
                    f"phase.phase_id {phase.phase_id!r}"
                )
        self._phases = dict(phases)
        self._scorer = scorer
        self._config = config or MachineConfig()

        self._last_current_phase: str | None = None
        self._pending_phase_id: str | None = None
        self._countdown_started_at: float | None = None
        self._debounce_phase_id: str | None = None
        self._debounce_started_at: float | None = None

    @property
    def stage(self) -> MachineStage:
        return (
            MachineStage.COUNTDOWN
            if self._countdown_started_at is not None
            else MachineStage.GREEN
        )

    @property
    def pending_phase_id(self) -> str | None:
        return self._pending_phase_id

    def tick(
        self,
        state: ControllerState,
        now: float,
        demands: Mapping[str, PhaseDemand],
    ) -> PhaseDecision:
        if state.mode != ControllerMode.NORMAL:
            return PhaseDecision(None, "controller_not_normal", feasible=False)

        if (
            state.current_phase_id is None
            or state.current_phase_id not in self._phases
        ):
            return PhaseDecision(None, "unknown_current_phase", feasible=False)

        current = self._phases[state.current_phase_id]
        elapsed = now - state.phase_started_at
        if elapsed < 0:
            return PhaseDecision(None, "invalid_phase_start_time", feasible=False)

        if self._last_current_phase != state.current_phase_id:
            self._last_current_phase = state.current_phase_id
            self._clear_transient()

        scores = {
            phase_id: self._phase_score(phase_id, demands)
            for phase_id in self._phases
        }

        red_phases = [
            phase_id
            for phase_id in self._phases
            if phase_id != current.phase_id
        ]
        blocked = {
            phase_id
            for phase_id in red_phases
            if demands.get(phase_id, PhaseDemand()).downstream_blocked
        }

        # Worst-case time until a red direction can receive green: finish the
        # current phase's remaining minimum green, then yellow + all-red.
        remaining_min_green = max(0.0, current.min_green_s - elapsed)
        clearance = current.yellow_s + current.all_red_s
        time_to_green = remaining_min_green + clearance
        deadline = self._scorer.max_wait_target_s

        preempt_candidates = [
            phase_id
            for phase_id in red_phases
            if phase_id not in blocked
            and demands.get(phase_id, PhaseDemand()).oldest_wait_s
            >= deadline - time_to_green
        ]
        blocked_deadline = [
            phase_id
            for phase_id in red_phases
            if phase_id in blocked
            and demands.get(phase_id, PhaseDemand()).oldest_wait_s
            >= deadline - time_to_green
        ]

        if blocked_deadline:
            # P0 and P1 conflict: the starving direction cannot be served
            # without pushing vehicles into a congested downstream link.
            # Clear any in-progress countdown/debounce: the conflict changed
            # the feasible set, so a stale pending switch must not survive.
            self._clear_transient()
            return PhaseDecision(
                None,
                "p0_p1_conflict",
                scores=scores,
                constraints_applied=("spillback", "wait_deadline"),
                feasible=False,
            )

        preempt_target = None
        if preempt_candidates:
            preempt_target = max(
                preempt_candidates,
                key=lambda phase_id: demands.get(
                    phase_id, PhaseDemand()
                ).oldest_wait_s,
            )

        # Minimum green is a hard floor: no switch, not even for a deadline.
        if elapsed < current.min_green_s:
            self._clear_transient()
            return PhaseDecision(
                None,
                "minimum_green_not_met",
                scores=scores,
                constraints_applied=("min_green",),
                feasible=True,
            )

        # P0 deadline preemption: bypass countdown, switch immediately.
        if preempt_target is not None:
            self._clear_transient()
            return PhaseDecision(
                preempt_target,
                "p0_wait_preempt",
                scores=scores,
                constraints_applied=("min_green", "wait_deadline"),
                feasible=True,
            )

        # Maximum green is a hard cap: terminate the current phase.
        if elapsed >= current.max_green_s:
            successor = self._best_candidate(red_phases, blocked, demands)
            self._clear_transient()
            if successor is None:
                return PhaseDecision(
                    None,
                    "no_feasible_successor",
                    scores=scores,
                    constraints_applied=("max_green",),
                    feasible=True,
                )
            return PhaseDecision(
                successor,
                "max_green_reached",
                scores=scores,
                constraints_applied=("max_green",),
                feasible=True,
            )

        # ---- Optional (score-driven) switch with a 15 s countdown. ----
        if self._countdown_started_at is not None:
            if not self._score_exceeds(
                self._pending_phase_id, current, demands
            ):
                self._clear_transient()
                return PhaseDecision(
                    None, "countdown_cancelled", scores=scores, feasible=True
                )
            if (
                now - self._countdown_started_at
                >= self._config.countdown_duration_s
            ):
                target = self._pending_phase_id
                self._clear_transient()
                return PhaseDecision(
                    target,
                    "countdown_complete",
                    scores=scores,
                    constraints_applied=(
                        "hysteresis",
                        "debounce",
                        "countdown",
                    ),
                    feasible=True,
                )
            return PhaseDecision(
                None,
                "countdown_in_progress",
                scores=scores,
                constraints_applied=("countdown",),
                feasible=True,
            )

        best = self._best_candidate(red_phases, blocked, demands)
        if best is None:
            self._clear_transient()
            return PhaseDecision(
                None, "no_feasible_candidate", scores=scores, feasible=True
            )

        if not self._score_exceeds(best, current, demands):
            self._debounce_phase_id = None
            self._debounce_started_at = None
            return PhaseDecision(
                None, "score_not_exceeded", scores=scores, feasible=True
            )

        if self._debounce_phase_id != best:
            self._debounce_phase_id = best
            self._debounce_started_at = now
            return PhaseDecision(
                None,
                "debouncing",
                scores=scores,
                constraints_applied=("hysteresis",),
                feasible=True,
            )

        if now - self._debounce_started_at < self._config.switch_debounce_s:
            return PhaseDecision(
                None,
                "debouncing",
                scores=scores,
                constraints_applied=("hysteresis",),
                feasible=True,
            )

        self._debounce_phase_id = None
        self._debounce_started_at = None
        self._pending_phase_id = best
        self._countdown_started_at = now
        return PhaseDecision(
            None,
            "countdown_started",
            scores=scores,
            constraints_applied=("hysteresis", "debounce", "countdown"),
            feasible=True,
        )

    # -- helpers ------------------------------------------------------------
    def _phase_score(
        self, phase_id: str, demands: Mapping[str, PhaseDemand]
    ) -> float:
        demand = demands.get(phase_id, PhaseDemand())
        return self._scorer.phase_score(
            demand.vehicle_count,
            demand.oldest_wait_s,
            green_wave_active=demand.green_wave_active,
            downstream_blocked=demand.downstream_blocked,
        )

    def _best_candidate(
        self,
        red_phases: list[str],
        blocked: set[str],
        demands: Mapping[str, PhaseDemand],
    ) -> str | None:
        candidates = [phase_id for phase_id in red_phases if phase_id not in blocked]
        if not candidates:
            return None
        return max(candidates, key=lambda phase_id: self._phase_score(phase_id, demands))

    def _score_exceeds(
        self,
        candidate: str,
        current: SignalPhase,
        demands: Mapping[str, PhaseDemand],
    ) -> bool:
        candidate_score = self._phase_score(candidate, demands)
        current_score = self._phase_score(current.phase_id, demands)
        return candidate_score > current_score + self._config.score_hysteresis

    def _clear_transient(self) -> None:
        self._pending_phase_id = None
        self._countdown_started_at = None
        self._debounce_phase_id = None
        self._debounce_started_at = None
