from pathlib import Path

import pytest

from traffic_control.config_loader import load_config
from traffic_control.degradation import DegradationMode
from traffic_control.feedback import FeedbackStatus
from traffic_control.models import ControllerMode, ControllerState
from traffic_control.timing_analysis import analyze_timing
from traffic_control.runtime import (
    build_conflict_matrix,
    build_machine_config,
    build_phase_groups,
    build_runtime,
    build_score_config,
)
from traffic_control.state_machine import PhaseDemand


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT = PROJECT_ROOT / "configs" / "default.yaml"
SIMULATION = PROJECT_ROOT / "configs" / "simulation.yaml"


def test_build_score_config_defaults() -> None:
    cfg = build_score_config(load_config(DEFAULT))

    assert cfg.max_wait_target_s == 120.0
    assert cfg.wait_urgency_start_s == 80.0
    assert cfg.wait_urgency_max_score == 5.0
    assert cfg.green_wave_score == 2.0
    assert cfg.o_high == 0.8
    assert cfg.o_low == 0.5
    assert cfg.spillback_penalty == -1000.0


def test_build_machine_config_defaults() -> None:
    cfg = build_machine_config(load_config(DEFAULT))

    assert cfg.countdown_duration_s == 15.0
    assert cfg.switch_debounce_s == 3.0
    assert cfg.score_hysteresis == 0.1


def test_build_phase_groups_minor_only() -> None:
    groups = build_phase_groups(load_config(DEFAULT))

    assert [g.phase_id for g in groups] == ["P_N", "P_E", "P_S", "P_W"]


def test_build_phase_groups_main_road() -> None:
    groups = build_phase_groups(load_config(SIMULATION))

    assert [g.phase_id for g in groups] == ["P_MT", "P_ML", "P_E", "P_W"]
    assert groups[0].green_wave_eligible is True


def test_build_conflict_matrix() -> None:
    matrix = build_conflict_matrix(load_config(DEFAULT))

    assert matrix.are_conflicting("P_N", "P_S") is True
    assert matrix.are_conflicting("P_N", "P_N") is False


def test_build_runtime_cross_checks_match() -> None:
    minor = build_runtime(load_config(DEFAULT))
    assert {g.phase_id for g in minor.phase_groups} == {
        "P_N",
        "P_E",
        "P_S",
        "P_W",
    }

    main_road = build_runtime(load_config(SIMULATION))
    assert {g.phase_id for g in main_road.phase_groups} == {
        "P_MT",
        "P_ML",
        "P_E",
        "P_W",
    }


def test_build_runtime_rejects_matrix_layout_mismatch() -> None:
    config = load_config(DEFAULT)
    config["signal"]["conflict_matrix"] = {
        "P_A": ["P_B"],
        "P_B": ["P_A"],
    }

    with pytest.raises(ValueError, match="do not match"):
        build_runtime(config)


def test_build_phases_uses_placeholders_when_empty() -> None:
    runtime = build_runtime(load_config(DEFAULT))

    assert set(runtime.phases) == {"P_N", "P_E", "P_S", "P_W"}
    assert runtime.phases["P_N"].min_green_s == 10.0


def test_build_phases_rejects_layout_mismatch() -> None:
    config = load_config(DEFAULT)
    config["signal"]["phases"] = [
        {
            "phase_id": "P_X",
            "movement_ids": ["north_through", "north_left"],
            "min_green_s": 10,
            "max_green_s": 60,
            "yellow_s": 3,
            "all_red_s": 1,
        }
    ]

    with pytest.raises(ValueError, match="do not match"):
        build_runtime(config)


def test_runtime_assembles_and_decides() -> None:
    runtime = build_runtime(load_config(SIMULATION))

    state = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="P_MT",
        phase_started_at=0.0,
    )
    demands = {
        "P_MT": PhaseDemand(green_wave_active=True),
        "P_ML": PhaseDemand(),
        "P_E": PhaseDemand(),
        "P_W": PhaseDemand(oldest_wait_s=120.0),
    }

    decision = runtime.state_machine.tick(state, 12.0, demands)

    assert decision.selected_phase_id == "P_W"
    assert decision.reason == "p0_wait_preempt"


def test_runtime_includes_degradation_controller() -> None:
    runtime = build_runtime(load_config(DEFAULT))
    phases = {g.phase_id for g in runtime.phase_groups}

    now = 10.0
    all_fresh = {phase_id: 9.0 for phase_id in phases}
    assert (
        runtime.degradation.decide(now, phases, all_fresh)
        == DegradationMode.NORMAL
    )

    all_stale = {phase_id: 5.0 for phase_id in phases}
    assert (
        runtime.degradation.decide(now, phases, all_stale)
        == DegradationMode.FAILSAFE
    )


def test_runtime_timing_analysis_feasible() -> None:
    runtime = build_runtime(load_config(DEFAULT))

    analysis = analyze_timing(
        runtime.phases, runtime.score_config.max_wait_target_s
    )

    # Placeholder timings: min green 10 + clearance 4 = 14 < 120 -> feasible.
    assert analysis.feasible_120s is True


def test_runtime_includes_feedback_monitor() -> None:
    from traffic_control.models import PhaseDecision

    runtime = build_runtime(load_config(DEFAULT))

    state = ControllerState(
        mode=ControllerMode.NORMAL,
        current_phase_id="P_N",
        phase_started_at=0.0,
    )
    decision = PhaseDecision(selected_phase_id="P_S", reason="test")

    status = runtime.feedback_monitor.observe(decision, state, 0.0)
    assert status == FeedbackStatus.PENDING
