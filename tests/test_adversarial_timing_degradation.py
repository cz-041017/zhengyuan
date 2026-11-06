"""回归测试：配时可追溯性与 P0 结构性保证（漏洞3/5，已修复）。

- 漏洞5：仿真占位配时原为 max_green=60 → cycle=256s、worst_wait=196s>130s，
  P0 130s 保证结构上依赖抢占。修复=占位配时统一用 SIM_MAX_GREEN_S=30，
  worst_wait=3*30+16=106<130，保证变为结构性（preempt_required 为空）。
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


def test_sim_placeholder_timing_is_not_preempt_load_bearing() -> None:
    """占位配时不得让 P0 130s 保证退化为"纯靠抢占"。"""
    analysis = analyze_timing(_sim_placeholder_phases(), max_wait_target_s=130.0)

    assert analysis.preempt_required == frozenset()
    assert "preempt_load_bearing" not in analysis.reasons
    assert analysis.worst_wait_s["P_N"] < 130.0


def test_never_observed_wait_is_bounded() -> None:
    """never-observed 方向的等待值必须有界（≤ P0 截止 130s）。"""
    wait = conservative_oldest_wait(
        now=3600.0, last_seen_at=None, observed_wait_s=0.0, timeout_s=3.0
    )

    assert wait <= 130.0
