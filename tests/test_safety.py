from traffic_control.models import (
    ControllerMode,
    ControllerState,
    SignalPhase,
)
from traffic_control.safety import SafetyGuard


def make_guard() -> SafetyGuard:
    phases = {
        "NS": SignalPhase(
            phase_id="NS",
            movement_ids=frozenset({"north_south"}),
            min_green_s=10,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        ),
        "EW": SignalPhase(
            phase_id="EW",
            movement_ids=frozenset({"east_west"}),
            min_green_s=10,
            max_green_s=60,
            yellow_s=3,
            all_red_s=1,
        ),
    }
    return SafetyGuard(phases)


def test_rejects_switch_before_minimum_green() -> None:
    guard = make_guard()
    state = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="NS",
        phase_started_at=100,
    )

    result = guard.check_candidate("EW", state, now=105)

    assert result.allowed is False
    assert "minimum_green_not_met" in result.reasons


def test_rejects_candidate_in_fault_mode() -> None:
    guard = make_guard()
    state = ControllerState(
        mode=ControllerMode.FAULT,
        current_phase_id="NS",
        phase_started_at=100,
    )

    result = guard.check_candidate("EW", state, now=120)

    assert result.allowed is False
    assert "controller_in_fault_mode" in result.reasons


def test_rejects_conflicting_candidate() -> None:
    guard = make_guard()
    state = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="NS",
        phase_started_at=100,
    )

    result = guard.check_candidate(
        "EW",
        state,
        now=120,
        conflicting_phase_ids={"EW"},
    )

    assert result.allowed is False
    assert "candidate_conflicts_with_current_phase" in result.reasons


def test_allows_candidate_after_minimum_green() -> None:
    guard = make_guard()
    state = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="NS",
        phase_started_at=100,
    )

    result = guard.check_candidate("EW", state, now=110)

    assert result.allowed is True
    assert result.reasons == ()