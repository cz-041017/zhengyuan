from traffic_control.models import SignalPhase
from traffic_control.timing_analysis import analyze_timing


def make_phases(min_green=10.0, max_green=60.0, yellow=3.0, all_red=1.0):
    return {
        phase_id: SignalPhase(
            phase_id=phase_id,
            movement_ids=frozenset({phase_id}),
            min_green_s=min_green,
            max_green_s=max_green,
            yellow_s=yellow,
            all_red_s=all_red,
        )
        for phase_id in ("P_N", "P_E", "P_S", "P_W")
    }


def test_feasible_when_switch_overhead_below_deadline() -> None:
    analysis = analyze_timing(make_phases(), 120.0)

    assert analysis.feasible_120s is True
    # min green 10 + yellow 3 + all-red 1 = 14
    assert analysis.max_time_to_green_s == 14.0


def test_infeasible_when_min_green_plus_clearance_reaches_deadline() -> None:
    # min green 120 + clearance 4 = 124 >= 120: even immediate preemption
    # cannot serve a direction within the deadline.
    analysis = analyze_timing(
        make_phases(min_green=120.0, max_green=120.0), 120.0
    )

    assert analysis.feasible_120s is False
    assert "switch_overhead_exceeds_deadline" in analysis.reasons


def test_worst_wait_without_preemption() -> None:
    analysis = analyze_timing(make_phases(), 120.0)

    # cycle = 4 * (60 + 4) = 256; worst red time = 256 - 60 = 196 s.
    assert analysis.worst_wait_s["P_N"] == 196.0
    assert analysis.preempt_required == frozenset(
        {"P_N", "P_E", "P_S", "P_W"}
    )


def test_no_preempt_required_when_cycle_short() -> None:
    # cycle = 4 * (20 + 4) = 96; worst red time = 96 - 20 = 76 < 120.
    analysis = analyze_timing(make_phases(max_green=20.0), 120.0)

    assert analysis.preempt_required == frozenset()
    assert analysis.feasible_120s is True


def test_rejects_empty_phases() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least one phase"):
        analyze_timing({}, 120.0)


def test_rejects_non_positive_deadline() -> None:
    import pytest

    with pytest.raises(ValueError, match="max_wait_target_s"):
        analyze_timing(make_phases(), 0.0)
