"""Regression tests for the hardening pass (review round 2)."""
from traffic_control.config_loader import load_config, validate_config
from traffic_control.decision_layer import DecisionLayer
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    SignalPhase,
    VehicleObservation,
)
from traffic_control.phasing import CROSSINGS, phase_groups
from traffic_control.scoring import ScoreConfig, Scorer
from traffic_control.state_machine import (
    MachineConfig,
    PhaseDemand,
    PhaseStateMachine,
)

DEFAULT = "configs/default.yaml"


def make_phases() -> dict[str, SignalPhase]:
    return {
        p: SignalPhase(
            phase_id=p,
            movement_ids=frozenset({p}),
            min_green_s=15,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        )
        for p in ("P_N", "P_E", "P_S", "P_W")
    }


def state(phase: str, started: float) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase,
        phase_started_at=started,
    )


# -- S3: pedestrian section is optional -----------------------------------
def test_pedestrian_section_is_optional() -> None:
    # panan config has no pedestrian section and must still load.
    config = load_config("panan/config/panan.yaml")
    assert "pedestrian" not in config or not config["pedestrian"]


def test_pedestrian_null_is_tolerated() -> None:
    config = load_config(DEFAULT)
    config["pedestrian"] = None
    validate_config(config)  # must not raise
    DecisionLayer(config)  # must not raise


# -- S2: guarantee must never push a direction past the P0 deadline ------
def test_pedestrian_guarantee_rejected_when_hold_would_overshoot() -> None:
    machine = PhaseStateMachine(
        make_phases(),
        Scorer(ScoreConfig(max_wait_target_s=130.0)),
        MachineConfig(
            pedestrian_max_red_s=150.0,
            pedestrian_min_green_s=12.0,
            data_gap_s=1000.0,
        ),
    )
    machine.tick(state("P_N", 0.0), 0.0, {})
    machine._last_current_phase = "P_N"
    # P_E/P_S/P_W overdue; P_S waits 110 s so a 19 s hold would overshoot 130.
    machine._last_green_end = {
        "P_N": 0.0,
        "P_E": 0.0,
        "P_W": 0.0,
        "P_S": 160.0,
    }
    decision = machine.tick(
        state("P_N", 140.0), 160.0, {"P_S": PhaseDemand(oldest_wait_s=110.0)}
    )
    assert decision.reason != "pedestrian_guarantee"


# -- S5: EW green-wave crossing mapping ----------------------------------
def test_green_wave_ew_crosswalk_mapping() -> None:
    groups = {
        g.phase_id: g.crosswalk_ids for g in phase_groups("main_road", "EW")
    }
    assert groups["P_MT"] == frozenset({CROSSINGS["N"], CROSSINGS["S"]})
    assert groups["P_ML"] == frozenset()
    assert groups["P_N"] == frozenset({CROSSINGS["W"]})
    assert groups["P_S"] == frozenset({CROSSINGS["E"]})


# -- M2: active_crosswalks on a normal tick ------------------------------
def test_active_crosswalks_on_normal_tick() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    observations = [
        VehicleObservation(
            track_id=i,
            lane_id=lane,
            observed_at=0.0,
            speed_mps=0.0,
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    result = layer.tick(0.0, state("P_N", 0.0), observations=observations)
    assert result.active_crosswalks == frozenset({CROSSINGS["E"]})


# -- M4: all non-blocked successors excluded -> infeasible ---------------
def test_all_blocked_returns_infeasible() -> None:
    machine = PhaseStateMachine(
        make_phases(),
        Scorer(ScoreConfig()),
        MachineConfig(data_gap_s=1000.0),
    )
    machine.tick(state("P_N", 0.0), 0.0, {})
    # Every red direction is downstream-blocked.
    demands = {
        "P_E": PhaseDemand(downstream_blocked=True),
        "P_S": PhaseDemand(downstream_blocked=True),
        "P_W": PhaseDemand(downstream_blocked=True),
    }
    decision = machine.tick(state("P_N", 0.0), 20.0, demands)
    assert decision.feasible is False


# -- M10: release must actually recover ----------------------------------
def test_release_recovers_with_fresh_observations() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    layer.takeover(10.0)
    layer.release(20.0)

    observations = [
        VehicleObservation(
            track_id=i,
            lane_id=lane,
            observed_at=36.0,
            speed_mps=0.0,
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    result = layer.tick(36.0, state("P_N", 20.0), observations=observations)

    assert result.startup is None
    assert result.decision.reason != "startup_sequence"
    assert result.decision.reason != "failsafe_hold"
