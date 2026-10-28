"""Third-round hardening tests: gaps the reviewer flagged as untested."""
import pytest

from traffic_control.config_loader import ConfigError, load_config, validate_config
from traffic_control.decision_layer import DecisionLayer
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    SignalPhase,
    VehicleObservation,
)
from traffic_control.scoring import ScoreConfig, Scorer
from traffic_control.state_machine import (
    MachineConfig,
    PhaseDemand,
    PhaseStateMachine,
)

DEFAULT = "configs/default.yaml"


def make_phases(min_green: float = 15.0) -> dict[str, SignalPhase]:
    return {
        p: SignalPhase(
            phase_id=p,
            movement_ids=frozenset({p}),
            min_green_s=min_green,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        )
        for p in ("P_N", "P_E", "P_S", "P_W")
    }


def st(phase: str, started: float) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase,
        phase_started_at=started,
    )


def make_machine(**overrides) -> PhaseStateMachine:
    cfg = dict(
        pedestrian_max_red_s=150.0,
        pedestrian_min_green_s=12.0,
        data_gap_s=6.0,
    )
    cfg.update(overrides)
    return PhaseStateMachine(
        make_phases(),
        Scorer(ScoreConfig(max_wait_target_s=130.0)),
        MachineConfig(**cfg),
    )


# -- S1: data gap with multiple unobserved switches ------------------------
def test_data_gap_rebuilds_baseline_and_does_not_fire_early() -> None:
    """A long gap must not be counted as pedestrian waiting time.

    Without the rebuild the P_N baseline would be t=0 and at t=181 it would look
    like 181 s of red (>= 150) and fire the guarantee. With the rebuild the
    baseline is t=180, so nothing is overdue.
    """
    machine = make_machine()
    machine.tick(st("P_N", 0.0), 0.0, {})
    # 181 s gap; the controller switched unobserved (P_W started at 160, so its
    # elapsed green is >= min green and the min-green guard does not mask this).
    machine.tick(st("P_W", 160.0), 181.0, {})

    decision = machine.tick(st("P_W", 160.0), 182.0, {})
    assert decision.reason != "pedestrian_guarantee"


# -- S2: lower bound of the hold-rejection window -------------------------
def _overdue_machine(wait: float):
    machine = make_machine(data_gap_s=1000.0)
    machine.tick(st("P_N", 0.0), 0.0, {})
    machine._last_current_phase = "P_N"
    machine._last_green_end = {
        "P_N": 0.0,
        "P_E": 0.0,
        "P_W": 0.0,
        "P_S": 160.0,
    }
    return machine.tick(
        st("P_N", 140.0), 160.0, {"P_S": PhaseDemand(oldest_wait_s=wait)}
    )


def test_pedestrian_guarantee_allowed_just_below_threshold() -> None:
    # hold = 15+3+1 = 19; deadline 130, time_to_green 4 -> threshold 107.
    decision = _overdue_machine(106.9)
    assert decision.reason == "pedestrian_guarantee"


def test_pedestrian_guarantee_rejected_at_threshold() -> None:
    decision = _overdue_machine(107.0)
    assert decision.reason != "pedestrian_guarantee"


# -- M4: both no-feasible paths are infeasible and alarming ---------------
def test_no_feasible_candidate_is_infeasible() -> None:
    machine = make_machine()
    machine.tick(st("P_N", 0.0), 0.0, {})
    decision = machine.tick(
        st("P_N", 0.0),
        20.0,
        {p: PhaseDemand(downstream_blocked=True) for p in ("P_E", "P_S", "P_W")},
    )
    assert decision.reason == "no_feasible_candidate"
    assert decision.feasible is False


def test_no_feasible_successor_is_infeasible() -> None:
    machine = make_machine()
    machine.tick(st("P_N", 0.0), 0.0, {})
    # Past max green (60 s) with every successor blocked.
    decision = machine.tick(
        st("P_N", 0.0),
        70.0,
        {p: PhaseDemand(downstream_blocked=True) for p in ("P_E", "P_S", "P_W")},
    )
    assert decision.reason == "no_feasible_successor"
    assert decision.feasible is False


def test_no_feasible_alarm_is_emitted() -> None:
    from traffic_control.telemetry import AlarmType, CollectingTelemetrySink
    from traffic_control.decision_layer import DecisionLayer

    sink = CollectingTelemetrySink()
    layer = DecisionLayer.from_yaml(DEFAULT, sink=sink)
    obs = [
        VehicleObservation(
            track_id=i, lane_id=lane, observed_at=0.0, speed_mps=0.0
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    layer.tick(0.0, st("P_N", 0.0), observations=obs)
    fresh = [
        VehicleObservation(
            track_id=10 + i, lane_id=lane, observed_at=20.0, speed_mps=0.0
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    # All red directions report "downstream jammed" via occupancy 1.0.
    result = layer.tick(
        20.0,
        st("P_N", 0.0),
        observations=fresh,
        downstream_occupancy={
            "east_through": 1.0,
            "south_through": 1.0,
            "west_through": 1.0,
        },
    )
    assert result.decision.feasible is False
    assert any(a.alarm_type is AlarmType.NO_FEASIBLE_PHASE for a in sink.alarms)


# -- S4: manual gap must not produce a false FAILURE ----------------------
def test_manual_gap_does_not_fail_feedback() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    obs = [
        VehicleObservation(
            track_id=i, lane_id=lane, observed_at=0.0, speed_mps=0.0
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    layer.tick(0.0, st("P_N", 0.0), observations=obs)
    # 130 s: west feed dead -> P0 preemption arms a pending switch.
    layer.tick(
        130.0,
        st("P_N", 0.0),
        observations=[
            VehicleObservation(
                track_id=10 + i, lane_id=lane, observed_at=129.0, speed_mps=0.0
            )
            for i, lane in enumerate(
                ["north_through", "east_through", "south_through"], 1
            )
        ],
    )
    layer.takeover(131.0)
    for t in range(131, 142):
        layer.tick(float(t), st("P_N", 0.0))
    layer.release(142.0)
    result = layer.tick(143.0, st("P_N", 20.0), observations=obs)
    assert result.feedback.value != "failure"


# -- M8: loading-time guards ---------------------------------------------
def test_pedestrian_min_green_guard_placeholder() -> None:
    config = load_config(DEFAULT)
    config["pedestrian"] = {
        "max_red_s": 150.0,
        "min_green_s": 30.0,
        "yellow_s": 3.0,
    }
    with pytest.raises(ConfigError):
        validate_config(config)


def test_pedestrian_min_green_guard_explicit_phase() -> None:
    config = load_config(DEFAULT)
    config["pedestrian"] = {
        "max_red_s": 150.0,
        "min_green_s": 12.0,
        "yellow_s": 3.0,
    }
    config["signal"]["phases"] = [
        {
            "phase_id": "P_N",
            "movement_ids": ["north_through"],
            "min_green_s": 10.0,
            "max_green_s": 60.0,
            "yellow_s": 3.0,
        }
    ]
    with pytest.raises(ConfigError):
        validate_config(config)


def test_pedestrian_null_tolerated() -> None:
    config = load_config(DEFAULT)
    config["pedestrian"] = None
    validate_config(config)
    DecisionLayer(config)


# -- M1: pedestrian yellow remaining -------------------------------------
def test_pedestrian_yellow_remaining_window() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    obs = [
        VehicleObservation(
            track_id=i, lane_id=lane, observed_at=0.0, speed_mps=0.0
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    # Placeholder min green = 15 s, pedestrian yellow = 3 s -> starts at 12 s.
    def fresh(now: float):
        return [
            VehicleObservation(
                track_id=20 + i, lane_id=lane, observed_at=now, speed_mps=0.0
            )
            for i, lane in enumerate(
                ["north_through", "east_through", "south_through", "west_through"], 1
            )
        ]

    layer.tick(0.0, st("P_N", 0.0), observations=fresh(0.0))
    r12 = layer.tick(12.0, st("P_N", 0.0), observations=fresh(12.0))
    assert r12.pedestrian_yellow_remaining_s == pytest.approx(3.0)
    r135 = layer.tick(13.5, st("P_N", 0.0), observations=fresh(13.5))
    assert r135.pedestrian_yellow_remaining_s == pytest.approx(1.5)
    r15 = layer.tick(15.0, st("P_N", 0.0), observations=fresh(15.0))
    assert r15.pedestrian_yellow_remaining_s == pytest.approx(0.0)


def test_score_config_default_max_wait_target_is_130() -> None:
    """Code default must match the documented 130 s P0 deadline."""
    from traffic_control.scoring import ScoreConfig

    assert ScoreConfig().max_wait_target_s == 130.0
