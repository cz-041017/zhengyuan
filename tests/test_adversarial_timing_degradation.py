"""回归测试：配时可追溯性与 P0 结构性保证（漏洞3/5，已修复）。

- 漏洞5：仿真占位配时曾为 max_green=60 → cycle=256s、worst_wait=196s>130s，
  P0 130s 保证结构上依赖抢占。SIM_MAX_GREEN_S 现为 80s：4 相位 worst_wait =
  4*(80+4)-80 = 256s > 130s，P0 必然依赖抢占。要恢复"结构性保证"，必须给
  signal.phases 配差异化 max_green（例如主路 80s + 支路 20~30s）。
- 漏洞3：conservative_oldest_wait 的 never-observed 分支曾无界返回 now；
  修复后按 max_wait_s（默认 130）封顶。
"""
from __future__ import annotations

from traffic_control.degradation import conservative_oldest_wait
from traffic_control.models import (
    SIM_ALL_RED_S,
    SIM_MAX_GREEN_S,
    SIM_MIN_GREEN_S,
    SIM_YELLOW_S,
    SignalPhase,
)
from traffic_control.timing_analysis import analyze_timing


def _sim_placeholder_phases() -> dict[str, SignalPhase]:
    """与 configs/*.yaml signal.phases=[] 时 runtime.build_phases 的占位值一致。"""
    return {
        "P_N": SignalPhase(
            "P_N", frozenset({"north_through", "north_left"}),
            SIM_MIN_GREEN_S, SIM_MAX_GREEN_S, SIM_YELLOW_S, SIM_ALL_RED_S,
        ),
        "P_E": SignalPhase(
            "P_E", frozenset({"east_through", "east_left"}),
            SIM_MIN_GREEN_S, SIM_MAX_GREEN_S, SIM_YELLOW_S, SIM_ALL_RED_S,
        ),
        "P_S": SignalPhase(
            "P_S", frozenset({"south_through", "south_left"}),
            SIM_MIN_GREEN_S, SIM_MAX_GREEN_S, SIM_YELLOW_S, SIM_ALL_RED_S,
        ),
        "P_W": SignalPhase(
            "P_W", frozenset({"west_through", "west_left"}),
            SIM_MIN_GREEN_S, SIM_MAX_GREEN_S, SIM_YELLOW_S, SIM_ALL_RED_S,
        ),
    }


def test_sim_placeholder_timing_under_80s_relies_on_preemption() -> None:
    """80s 占位配时下，4 相位 worst_wait 超过 130s，P0 必然依赖抢占。

    这是 SIM_MAX_GREEN_S=80 的既定取舍：用更长的单相位绿灯换效率，
    代价是其他方向的最坏等待超过 130s，由 P0 硬抢占兜底。
    """
    analysis = analyze_timing(_sim_placeholder_phases(), max_wait_target_s=130.0)

    # 4 * (80 + 4) - 80 = 256 > 130 -> 每个相位都需要 P0 兜底
    assert analysis.worst_wait_s["P_N"] == 256.0
    assert analysis.preempt_required == frozenset(
        {"P_N", "P_E", "P_S", "P_W"}
    )
    assert "preempt_load_bearing" in analysis.reasons


def test_never_observed_wait_is_bounded() -> None:
    """never-observed 方向的等待值必须有界（≤ P0 截止 130s）。"""
    wait = conservative_oldest_wait(
        now=3600.0, last_seen_at=None, observed_wait_s=0.0, timeout_s=3.0
    )

    assert wait <= 130.0
