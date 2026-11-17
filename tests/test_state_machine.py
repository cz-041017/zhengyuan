import pytest

from traffic_control.models import ControllerMode, ControllerState, SignalPhase
from traffic_control.scoring import ScoreConfig, Scorer
from traffic_control.state_machine import (
    MachineConfig,
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
    # 130 - (0 remaining min green + 4 clearance) = 126. wait 127 preempts.
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=127.0)}

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


def test_blocked_deadline_does_not_freeze_other_directions() -> None:
    # A blocked direction at its deadline must NOT freeze the whole junction:
    # the serviceable direction at deadline still preempts (P0 fires for it),
    # while the blocked direction is simply excluded (P1 beats P0 per direction).
    phases = make_phases()
    phases["WL"] = SignalPhase(
        phase_id="WL",
        movement_ids=frozenset({"west_left"}),
        min_green_s=10,
        max_green_s=60,
        yellow_s=3,
        all_red_s=1,
    )
    machine = PhaseStateMachine(phases, Scorer(ScoreConfig()))
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(vehicle_count=1),
        "EW": PhaseDemand(oldest_wait_s=200.0, downstream_blocked=True),
        "WL": PhaseDemand(oldest_wait_s=130.0),
    }

    decision = machine.tick(state, 12.0, demands)

    assert decision.selected_phase_id == "WL"
    assert decision.reason == "p0_wait_preempt"
    assert decision.feasible is True


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


def test_score_switch_after_debounce() -> None:
    """Score-winning successor is taken after the debounce; no countdown stage."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    # Current phase carries traffic, so the no-demand fast switch does not fire.
    demands = {"NS": PhaseDemand(vehicle_count=1), "EW": PhaseDemand(oldest_wait_s=60.0)}

    decision = machine.tick(state, 20.0, demands)
    assert decision.reason == "debouncing"

    # Debounce (2 s) elapsed -> switch immediately, no countdown.
    decision = machine.tick(state, 22.0, demands)
    assert decision.reason == "score_switch"
    assert decision.selected_phase_id == "EW"


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
    with pytest.raises(ValueError, match="switch_debounce_s"):
        MachineConfig(switch_debounce_s=-1.0)


def test_phase_demand_rejects_negative_count() -> None:
    with pytest.raises(ValueError, match="vehicle_count"):
        PhaseDemand(vehicle_count=-1)


def test_no_demand_switch_skips_debounce_after_confirm() -> None:
    """Zero vehicles for 3 s -> take the score-winning successor immediately."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    # EW must actually carry a vehicle: a merely "high wait" empty direction
    # is not a valid no-demand target (all-empty must hold).
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(vehicle_count=1, oldest_wait_s=60.0)}

    # t=20: first empty tick starts the confirm clock (no switch yet).
    decision = machine.tick(state, 20.0, demands)
    assert decision.reason == "debouncing"

    # t=21: 1 s of confirmed emptiness -> switch BEFORE the 2 s debounce,
    # proving the no-demand path really skips the score debounce.
    decision = machine.tick(state, 21.0, demands)
    assert decision.reason == "no_demand_switch"
    assert decision.selected_phase_id == "EW"


def test_all_directions_empty_keeps_current_phase() -> None:
    """Every direction empty -> scores do not exceed -> hold current phase."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand()}

    for now in (20.0, 23.0, 30.0):
        decision = machine.tick(state, now, demands)
        assert decision.reason != "no_demand_switch"
        assert decision.selected_phase_id is None


def test_no_demand_switch_waits_for_minimum_green() -> None:
    """The fast switch never bypasses the minimum green."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(), "EW": PhaseDemand(oldest_wait_s=60.0)}

    # elapsed 5 s < min green 10 s
    decision = machine.tick(state, 5.0, demands)
    assert decision.reason == "minimum_green_not_met"


def test_debounce_resets_when_score_drops() -> None:
    """Score falling back during debounce cancels the pending switch."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    high = {"NS": PhaseDemand(vehicle_count=1), "EW": PhaseDemand(vehicle_count=1, oldest_wait_s=60.0)}
    low = {"NS": PhaseDemand(vehicle_count=1), "EW": PhaseDemand()}

    machine.tick(state, 20.0, high)   # debouncing
    machine.tick(state, 21.0, low)    # score dropped -> debounce cleared
    decision = machine.tick(state, 22.0, low)
    assert decision.reason == "score_not_exceeded"


def test_debounce_restarts_when_candidate_becomes_blocked() -> None:
    """If the candidate becomes spillback-blocked, the pending switch is dropped."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    high = {"NS": PhaseDemand(vehicle_count=1), "EW": PhaseDemand(vehicle_count=1, oldest_wait_s=60.0)}
    blocked = {
        "NS": PhaseDemand(vehicle_count=1),
        "EW": PhaseDemand(vehicle_count=1, oldest_wait_s=200.0, downstream_blocked=True),
    }

    machine.tick(state, 20.0, high)      # debouncing toward EW
    decision = machine.tick(state, 25.0, blocked)
    assert decision.reason != "score_switch"
    assert decision.selected_phase_id != "EW"


def test_data_gap_resets_trackers() -> None:
    """A perception gap must not let a stale debounce complete."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {"NS": PhaseDemand(vehicle_count=1), "EW": PhaseDemand(vehicle_count=1, oldest_wait_s=60.0)}

    machine.tick(state, 20.0, demands)   # debouncing
    machine.tick(state, 30.0, demands)   # 10 s gap > data_gap_s (6 s), resets
    decision = machine.tick(state, 31.0, demands)  # 1 s after reset -> re-debouncing
    assert decision.reason == "debouncing"


def test_green_wave_empty_candidate_may_win() -> None:
    """A green-wave phase may be released even if it currently has no vehicles."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(),
        "EW": PhaseDemand(green_wave_active=True),
    }

    machine.tick(state, 20.0, demands)   # empty -> confirm clock starts
    decision = machine.tick(state, 21.0, demands)
    assert decision.reason == "no_demand_switch"
    assert decision.selected_phase_id == "EW"


def test_stale_high_wait_empty_candidate_does_not_win() -> None:
    """An empty direction that merely has a stale high wait is NOT a target."""
    machine = make_machine()
    state = make_state("NS", 0.0)
    demands = {
        "NS": PhaseDemand(),
        "EW": PhaseDemand(vehicle_count=0, oldest_wait_s=100.0),
    }

    for now in (20.0, 23.0, 30.0):
        decision = machine.tick(state, now, demands)
        assert decision.reason != "no_demand_switch"
