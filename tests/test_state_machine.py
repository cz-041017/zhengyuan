import pytest

from traffic_control.models import ControllerMode, ControllerState, SignalPhase
from traffic_control.scoring import ScoreConfig, Scorer
from traffic_control.state_machine import (
    MachineConfig,
    MachineStage,
    PhaseDemand,
    PhaseStateMachine,
)


def make_phases() -> dict[str, SignalPhase]:
    return {
        "NS": SignalPhase(
            phase_id="NS",
            movement_ids=frozenset({"north_south"}),
            min_green_s=10,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        ),
        "EW": SignalPhase(
            phase_id="EW",
            movement_ids=frozenset({"east_west"}),
            min_green_s=10,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        ),
    }


def make_machine() -> PhaseStateMachine:
    return PhaseStateMachine(make_phases(), Scorer(ScoreConfig()))


def make_state(
    phase_id: str,
    started_at: float,
    mode: ControllerMode = ControllerMode.NORMAL,
) -> ControllerState:
    return ControllerState(
        mode=mode, current_phase_id=phase_id, phase_started_at=started_at
    )


def test_hold_before_minimum_green_even_for_preempt() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=200.0)}

    decision = machine.tick(state, 5.0, demands)

    assert decision.selected_phase_id is None
    assert decision.reason == "minimum_green_not_met"


def test_p0_preempt_switches_immediately() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    # At now=12 the minimum green (10) has elapsed, so the deadline is
    # 120 - (0 remaining min green + 4 clearance) = 116. wait 117 preempts.
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=117.0)}

    decision = machine.tick(state, 12.0, demands)

    assert decision.selected_phase_id == "EW"
    assert decision.reason == "p0_wait_preempt"
    assert decision.feasible is True


def test_p0_below_threshold_does_not_preempt() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=100.0)}

    decision = machine.tick(state, 12.0, demands)

    assert decision.selected_phase_id is None
    assert decision.reason != "p0_wait_preempt"


def test_p0_p1_conflict_is_infeasible() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(),
        "EW": PhaseDemand(oldest_wait_s=200.0, downstream_blocked=True),
    }

    decision = machine.tick(state, 12.0, demands)

    assert decision.feasible is False
    assert decision.reason == "p0_p1_conflict"


def test_max_green_forces_switch() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand()}

    decision = machine.tick(state, 60.0, demands)

    assert decision.selected_phase_id == "EW"
    assert decision.reason == "max_green_reached"


def test_blocked_candidate_excluded() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(),
        "EW": PhaseDemand(downstream_blocked=True),
    }

    decision = machine.tick(state, 20.0, demands)

    assert decision.reason == "no_feasible_candidate"
    assert decision.selected_phase_id is None


def test_green_wave_holds_current() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(green_wave_active=True),  # score 2.0
        "EW": PhaseDemand(),  # score 0.0
    }

    decision = machine.tick(state, 20.0, demands)

    assert decision.reason == "score_not_exceeded"


def test_score_switch_full_countdown_lifecycle() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=60.0)}

    decision = machine.tick(state, 20.0, demands)
    assert decision.reason == "debouncing"
    assert machine.stage == MachineStage.GREEN

    decision = machine.tick(state, 23.0, demands)
    assert decision.reason == "countdown_started"
    assert machine.stage == MachineStage.COUNTDOWN
    assert machine.pending_phase_id == "EW"

    decision = machine.tick(state, 30.0, demands)
    assert decision.reason == "countdown_in_progress"

    decision = machine.tick(state, 38.0, demands)
    assert decision.reason == "countdown_complete"
    assert decision.selected_phase_id == "EW"
    assert machine.stage == MachineStage.GREEN


def test_countdown_cancelled_when_score_drops() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    high = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=60.0)}
    low = {"NS": PhaseDemand(), "EW": PhaseDemand()}

    machine.tick(state, 20.0, high)  # debouncing
    machine.tick(state, 23.0, high)  # countdown_started

    decision = machine.tick(state, 30.0, low)  # score dropped

    assert decision.reason == "countdown_cancelled"
    assert machine.stage == MachineStage.GREEN


def test_current_downstream_blocked_switches_away() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(downstream_blocked=True),  # score -1000
        "EW": PhaseDemand(),  # score 0.0
    }

    decision = machine.tick(state, 20.0, demands)

    # Any unblocked candidate exceeds the blocked current phase.
    assert decision.reason == "debouncing"


def test_invalid_mode_is_infeasible() -> None:
    machine = make_machine()
    state = make_state("NS", 0.0, mode=ControllerMode.FAULT)

    decision = machine.tick(state, 20.0, {})

    assert decision.feasible is False
    assert decision.reason == "controller_not_normal"


def test_unknown_current_phase_is_infeasible() -> None:
    machine = make_machine()
    state = make_state("UNKNOWN", 0.0)

    decision = machine.tick(state, 20.0, {})

    assert decision.feasible is False
    assert decision.reason == "unknown_current_phase"


def test_machine_config_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="countdown_duration_s"):
        MachineConfig(countdown_duration_s=0.0)


def test_phase_demand_rejects_negative_count() -> None:
    with pytest.raises(ValueError, match="vehicle_count"):
        PhaseDemand(vehicle_count=-1)
