from traffic_control.models import ControllerMode, ControllerState, SignalPhase
from traffic_control.phasing import CROSSINGS, phase_groups
from traffic_control.scoring import ScoreConfig, Scorer
from traffic_control.state_machine import (
    MachineConfig,
    PhaseDemand,
    PhaseStateMachine,
)


def make_phases() -> dict[str, SignalPhase]:
    return {
        phase: SignalPhase(
            phase_id=phase,
            movement_ids=frozenset({f"{phase.lower()}_through"}),
            min_green_s=15,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        )
        for phase in ("P_N", "P_E", "P_S", "P_W")
    }


def make_machine() -> PhaseStateMachine:
    config = MachineConfig(
        pedestrian_max_red_s=150.0,
        pedestrian_min_green_s=12.0,
        data_gap_s=1000.0,  # these tests drive sparse ticks on purpose
    )
    return PhaseStateMachine(make_phases(), Scorer(ScoreConfig()), config)


def state(phase_id: str, started_at: float) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase_id,
        phase_started_at=started_at,
    )


# -- crosswalk mapping -----------------------------------------------------
def test_minor_only_crosswalk_mapping_is_right_hand_side() -> None:
    groups = {g.phase_id: g.crosswalk_ids for g in phase_groups("minor_only")}

    assert groups["P_N"] == frozenset({CROSSINGS["E"]})
    assert groups["P_E"] == frozenset({CROSSINGS["S"]})
    assert groups["P_S"] == frozenset({CROSSINGS["W"]})
    assert groups["P_W"] == frozenset({CROSSINGS["N"]})


def test_green_wave_crosswalk_mapping() -> None:
    groups = {
        g.phase_id: g.crosswalk_ids for g in phase_groups("main_road", "NS")
    }

    # Main-road through releases both crossings alongside the main road.
    assert groups["P_MT"] == frozenset({CROSSINGS["E"], CROSSINGS["W"]})
    # Main-road left releases none (all pedestrians red).
    assert groups["P_ML"] == frozenset()
    assert groups["P_E"] == frozenset({CROSSINGS["N"]})
    assert groups["P_W"] == frozenset({CROSSINGS["S"]})


# -- pedestrian guarantee --------------------------------------------------
def test_pedestrian_guarantee_fires_after_max_red() -> None:
    machine = make_machine()
    # t=0: P_N green (records _started_at).
    machine.tick(state("P_N", 0.0), 0.0, {})
    # Simulate history: the controller already switched to P_E, P_N last green
    # ended at t=10, the other three at t=170. At t=175 P_N has been red 165 s.
    machine._last_current_phase = "P_E"
    machine._last_green_end = {
        "P_N": 10.0,
        "P_E": 170.0,
        "P_S": 170.0,
        "P_W": 170.0,
    }
    decision = machine.tick(state("P_E", 20.0), 175.0, {})
    assert decision.selected_phase_id == "P_N"
    assert decision.reason == "pedestrian_guarantee"


def test_pedestrian_guarantee_yields_to_p0_deadline() -> None:
    machine = make_machine()
    machine.tick(state("P_N", 0.0), 0.0, {})
    # t=20: move to P_E.
    machine.tick(
        state("P_N", 0.0), 20.0, {"P_E": PhaseDemand(oldest_wait_s=130.0)}
    )
    # t=180: P_N is overdue (red 160 s) AND P_W hits the P0 deadline.
    # P0 must win.
    decision = machine.tick(
        state("P_E", 20.0),
        180.0,
        {"P_W": PhaseDemand(oldest_wait_s=130.0)},
    )
    assert decision.selected_phase_id == "P_W"
    assert decision.reason == "p0_wait_preempt"


def test_pedestrian_guarantee_skips_blocked_direction() -> None:
    machine = make_machine()
    machine.tick(state("P_N", 0.0), 0.0, {})
    machine._last_current_phase = "P_E"
    machine._last_green_end = {
        "P_N": 10.0,
        "P_E": 170.0,
        "P_S": 170.0,
        "P_W": 170.0,
    }
    # P_N is overdue but downstream blocked; no other direction is overdue.
    decision = machine.tick(
        state("P_E", 20.0),
        175.0,
        {"P_N": PhaseDemand(downstream_blocked=True)},
    )
    assert decision.reason != "pedestrian_guarantee"


def test_pedestrian_guarantee_respects_minimum_green() -> None:
    machine = make_machine()
    machine.tick(state("P_N", 0.0), 0.0, {})
    machine.tick(
        state("P_N", 0.0), 20.0, {"P_E": PhaseDemand(oldest_wait_s=130.0)}
    )
    # At t=30 P_E has only 10 s green (< 15 s min): the guarantee must wait.
    decision = machine.tick(state("P_E", 20.0), 30.0, {})
    assert decision.reason == "minimum_green_not_met"


def test_pedestrian_guarantee_via_real_ticks() -> None:
    """Exercise the real bookkeeping path (no private-field injection)."""
    from traffic_control.models import ControllerMode, ControllerState, SignalPhase
    from traffic_control.scoring import ScoreConfig, Scorer
    from traffic_control.state_machine import (
        MachineConfig,
        PhaseDemand,
        PhaseStateMachine,
    )

    phases = {
        "P_N": SignalPhase(
            phase_id="P_N",
            movement_ids=frozenset({"north_through"}),
            min_green_s=15,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        ),
        "P_E": SignalPhase(
            phase_id="P_E",
            movement_ids=frozenset({"east_through"}),
            min_green_s=15,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        ),
    }
    machine = PhaseStateMachine(
        phases,
        Scorer(ScoreConfig()),
        MachineConfig(
            pedestrian_max_red_s=150.0,
            pedestrian_min_green_s=12.0,
            data_gap_s=1000.0,
        ),
    )

    def st(phase: str, started: float) -> ControllerState:
        return ControllerState(
            mode=ControllerMode.NORMAL,
            current_phase_id=phase,
            phase_started_at=started,
        )

    # t=0: P_N green.
    machine.tick(st("P_N", 0.0), 0.0, {})
    # t=20: P_E preempts via P0.
    decision = machine.tick(
        st("P_N", 0.0), 20.0, {"P_E": PhaseDemand(oldest_wait_s=130.0)}
    )
    assert decision.selected_phase_id == "P_E"
    # t=40: controller now reports P_E started at 20.
    machine.tick(st("P_E", 20.0), 40.0, {})
    # t=180: P_N red since 20 -> 160 s >= 150 s, guarantee fires.
    decision = machine.tick(st("P_E", 20.0), 180.0, {})
    assert decision.selected_phase_id == "P_N"
    assert decision.reason == "pedestrian_guarantee"
