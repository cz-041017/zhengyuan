"""回归测试：首次上电执行启动序列（漏洞8，已修复）。

DecisionLayer.from_yaml 构造后首次 tick 现在进入启动序列（5s 黄闪 + 10s 全红），
并 emit STARTUP_SEQUENCE 告警，符合 GB 25280 首次上电安全要求。
"""
from __future__ import annotations

from pathlib import Path

from traffic_control.decision_layer import DecisionLayer
from traffic_control.models import ControllerMode, ControllerState
from traffic_control.startup import StartupStage
from traffic_control.telemetry import AlarmType, CollectingTelemetrySink

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT = PROJECT_ROOT / "configs" / "default.yaml"


def make_state(phase_id: str, started_at: float) -> ControllerState:
    return ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id=phase_id,
        phase_started_at=started_at,
    )


def test_first_power_on_runs_startup_sequence() -> None:
    """首次上电应执行 5s 黄闪 + 10s 全红。"""
    sink = CollectingTelemetrySink()
    layer = DecisionLayer.from_yaml(DEFAULT, sink=sink)

    result = layer.tick(now=0.0, state=make_state("P_N", 0.0))

    assert result.startup == StartupStage.FLASH
    assert any(
        a.alarm_type == AlarmType.STARTUP_SEQUENCE for a in sink.alarms
    ), "首次上电未发出 STARTUP_SEQUENCE 告警"
