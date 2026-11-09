from __future__ import annotations

import json
from pathlib import Path

from traffic_control.decision_layer import LayerResult
from traffic_control.degradation import DegradationMode
from traffic_control.feedback import FeedbackStatus
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    LaneState,
    PhaseDecision,
    SignalColor,
)
from traffic_control.state_machine import PhaseDemand
from traffic_control.startup import StartupStage
from traffic_control.structured_log import (
    EVENT_ALARM,
    EVENT_DECISION_CHANGE,
    EVENT_DEGRADATION_CHANGE,
    EVENT_FEEDBACK_FAILURE,
    EVENT_MODE_CHANGE,
    BlackBox,
    CloudSink,
    JsonlFileSink,
    LoggingPipeline,
    build_tick_snapshot,
    compute_config_hash,
)
from traffic_control.telemetry import AlarmEvent, AlarmLevel, AlarmType, LogEvent


def _controller(mode: ControllerMode = ControllerMode.NORMAL, phase: str | None = "P_N") -> ControllerState:
    return ControllerState(
        mode=mode,
        current_phase_id=phase,
        phase_started_at=0.0,
        signal_colors={"north_through": SignalColor.GREEN},
    )


def _result(
    phase: str | None = "P_N",
    reason: str = "green_running",
    degradation: DegradationMode = DegradationMode.NORMAL,
    feedback: FeedbackStatus = FeedbackStatus.CONFIRMED,
) -> LayerResult:
    return LayerResult(
        now=0.0,
        decision=PhaseDecision(
            selected_phase_id=phase,
            reason=reason,
            scores={"P_N": 1.0},
            constraints_applied=(),
            feasible=True,
        ),
        degradation=degradation,
        feedback=feedback,
        demands={"P_N": PhaseDemand(vehicle_count=3, oldest_wait_s=12.0)},
        lane_states={
            "north_through": LaneState(
                lane_id="north_through",
                vehicle_count=2,
                queue_length_m=14.0,
                oldest_wait_s=8.0,
                last_updated_at=1.0,
            )
        },
        downstream_blocked={},
    )


class _RecordingSink(CloudSink):
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish_event(self, event_type: str, payload) -> None:
        self.events.append((event_type, dict(payload)))

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_build_tick_snapshot_shape() -> None:
    snap = build_tick_snapshot(
        tick_id=7,
        ts_wall=100.0,
        ts_mono=50.0,
        boot_id="boot-1",
        junction_id="j1",
        config_hash="sha256:abc",
        controller_state=_controller(),
        result=_result(),
    )
    assert snap["schema_version"] == 1
    assert snap["tick_id"] == 7
    assert snap["junction_id"] == "j1"
    assert snap["reward"] is None
    assert snap["mode"] == "normal"
    assert snap["state"]["degradation"] == "normal"
    assert snap["state"]["demands"]["P_N"]["vehicle_count"] == 3
    assert snap["state"]["lane_states"]["north_through"]["queue_length_m"] == 14.0
    assert snap["state"]["controller"]["signal_colors"]["north_through"] == "green"
    assert snap["action"]["selected_phase_id"] == "P_N"
    assert snap["action"]["feedback"] == "confirmed"
    # JSON-serializable.
    json.dumps(snap)


def test_build_tick_snapshot_excludes_raw_observations() -> None:
    snap = build_tick_snapshot(
        tick_id=0, ts_wall=0.0, ts_mono=0.0, boot_id="b", junction_id="j",
        config_hash="h", controller_state=_controller(), result=_result(),
    )
    assert "observations" not in snap
    assert "track_id" not in json.dumps(snap)


def test_jsonl_sink_writes_and_reads_back() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sink = JsonlFileSink(tmp)
        sink.write({"a": 1, "b": "x"})
        sink.write({"a": 2})
        sink.close()
        lines = (Path(tmp) / "tick.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1, "b": "x"}
        assert json.loads(lines[1]) == {"a": 2}


def test_jsonl_sink_rotation_by_size(tmp_path: Path) -> None:
    sink = JsonlFileSink(tmp_path, max_bytes_per_file=40)
    for i in range(10):
        sink.write({"i": i, "pad": "x" * 10})
    sink.close()
    files = list(tmp_path.glob("tick_*.jsonl"))
    assert len(files) >= 1  # rotated at least once


def test_jsonl_sink_retention(tmp_path: Path) -> None:
    import os
    import time

    old = tmp_path / "tick_20000101_000000.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    sink = JsonlFileSink(tmp_path, retention_days=30)
    sink._last_purge = 0.0  # force purge on first write
    sink.write({"x": 1})
    sink.close()
    assert not old.exists()


def test_black_box_ring() -> None:
    bb = BlackBox(max_ticks=2)
    bb.record({"i": 1})
    bb.record({"i": 2})
    bb.record({"i": 3})
    assert len(bb) == 2
    assert [s["i"] for s in bb.snapshots()] == [2, 3]


def test_black_box_dump(tmp_path: Path) -> None:
    bb = BlackBox(max_ticks=2)
    bb.record({"i": 1})
    bb.record({"i": 2})
    out = tmp_path / "crash.jsonl"
    bb.dump(out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["i"] for l in lines] == [1, 2]


def test_pipeline_upload_triggers() -> None:
    sink = _RecordingSink()
    pipe = LoggingPipeline("j1", "boot-1", "h", cloud_sink=sink)

    # First tick publishes initial decision/degradation/mode.
    pipe.on_tick(_controller(ControllerMode.NORMAL, "P_N"), _result("P_N"), ts_wall=0.0)
    types = [e[0] for e in sink.events]
    assert EVENT_DECISION_CHANGE in types
    assert EVENT_DEGRADATION_CHANGE in types
    assert EVENT_MODE_CHANGE in types
    assert types.count(EVENT_DECISION_CHANGE) == 1

    # Same phase -> no new decision_change.
    pipe.on_tick(_controller(ControllerMode.NORMAL, "P_N"), _result("P_N"), ts_wall=1.0)
    assert sum(1 for e in sink.events if e[0] == EVENT_DECISION_CHANGE) == 1

    # Phase switch -> decision_change with from/to.
    pipe.on_tick(_controller(ControllerMode.NORMAL, "P_E"), _result("P_E"), ts_wall=2.0)
    decision_events = [e for e in sink.events if e[0] == EVENT_DECISION_CHANGE]
    assert decision_events[-1][1]["from_phase"] == "P_N"
    assert decision_events[-1][1]["to_phase"] == "P_E"

    # Degradation change -> degradation_change.
    pipe.on_tick(
        _controller(ControllerMode.NORMAL, "P_E"),
        _result("P_E", degradation=DegradationMode.PARTIAL),
        ts_wall=3.0,
    )
    deg = [e for e in sink.events if e[0] == EVENT_DEGRADATION_CHANGE][-1]
    assert deg[1]["to_mode"] == "partial"


def test_pipeline_feedback_failure_edge_triggered() -> None:
    sink = _RecordingSink()
    pipe = LoggingPipeline("j1", "boot-1", "h", cloud_sink=sink)
    pipe.on_tick(_controller(), _result(feedback=FeedbackStatus.FAILURE), ts_wall=0.0)
    pipe.on_tick(_controller(), _result(feedback=FeedbackStatus.FAILURE), ts_wall=1.0)
    pipe.on_tick(_controller(), _result(feedback=FeedbackStatus.CONFIRMED), ts_wall=2.0)
    failures = [e for e in sink.events if e[0] == EVENT_FEEDBACK_FAILURE]
    assert len(failures) == 1  # rising edge only


def test_pipeline_report_alarm() -> None:
    sink = _RecordingSink()
    pipe = LoggingPipeline("j1", "boot-1", "h", cloud_sink=sink)
    pipe.report_alarm(
        AlarmEvent(
            alarm_type=AlarmType.FAILSAFE,
            junction_id="j1",
            now=10.0,
            level=AlarmLevel.CRITICAL,
            detail={"reason": "all feeds dead"},
        )
    )
    assert sink.events[0][0] == EVENT_ALARM
    assert sink.events[0][1]["alarm_type"] == "failsafe"
    assert sink.events[0][1]["detail"] == {"reason": "all feeds dead"}


def test_pipeline_report_log_to_file(tmp_path: Path) -> None:
    sink = JsonlFileSink(tmp_path)
    pipe = LoggingPipeline("j1", "boot-1", "h", file_sink=sink)
    pipe.report_log(LogEvent(now=1.0, message="hello", junction_id="j1"))
    pipe.close()
    lines = (tmp_path / "tick.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["type"] == "log"
    assert record["message"] == "hello"


def test_compute_config_hash_stable_and_sensitive() -> None:
    a = {"signal": {"phases": [{"phase_id": "P_N", "min_green_s": 12.0}]}}
    b = {"signal": {"phases": [{"phase_id": "P_N", "min_green_s": 12.0}]}}
    c = {"signal": {"phases": [{"phase_id": "P_N", "min_green_s": 30.0}]}}
    assert compute_config_hash(a) == compute_config_hash(b)
    assert compute_config_hash(a) != compute_config_hash(c)
    assert compute_config_hash(a).startswith("sha256:")


def test_pipeline_publish_event_records_locally(tmp_path: Path) -> None:
    sink = JsonlFileSink(tmp_path)
    pipe = LoggingPipeline("j1", "boot-1", "h", file_sink=sink)
    pipe.publish_event("heartbeat_fault", {"ts_wall": 1.0, "consecutive_misses": 3})
    pipe.close()
    record = json.loads((tmp_path / "tick.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["type"] == "heartbeat_fault"
    assert record["consecutive_misses"] == 3
