from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from traffic_control.models import SignalPhase


@dataclass(frozen=True)
class TimingAnalysis:
    """Result of the worst-case timing analysis for the P0 deadline."""

    feasible_120s: bool
    max_time_to_green_s: float
    worst_wait_s: Mapping[str, float]
    preempt_required: frozenset[str]
    reasons: tuple[str, ...]


def analyze_timing(
    phases: Mapping[str, SignalPhase], max_wait_target_s: float
) -> TimingAnalysis:
    """Worst-case timing analysis for the P0 ≤ D guarantee.

    ``feasible_120s`` is True only when the largest switch overhead — a
    phase's minimum green plus its yellow/all-red clearance — is strictly
    below the deadline. Otherwise a direction could not be served within the
    deadline even by an immediate preemption, because the current phase must
    first finish its minimum green.

    ``worst_wait_s`` reports each phase's worst red time WITHOUT preemption
    (the full cycle minus its own green). A phase whose worst wait reaches the
    deadline appears in ``preempt_required``: the deadline is then
    load-bearing on preemption rather than structurally guaranteed, which
    signals that the maximum greens are too generous for the target.
    """
    if not phases:
        raise ValueError("at least one phase is required")
    if max_wait_target_s <= 0:
        raise ValueError("max_wait_target_s must be positive")

    clearances = {
        phase_id: phase.yellow_s + phase.all_red_s
        for phase_id, phase in phases.items()
    }

    # Largest time from "preempt now" to "direction gets green": the current
    # phase's full minimum green plus its clearance (worst case: just started).
    max_time_to_green = max(
        phase.min_green_s + clearances[phase_id]
        for phase_id, phase in phases.items()
    )

    # Full cycle at maximum green, then each phase's worst red time is the
    # cycle minus its own green (its clearance is part of the cycle).
    cycle = sum(
        phase.max_green_s + clearances[phase_id]
        for phase_id, phase in phases.items()
    )
    worst_wait = {
        phase_id: cycle - phase.max_green_s
        for phase_id, phase in phases.items()
    }

    reasons: list[str] = []
    feasible = max_time_to_green < max_wait_target_s
    if not feasible:
        reasons.append("switch_overhead_exceeds_deadline")

    preempt_required = frozenset(
        phase_id
        for phase_id, wait in worst_wait.items()
        if wait >= max_wait_target_s
    )
    if preempt_required:
        reasons.append("preempt_load_bearing")

    return TimingAnalysis(
        feasible_120s=feasible,
        max_time_to_green_s=max_time_to_green,
        worst_wait_s=worst_wait,
        preempt_required=preempt_required,
        reasons=tuple(reasons),
    )
