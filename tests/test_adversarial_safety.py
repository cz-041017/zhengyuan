"""回归测试：安全互锁纵深防御（漏洞1/2，已修复）。

SafetyGuard 现在从构造时注入的冲突图推导冲突集，即使调用方不传
conflicting_phase_ids，也能拒绝与当前相位冲突的候选（纵深防御）。
"""
from __future__ import annotations

from traffic_control.models import ControllerMode, ControllerState, SignalPhase
from traffic_control.phasing import ConflictMatrix
from traffic_control.safety import SafetyGuard

_CONFLICTS = {
    "P_N": frozenset({"P_E", "P_S", "P_W"}),
    "P_E": frozenset({"P_N", "P_S", "P_W"}),
    "P_S": frozenset({"P_N", "P_E", "P_W"}),
    "P_W": frozenset({"P_N", "P_E", "P_S"}),
}


def _phases() -> dict[str, SignalPhase]:
    return {
        "P_N": SignalPhase(
            "P_N", frozenset({"north_through", "north_left"}), 12.0, 60.0, 3.0, 1.0
        ),
        "P_E": SignalPhase(
            "P_E", frozenset({"east_through", "east_left"}), 12.0, 60.0, 3.0, 1.0
        ),
        "P_S": SignalPhase(
            "P_S", frozenset({"south_through", "south_left"}), 12.0, 60.0, 3.0, 1.0
        ),
        "P_W": SignalPhase(
            "P_W", frozenset({"west_through", "west_left"}), 12.0, 60.0, 3.0, 1.0
        ),
    }


def test_guard_rejects_conflicting_candidate_without_explicit_ids() -> None:
    """调用方不传 conflicting_phase_ids 时，guard 仍用注入的冲突图拒绝冲突候选。"""
    guard = SafetyGuard(_phases(), conflicts=_CONFLICTS)
    state = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="P_N",
        phase_started_at=100.0,
    )

    # P_N 与 P_S 冲突；elapsed = 120 - 100 = 20 >= min_green(12)。
    result = guard.check_candidate("P_S", state, now=120.0)

    assert result.allowed is False
    assert "candidate_conflicts_with_current_phase" in result.reasons


def test_conflict_matrix_semantics_are_correct() -> None:
    """基线：矩阵本身判冲突正确。"""
    matrix = ConflictMatrix(_CONFLICTS)
    assert matrix.are_conflicting("P_N", "P_S") is True
    assert matrix.are_conflicting("P_N", "P_N") is False
    assert matrix.conflicts_of("P_N") == frozenset({"P_E", "P_S", "P_W"})
