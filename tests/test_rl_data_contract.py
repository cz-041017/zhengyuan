"""Phase 4 验收：RL 数据预留（docs/logging-data-contract.md §8）。

不开发导出工具，只验证「记录的数据足够离线回填 reward、足以构成 (s,a,r)
三元组」——即预留是完整可用的，不是只有个空壳。
"""
from __future__ import annotations

import json

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
from traffic_control.structured_log import build_tick_snapshot


def _snapshot() -> dict:
    controller = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="P_N",
        phase_started_at=10.0,
        signal_colors={"north_through": SignalColor.GREEN},
    )
    result = LayerResult(
        now=0.0,
        decision=PhaseDecision(
            selected_phase_id="P_E",
            reason="countdown_complete",
            scores={"P_N": 1.2, "P_E": 1.5},
            constraints_applied=("hysteresis", "countdown"),
            feasible=True,
        ),
        degradation=DegradationMode.NORMAL,
        feedback=FeedbackStatus.PENDING,
        demands={
            "P_N": PhaseDemand(
                vehicle_count=3, oldest_wait_s=12.0, downstream_blocked=False
            )
        },
        lane_states={
            "north_through": LaneState(
                lane_id="north_through",
                vehicle_count=2,
                queue_length_m=14.0,
                oldest_wait_s=8.0,
                downstream_blocked=False,
                last_updated_at=1.0,
            )
        },
        downstream_blocked={"north_through": False},
    )
    return build_tick_snapshot(
        tick_id=7,
        ts_wall=100.0,
        ts_mono=50.0,
        boot_id="boot-1",
        junction_id="panan_1",
        config_hash="sha256:ab12cd34ef123456",
        controller_state=controller,
        result=result,
    )


def test_rl_triple_skeleton_present() -> None:
    snap = _snapshot()
    assert "state" in snap and "action" in snap and "reward" in snap
    assert snap["reward"] is None  # reserved for offline backfill


def test_offline_reward_inputs_are_recorded() -> None:
    snap = _snapshot()
    lane = snap["state"]["lane_states"]["north_through"]
    # 离线 reward 所需指标必须已落盘（§8.2）。
    assert lane["oldest_wait_s"] == 8.0
    assert lane["queue_length_m"] == 14.0
    assert lane["vehicle_count"] == 2

    demand = snap["state"]["demands"]["P_N"]
    assert demand["oldest_wait_s"] == 12.0
    assert demand["vehicle_count"] == 3

    assert snap["state"]["downstream_blocked"] == {"north_through": False}
    assert snap["state"]["controller"]["current_phase_id"] == "P_N"
    assert snap["state"]["controller"]["phase_started_at"] == 10.0


def test_action_carries_policy_metadata() -> None:
    snap = _snapshot()
    assert snap["action"]["selected_phase_id"] == "P_E"
    assert snap["action"]["scores"] == {"P_N": 1.2, "P_E": 1.5}
    assert "hysteresis" in snap["action"]["constraints_applied"]
    assert snap["action"]["feasible"] is True


def test_trajectory_identity_fields() -> None:
    snap = _snapshot()
    assert snap["junction_id"] == "panan_1"
    assert snap["boot_id"] == "boot-1"
    assert snap["tick_id"] == 7
    assert snap["config_hash"].startswith("sha256:")


def test_snapshot_roundtrips_as_json() -> None:
    snap = _snapshot()
    back = json.loads(json.dumps(snap, sort_keys=True))
    assert back["state"]["lane_states"]["north_through"]["queue_length_m"] == 14.0
    assert back["action"]["selected_phase_id"] == "P_E"
    assert back["reward"] is None
