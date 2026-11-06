"""回归测试：执行反馈失败必须升级为 FEEDBACK_TIMEOUT 告警（漏洞4，已修复）。

DecisionLayer.tick() 检测到 FeedbackStatus.FAILURE 时必须 emit
AlarmType.FEEDBACK_TIMEOUT，否则反馈超时故障会静默丢失。
"""
from __future__ import annotations

from pathlib import Path

from traffic_control.decision_layer import DecisionLayer
from traffic_control.feedback import FeedbackStatus
from traffic_control.models import ControllerMode, ControllerState, VehicleObservation
from traffic_control.telemetry import AlarmType, CollectingTelemetrySink

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT = PROJECT_ROOT / "configs" / "default.yaml"


def make_state(phase_id: str, started_at: float) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase_id,
        phase_started_at=started_at,
    )


def _three_approaches_observed(now: float) -> list[VehicleObservation]:
    """north/east/south 报告，west 缺席（dead feed → 保守等待 = now）。"""
    return [
        VehicleObservation(
            track_id=1, lane_id="north_through", observed_at=now - 1.0, speed_mps=0.0
        ),
        VehicleObservation(
            track_id=2, lane_id="east_through", observed_at=now - 1.0, speed_mps=0.0
        ),
        VehicleObservation(
            track_id=3, lane_id="south_through", observed_at=now - 1.0, speed_mps=0.0
        ),
    ]


def test_feedback_failure_emits_feedback_timeout_alarm(boot) -> None:
    """feedback FAILURE 必须升级为 FEEDBACK_TIMEOUT 告警。"""
    sink = CollectingTelemetrySink()
    layer = DecisionLayer.from_yaml(DEFAULT, sink=sink)
    boot(layer, now=130.0)

    # t=130：west 缺席（dead），保守等待 = now = 130 触发 P0 抢占 → propose P_W。
    r1 = layer.tick(
        now=130.0,
        state=make_state("P_N", 100.0),
        observations=_three_approaches_observed(130.0),
    )
    assert r1.decision.selected_phase_id == "P_W"
    assert r1.decision.reason == "p0_wait_preempt"
    assert r1.feedback == FeedbackStatus.PENDING

    # t=145：控制器仍未执行 → 距首次 propose 15s > confirm_timeout 10s → FAILURE。
    r2 = layer.tick(
        now=145.0,
        state=make_state("P_N", 100.0),
        observations=_three_approaches_observed(145.0),
    )
    assert r2.feedback == FeedbackStatus.FAILURE

    assert any(
        a.alarm_type == AlarmType.FEEDBACK_TIMEOUT for a in sink.alarms
    ), "反馈失败未升级为 FEEDBACK_TIMEOUT 告警"
