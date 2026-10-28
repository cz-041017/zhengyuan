from __future__ import annotations

import warnings

from dataclasses import dataclass
from typing import Any

from traffic_control.degradation import DegradationController
from traffic_control.feedback import FeedbackMonitor
from traffic_control.models import SignalPhase
from traffic_control.phasing import ConflictMatrix, PhaseGroup, phase_groups
from traffic_control.scoring import ScoreConfig, Scorer, SpillbackMonitor
from traffic_control.state_machine import MachineConfig, PhaseStateMachine
from traffic_control.timing_analysis import analyze_timing

# Simulation-only placeholder timings. These are NOT audited values and must
# never be used for deployment; deployment configs must carry reviewed
# per-phase timings in signal.phases.
_SIM_MIN_GREEN_S = 15.0
_SIM_MAX_GREEN_S = 60.0
_SIM_YELLOW_S = 3.0
_SIM_ALL_RED_S = 1.0


def build_score_config(config: dict[str, Any]) -> ScoreConfig:
    """Build the scoring parameters from a validated configuration."""
    optimization = config["optimization"]
    demand = optimization["demand"]
    spillback = optimization["spillback"]
    return ScoreConfig(
        max_wait_target_s=optimization["max_wait_target_s"],
        wait_urgency_start_s=optimization["wait_urgency_start_s"],
        wait_urgency_max_score=optimization["wait_urgency_max_score"],
        green_wave_score=optimization["green_wave_score"],
        demand_vehicle_weight=demand["vehicle_weight"],
        demand_wait_weight=demand["wait_weight"],
        n_max=demand["n_max"],
        spillback_penalty=spillback["penalty"],
        o_high=spillback["o_high"],
        o_low=spillback["o_low"],
    )


def build_machine_config(config: dict[str, Any]) -> MachineConfig:
    """Build the switch-gating parameters from a validated configuration."""
    optimization = config["optimization"]
    pedestrian = config.get("pedestrian") or {}
    return MachineConfig(
        score_hysteresis=optimization["score_hysteresis"],
        switch_debounce_s=optimization["switch_debounce_s"],
        countdown_duration_s=config["countdown"]["display_duration_s"],
        pedestrian_max_red_s=pedestrian.get("max_red_s", 150.0),
        pedestrian_min_green_s=pedestrian.get("min_green_s", 12.0),
        data_gap_s=config.get("runtime", {}).get("data_gap_s", 6.0),
    )


def build_phase_groups(config: dict[str, Any]) -> tuple[PhaseGroup, ...]:
    """Derive the phase groups from the declared signal layout."""
    layout = config["signal"]["layout"]
    return phase_groups(layout["mode"], layout.get("main_road_direction"))


def build_conflict_matrix(config: dict[str, Any]) -> ConflictMatrix:
    """Load the audited conflict matrix from the configuration."""
    matrix = config["signal"]["conflict_matrix"]
    return ConflictMatrix({key: frozenset(value) for key, value in matrix.items()})


def build_phases(config: dict[str, Any]) -> dict[str, SignalPhase]:
    """Build SignalPhase objects from the configuration.

    When ``signal.phases`` is populated, each phase must match the
    layout-derived group (same phase id and movement set); a mismatch is a
    configuration error. When empty (simulation), placeholder timings are
    used and clearly flagged as un-audited.
    """
    groups = build_phase_groups(config)
    group_map = {group.phase_id: group for group in groups}
    phase_items = config["signal"]["phases"]

    if phase_items:
        phases: dict[str, SignalPhase] = {}
        for item in phase_items:
            phase_id = item["phase_id"]
            if phase_id in phases:
                raise ValueError(
                    f"duplicate phase definition for {phase_id!r} in "
                    "signal.phases; each phase must be defined exactly once"
                )
            phases[phase_id] = SignalPhase(
                phase_id=phase_id,
                movement_ids=frozenset(item["movement_ids"]),
                min_green_s=item["min_green_s"],
                max_green_s=item["max_green_s"],
                yellow_s=item["yellow_s"],
                all_red_s=item.get("all_red_s", 0.0),
                crosswalk_ids=(
                    group_map[phase_id].crosswalk_ids
                    if phase_id in group_map
                    else frozenset()
                ),
            )
        _cross_check_phases(phases, groups)
        _warn_pedestrian_min_green(phases, config)
        return phases

    placeholder = {
        group.phase_id: SignalPhase(
            phase_id=group.phase_id,
            movement_ids=group.movement_ids,
            min_green_s=_SIM_MIN_GREEN_S,
            max_green_s=_SIM_MAX_GREEN_S,
            yellow_s=_SIM_YELLOW_S,
            all_red_s=_SIM_ALL_RED_S,
            crosswalk_ids=group.crosswalk_ids,
        )
        for group in groups
    }
    _warn_pedestrian_min_green(placeholder, config)
    return placeholder


def _warn_pedestrian_min_green(
    phases: dict[str, SignalPhase], config: dict[str, Any]
) -> None:
    """A pedestrian hold lasts one phase minimum green.

    If a phase's minimum green is shorter than ``pedestrian.min_green_s`` the
    guarantee cannot actually give pedestrians their configured crossing time;
    warn loudly instead of silently under-serving them.
    """
    required = (config.get("pedestrian") or {}).get("min_green_s", 12.0)
    for phase in phases.values():
        if phase.min_green_s < required:
            warnings.warn(
                f"phase {phase.phase_id!r} min_green_s={phase.min_green_s}s "
                f"is shorter than pedestrian.min_green_s={required}s; "
                "pedestrian guarantee will be under-served",
                stacklevel=3,
            )


def _cross_check_phases(
    phases: dict[str, SignalPhase], groups: tuple[PhaseGroup, ...]
) -> None:
    expected = {group.phase_id: group.movement_ids for group in groups}
    actual = {
        phase_id: phase.movement_ids for phase_id, phase in phases.items()
    }
    if set(expected) != set(actual):
        raise ValueError(
            "configured phases do not match the layout phases "
            f"(layout={sorted(expected)}, config={sorted(actual)})"
        )
    for phase_id in expected:
        if expected[phase_id] != actual[phase_id]:
            raise ValueError(
                f"phase {phase_id!r} movements do not match the layout"
            )


@dataclass(frozen=True)
class RuntimeObjects:
    """The fully assembled decision-layer runtime from a validated config."""

    score_config: ScoreConfig
    machine_config: MachineConfig
    phases: dict[str, SignalPhase]
    phase_groups: tuple[PhaseGroup, ...]
    conflict_matrix: ConflictMatrix
    scorer: Scorer
    spillback_monitor: SpillbackMonitor
    degradation: DegradationController
    feedback_monitor: FeedbackMonitor
    state_machine: PhaseStateMachine


def build_runtime(config: dict[str, Any]) -> RuntimeObjects:
    """Build and cross-check the complete decision-layer runtime."""
    groups = build_phase_groups(config)
    matrix = build_conflict_matrix(config)

    group_ids = {group.phase_id for group in groups}
    matrix_ids = set(matrix.conflicts.keys())
    if group_ids != matrix_ids:
        raise ValueError(
            "conflict matrix phases "
            f"{sorted(matrix_ids)} do not match layout phases "
            f"{sorted(group_ids)}"
        )

    score_config = build_score_config(config)
    machine_config = build_machine_config(config)
    phases = build_phases(config)

    analysis = analyze_timing(phases, score_config.max_wait_target_s)
    if not analysis.feasible_120s:
        warnings.warn(
            "timing may be infeasible: the largest switch overhead "
            f"({analysis.max_time_to_green_s:.1f}s) reaches the wait deadline "
            f"({score_config.max_wait_target_s:.1f}s), so the P0 guarantee "
            "cannot hold even with an immediate preemption. Shorten the "
            "minimum greens or the clearance times.",
            stacklevel=2,
        )

    scorer = Scorer(score_config)
    spillback_monitor = SpillbackMonitor(
        o_high=score_config.o_high, o_low=score_config.o_low
    )
    degradation = DegradationController(
        timeout_s=config["runtime"]["observation_timeout_s"]
    )
    feedback_monitor = FeedbackMonitor(
        confirm_timeout_s=config["runtime"]["confirm_timeout_s"]
    )
    state_machine = PhaseStateMachine(phases, scorer, machine_config)

    return RuntimeObjects(
        score_config=score_config,
        machine_config=machine_config,
        phases=phases,
        phase_groups=groups,
        conflict_matrix=matrix,
        scorer=scorer,
        spillback_monitor=spillback_monitor,
        degradation=degradation,
        feedback_monitor=feedback_monitor,
        state_machine=state_machine,
    )
