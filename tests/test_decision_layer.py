"""Tests for the DecisionLayer facade (the ready-to-use decision package)."""

from pathlib import Path

import pytest

from traffic_control.decision_layer import DecisionLayer
from traffic_control.degradation import DegradationMode
from traffic_control.feedback import FeedbackStatus
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    VehicleObservation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT = PROJECT_ROOT / "configs" / "default.yaml"
SIMULATION = PROJECT_ROOT / "configs" / "simulation.yaml"


def make_state(phase_id: str, started_at: float) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase_id,
        phase_started_at=started_at,
    )


def test_from_yaml_builds_and_exposes_parameters() -> None:
    layer = DecisionLayer.from_yaml(SIMULATION)

    params = layer.parameters
    assert params.max_wait_target_s == 120.0
    assert params.observation_timeout_s == 3.0
    assert params.confirm_timeout_s == 10.0
    assert params.queue_speed_threshold_mps == 0.5
    assert params.score.green_wave_score == 2.0
    assert params.machine.countdown_duration_s == 15.0
    assert {g.phase_id for g in layer.runtime.phase_groups} == {
        "P_MT",
        "P_ML",
        "P_E",
        "P_W",
    }


def test_tick_with_no_observations_reports_failsafe() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    state = make_state("P_N", 0.0)

    result = layer.tick(now=5.0, state=state)

    # No phase has ever reported -> total data loss.
    assert result.degradation == DegradationMode.FAILSAFE
    assert result.feedback == FeedbackStatus.IDLE
    assert result.lane_states == {}


def test_tick_aggregates_observations_into_phase_demand() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    state = make_state("P_N", 0.0)

    observations = [
        VehicleObservation(
            track_id=1, lane_id="east_through", observed_at=4.0, speed_mps=0.1
        ),
        VehicleObservation(
            track_id=2, lane_id="east_through", observed_at=4.5, speed_mps=0.2
        ),
        VehicleObservation(
            track_id=3, lane_id="east_left", observed_at=3.0, speed_mps=0.0
        ),
    ]

    result = layer.tick(now=5.0, state=state, observations=observations)

    # P_E merges east_through + east_left: 3 vehicles, oldest wait 2 s.
    assert result.demands["P_E"].vehicle_count == 3
    assert result.demands["P_E"].oldest_wait_s == pytest.approx(2.0)
    assert result.demands["P_E"].downstream_blocked is False


def test_tick_preempts_a_deadline_direction_from_observations() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    # Elapsed 20 s: past the minimum green, before the maximum green.
    state = make_state("P_N", 100.0)

    # All four approaches report, so no direction is "never seen".
    observations = [
        VehicleObservation(
            track_id=1, lane_id="north_through", observed_at=119.0, speed_mps=0.0
        ),
        VehicleObservation(
            track_id=2, lane_id="east_through", observed_at=119.0, speed_mps=0.0
        ),
        VehicleObservation(
            track_id=3, lane_id="south_through", observed_at=119.0, speed_mps=0.0
        ),
        # The oldest vehicle on west_through arrived 120 s before now.
        VehicleObservation(
            track_id=4, lane_id="west_through", observed_at=0.0, speed_mps=0.0
        ),
    ]

    result = layer.tick(now=120.0, state=state, observations=observations)

    assert result.demands["P_W"].oldest_wait_s == pytest.approx(120.0)
    assert result.demands["P_N"].oldest_wait_s == pytest.approx(1.0)
    assert result.decision.selected_phase_id == "P_W"
    assert result.decision.reason == "p0_wait_preempt"


def test_stale_direction_keeps_ageing_and_is_eventually_served() -> None:
    """A dead perception feed must not starve a direction forever."""
    layer = DecisionLayer.from_yaml(DEFAULT)
    approaches = [
        "north_through",
        "east_through",
        "south_through",
        "west_through",
    ]

    # t=10: every approach reports.
    layer.tick(
        now=10.0,
        state=make_state("P_N", 0.0),
        observations=[
            VehicleObservation(
                track_id=index,
                lane_id=lane,
                observed_at=9.0,
                speed_mps=0.0,
            )
            for index, lane in enumerate(approaches, start=1)
        ],
    )

    # t=130: three approaches still report; west_through is gone (dead feed).
    result = layer.tick(
        now=130.0,
        state=make_state("P_N", 120.0),  # elapsed 10 s
        observations=[
            VehicleObservation(
                track_id=10 + index,
                lane_id=lane,
                observed_at=129.0,
                speed_mps=0.0,
            )
            for index, lane in enumerate(approaches[:3])
        ],
    )

    # Memory says west_through last reported at t=10 -> 120 s conservative wait.
    assert result.demands["P_W"].oldest_wait_s == pytest.approx(120.0)
    assert result.degradation == DegradationMode.PARTIAL
    assert result.decision.selected_phase_id == "P_W"
    assert result.decision.reason == "p0_wait_preempt"


def test_downstream_none_blocks_the_direction() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    state = make_state("P_N", 0.0)

    result = layer.tick(
        now=20.0,
        state=state,
        downstream_occupancy={"west_through": None},
    )

    assert result.downstream_blocked["P_W"] is True


def test_downstream_hysteresis_holds_until_released() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    state = make_state("P_N", 0.0)

    blocked = layer.tick(
        now=20.0, state=state, downstream_occupancy={"west_through": 0.9}
    )
    assert blocked.downstream_blocked["P_W"] is True

    # 0.6 is inside the hysteresis band -> stays blocked.
    still = layer.tick(
        now=22.0, state=state, downstream_occupancy={"west_through": 0.6}
    )
    assert still.downstream_blocked["P_W"] is True

    # 0.5 == o_low -> released.
    released = layer.tick(
        now=24.0, state=state, downstream_occupancy={"west_through": 0.5}
    )
    assert released.downstream_blocked["P_W"] is False


def test_green_wave_flag_is_passed_through() -> None:
    layer = DecisionLayer.from_yaml(SIMULATION)
    state = make_state("P_MT", 0.0)

    result = layer.tick(
        now=5.0, state=state, green_wave_active={"P_MT": True}
    )

    assert result.demands["P_MT"].green_wave_active is True


def test_tick_is_deterministic_for_the_same_input() -> None:
    first = DecisionLayer.from_yaml(DEFAULT)
    second = DecisionLayer.from_yaml(DEFAULT)
    observations = [
        VehicleObservation(
            track_id=5, lane_id="north_left", observed_at=4.0, speed_mps=0.3
        )
    ]

    a = first.tick(
        now=5.0, state=make_state("P_E", 0.0), observations=observations
    )
    b = second.tick(
        now=5.0, state=make_state("P_E", 0.0), observations=observations
    )

    assert a.decision.reason == b.decision.reason
    assert a.decision.selected_phase_id == b.decision.selected_phase_id
    assert a.demands == b.demands
