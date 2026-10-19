import pytest

from traffic_control.feedback import FeedbackMonitor, FeedbackStatus
from traffic_control.models import ControllerMode, ControllerState, PhaseDecision


def make_state(phase_id, mode=ControllerMode.NORMAL):
    return ControllerState(
        mode=mode, current_phase_id=phase_id, phase_started_at=0.0
    )


def make_decision(target=None):
    return PhaseDecision(selected_phase_id=target, reason="test")


def test_idle_without_proposal() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=10.0)

    assert (
        monitor.observe(make_decision(None), make_state("NS"), 0.0)
        == FeedbackStatus.IDLE
    )


def test_pending_right_after_proposal() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=10.0)

    assert (
        monitor.observe(make_decision("EW"), make_state("NS"), 0.0)
        == FeedbackStatus.PENDING
    )


def test_pending_during_clearance() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=10.0)
    monitor.observe(make_decision("EW"), make_state("NS"), 0.0)

    clearance = make_state("NS", mode=ControllerMode.CLEARANCE)
    assert (
        monitor.observe(make_decision(None), clearance, 1.0)
        == FeedbackStatus.PENDING
    )


def test_confirmed_when_target_reached() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=10.0)
    monitor.observe(make_decision("EW"), make_state("NS"), 0.0)

    assert (
        monitor.observe(make_decision(None), make_state("EW"), 2.0)
        == FeedbackStatus.CONFIRMED
    )


def test_failure_on_timeout() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=5.0)
    monitor.observe(make_decision("EW"), make_state("NS"), 0.0)

    # Still on NS after the timeout with no clearance: execution failure.
    assert (
        monitor.observe(make_decision(None), make_state("NS"), 6.0)
        == FeedbackStatus.FAILURE
    )


def test_failure_on_controller_fault() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=10.0)
    monitor.observe(make_decision("EW"), make_state("NS"), 0.0)

    fault = make_state("NS", mode=ControllerMode.FAULT)
    assert (
        monitor.observe(make_decision(None), fault, 1.0)
        == FeedbackStatus.FAILURE
    )


def test_failure_on_unexpected_phase() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=10.0)
    monitor.observe(make_decision("EW"), make_state("NS"), 0.0)

    # Controller landed on a phase we did not request.
    assert (
        monitor.observe(make_decision(None), make_state("P_S"), 1.0)
        == FeedbackStatus.FAILURE
    )


def test_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="confirm_timeout_s"):
        FeedbackMonitor(confirm_timeout_s=0.0)


def test_timeout_fires_when_same_target_is_reproposed_every_tick() -> None:
    """Regression: a non-executing controller must still be detected.

    The state machine re-issues a pending preemption or max-green switch on
    every tick, so the confirmation clock must not restart on a re-proposal
    of the same target.
    """
    monitor = FeedbackMonitor(confirm_timeout_s=5.0)

    for t in (0.0, 1.0, 2.0, 3.0, 4.0):
        assert (
            monitor.observe(make_decision("EW"), make_state("NS"), t)
            == FeedbackStatus.PENDING
        )

    # 6 s after the first proposal, while still re-proposing the same target.
    assert (
        monitor.observe(make_decision("EW"), make_state("NS"), 6.0)
        == FeedbackStatus.FAILURE
    )


def test_retarget_rearms_the_confirmation_clock() -> None:
    monitor = FeedbackMonitor(confirm_timeout_s=5.0)
    monitor.observe(make_decision("EW"), make_state("NS"), 0.0)

    # Same target: the clock is untouched, so 5.5 s is past the deadline.
    assert (
        monitor.observe(make_decision("EW"), make_state("NS"), 5.5)
        == FeedbackStatus.FAILURE
    )

    # A different target re-arms the clock from scratch.
    assert (
        monitor.observe(make_decision("P_S"), make_state("NS"), 5.5)
        == FeedbackStatus.PENDING
    )
    assert (
        monitor.observe(make_decision("P_S"), make_state("NS"), 9.0)
        == FeedbackStatus.PENDING
    )
