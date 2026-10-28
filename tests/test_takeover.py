from traffic_control.decision_layer import DecisionLayer
from traffic_control.degradation import DegradationMode
from traffic_control.models import (
    ControllerMode,
    ControllerState,
)
from traffic_control.startup import StartupStage
from traffic_control.telemetry import (
    AlarmType,
    CollectingTelemetrySink,
)

DEFAULT = "configs/default.yaml"


def make_state(phase_id: str = "P_N", started_at: float = 0.0) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase_id,
        phase_started_at=started_at,
    )


# -- takeover / release ----------------------------------------------------
def test_takeover_makes_layer_silent() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    layer.takeover(10.0)

    result = layer.tick(10.0, make_state())

    assert layer.manual_override is True
    assert result.decision.selected_phase_id is None
    assert result.decision.reason == "manual_override"


def test_release_enters_startup_sequence_and_recovers() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    layer.takeover(10.0)
    layer.release(20.0)

    # 1 s after release -> yellow flash (5 s).
    flash = layer.tick(21.0, make_state())
    assert flash.decision.reason == "startup_sequence"
    assert flash.startup is StartupStage.FLASH

    # 7 s after release -> all-red (10 s).
    red = layer.tick(27.0, make_state())
    assert red.decision.reason == "startup_sequence"
    assert red.startup is StartupStage.ALL_RED

    # 16 s after release -> sequence done. With fresh observations the layer
    # must resume normal decision-making (not FAILSAFE, not startup).
    from traffic_control.models import VehicleObservation

    fresh = [
        VehicleObservation(
            track_id=30 + i, lane_id=lane, observed_at=36.0, speed_mps=0.0
        )
        for i, lane in enumerate(
            ["north_through", "east_through", "south_through", "west_through"], 1
        )
    ]
    after = layer.tick(36.0, make_state("P_N", 20.0), observations=fresh)
    assert after.startup is None
    assert after.decision.reason != "startup_sequence"
    assert after.decision.reason != "failsafe_hold"


def test_release_resets_in_memory_state() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    # Prime the layer with some history.
    from traffic_control.models import VehicleObservation

    layer.tick(
        10.0,
        make_state(),
        observations=[
            VehicleObservation(
                track_id=1,
                lane_id="north_through",
                observed_at=9.0,
                speed_mps=0.0,
            )
        ],
    )
    assert layer._last_seen  # internal state exists

    layer.takeover(20.0)
    layer.release(30.0)

    assert layer._last_seen == {}
    assert layer._last_mode is None


# -- data watchdog / failsafe ---------------------------------------------
def test_failsafe_hands_over_control() -> None:
    layer = DecisionLayer.from_yaml(DEFAULT)
    # No observation ever arrives -> every feed is dead -> FAILSAFE.
    result = layer.tick(100.0, make_state())

    assert result.degradation is DegradationMode.FAILSAFE
    assert result.decision.selected_phase_id is None
    assert result.decision.reason == "failsafe_hold"
    assert result.decision.feasible is False


def test_failsafe_alarm_is_emitted_once_per_transition() -> None:
    sink = CollectingTelemetrySink()
    layer = DecisionLayer.from_yaml(DEFAULT, sink=sink)

    layer.tick(100.0, make_state())  # enters FAILSAFE -> one alarm
    layer.tick(101.0, make_state())  # still FAILSAFE -> no new alarm
    layer.tick(102.0, make_state())

    failsafe_alarms = [
        a for a in sink.alarms if a.alarm_type is AlarmType.FAILSAFE
    ]
    assert len(failsafe_alarms) == 1


def test_takeover_and_release_emit_alarms() -> None:
    sink = CollectingTelemetrySink()
    layer = DecisionLayer.from_yaml(DEFAULT, sink=sink)

    layer.takeover(10.0)
    layer.release(20.0)

    types = [a.alarm_type for a in sink.alarms]
    assert AlarmType.TAKEOVER in types
    assert AlarmType.RELEASE in types
    assert AlarmType.STARTUP_SEQUENCE in types


def test_default_sink_is_noop() -> None:
    # Constructing without a sink must work and must not require network.
    layer = DecisionLayer.from_yaml(DEFAULT)
    result = layer.tick(100.0, make_state())
    assert result.degradation is DegradationMode.FAILSAFE
