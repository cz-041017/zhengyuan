from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from traffic_control.scoring import ScoreConfig


class ConfigError(ValueError):
    """Raised when project configuration is missing or invalid."""


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML configuration file and validate its basic structure."""
    config_path = Path(path)

    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML syntax: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a YAML mapping")

    validate_config(data)
    return data


def validate_config(config: dict[str, Any]) -> None:
    """Validate required fields and basic parameter ranges."""
    required_sections = (
        "project",
        "runtime",
        "queue_estimation",
        "optimization",
        "signal",
        "green_wave",
        "countdown",
        "safety",
    )

    for section in required_sections:
        if not isinstance(config.get(section), dict):
            raise ConfigError(f"Missing or invalid section: {section}")

    project = config["project"]
    if not isinstance(project.get("name"), str) or not project["name"].strip():
        raise ConfigError("project.name must be a non-empty string")
    config_version = project.get("config_version")
    if (
        isinstance(config_version, bool)
        or not isinstance(config_version, int)
        or config_version <= 0
    ):
        raise ConfigError("project.config_version must be a positive integer")

    runtime = config["runtime"]
    if runtime.get("mode") not in {"simulation", "deployment"}:
        raise ConfigError("runtime.mode must be simulation or deployment")

    _require_positive_number(
        runtime.get("observation_timeout_s"),
        "runtime.observation_timeout_s",
    )
    _require_positive_number(
        runtime.get("confirm_timeout_s"),
        "runtime.confirm_timeout_s",
    )

    queue = config["queue_estimation"]
    _require_non_negative_number(
        queue.get("queue_speed_threshold_mps"),
        "queue_estimation.queue_speed_threshold_mps",
    )
    _require_positive_number(
        queue.get("vehicle_length_m"),
        "queue_estimation.vehicle_length_m",
    )
    _require_non_negative_number(
        queue.get("stopped_gap_m"),
        "queue_estimation.stopped_gap_m",
    )
    _require_positive_number(
        queue.get("stale_after_s"),
        "queue_estimation.stale_after_s",
    )

    optimization = config["optimization"]
    _require_positive_number(
        optimization.get("max_wait_target_s"),
        "optimization.max_wait_target_s",
    )
    _require_positive_number(
        optimization.get("wait_urgency_max_score"),
        "optimization.wait_urgency_max_score",
    )
    _require_non_negative_number(
        optimization.get("green_wave_score"),
        "optimization.green_wave_score",
    )
    _require_non_negative_number(
        optimization.get("score_hysteresis"),
        "optimization.score_hysteresis",
    )
    _require_non_negative_number(
        optimization.get("switch_debounce_s"),
        "optimization.switch_debounce_s",
    )

    wait_start = optimization.get("wait_urgency_start_s")
    if (
        isinstance(wait_start, bool)
        or not isinstance(wait_start, (int, float))
        or wait_start < 0
        or wait_start >= optimization["max_wait_target_s"]
    ):
        raise ConfigError(
            "optimization.wait_urgency_start_s must be within "
            "[0, max_wait_target_s)"
        )

    demand = optimization.get("demand")
    if not isinstance(demand, dict):
        raise ConfigError("optimization.demand must be a mapping")
    for weight in ("vehicle_weight", "wait_weight"):
        value = demand.get(weight)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
        ):
            raise ConfigError(
                f"optimization.demand.{weight} must be within [0, 1]"
            )
    if (
        demand.get("vehicle_weight", 0) + demand.get("wait_weight", 0)
    ) <= 0:
        raise ConfigError("optimization.demand weights must sum positively")
    _require_positive_number(
        demand.get("n_max"), "optimization.demand.n_max"
    )

    spillback = optimization.get("spillback")
    if not isinstance(spillback, dict):
        raise ConfigError("optimization.spillback must be a mapping")
    o_high = spillback.get("o_high")
    o_low = spillback.get("o_low")
    if (
        isinstance(o_low, bool)
        or isinstance(o_high, bool)
        or not isinstance(o_low, (int, float))
        or not isinstance(o_high, (int, float))
        or not 0 <= o_low < o_high
    ):
        raise ConfigError(
            "optimization.spillback must satisfy 0 <= o_low < o_high"
        )
    penalty = spillback.get("penalty")
    if (
        isinstance(penalty, bool)
        or not isinstance(penalty, (int, float))
        or penalty >= 0
    ):
        raise ConfigError("optimization.spillback.penalty must be negative")

    priorities = optimization.get("priorities")
    expected_priorities = [
        "wait_time_guarantee",
        "downstream_spillback_prevention",
        "main_road_green_wave",
        "demand_score",
    ]
    if priorities != expected_priorities:
        raise ConfigError(
            "optimization.priorities must list the supported priorities "
            "in the configured order"
        )

    _validate_score_ordering(optimization)

    countdown = config["countdown"]
    _require_positive_number(
        countdown.get("display_duration_s"),
        "countdown.display_duration_s",
    )

    # Pedestrian guarantee is optional; defaults match runtime.build_machine_config
    # so older configs (e.g. panan/config/panan.yaml) keep loading.
    pedestrian = config.get("pedestrian", {})
    if pedestrian is None:
        pedestrian = {}
    if not isinstance(pedestrian, dict):
        raise ConfigError("pedestrian section must be a mapping")
    _require_positive_number(
        pedestrian.get("max_red_s", 150.0),
        "pedestrian.max_red_s",
    )
    _require_positive_number(
        pedestrian.get("min_green_s", 12.0),
        "pedestrian.min_green_s",
    )
    # Ordering guard: the pedestrian limit must be looser than the vehicle
    # deadline, otherwise the guarantee would preempt normal scoring forever.
    if pedestrian.get("max_red_s", 150.0) <= optimization.get(
        "max_wait_target_s", 130.0
    ):
        raise ConfigError(
            "pedestrian.max_red_s must be greater than "
            "optimization.max_wait_target_s"
        )
    _require_non_negative_number(
        pedestrian.get("yellow_s", 3.0),
        "pedestrian.yellow_s",
    )

    signal = config["signal"]
    _validate_layout(signal)
    _validate_conflict_matrix(signal)

    phases = signal.get("phases")
    if not isinstance(phases, list):
        raise ConfigError("signal.phases must be a list")

    _validate_phases(phases)

    if phases:
        phase_min_greens = [
            item["min_green_s"]
            for item in phases
            if isinstance(item.get("min_green_s"), (int, float))
        ]
        if phase_min_greens and pedestrian.get("min_green_s", 12.0) > min(
            phase_min_greens
        ):
            raise ConfigError(
                "pedestrian.min_green_s must not exceed the shortest phase "
                "min_green_s"
            )
    else:
        # Placeholder (simulation) phases use a 15 s minimum green; a larger
        # pedestrian requirement cannot be honoured there either.
        if pedestrian.get("min_green_s", 12.0) > 15.0:
            raise ConfigError(
                "pedestrian.min_green_s must not exceed the placeholder "
                "phase min_green_s (15 s)"
            )

    if runtime["mode"] == "deployment" and not phases:
        raise ConfigError(
            "Deployment mode requires reviewed signal phase definitions"
        )

    _validate_green_wave(config["green_wave"])

    safety = config["safety"]
    safety_flags = (
        "require_config_validation",
        "reject_unknown_phases",
        "reject_stale_observations",
    )

    for flag in safety_flags:
        if not isinstance(safety.get(flag), bool):
            raise ConfigError(f"safety.{flag} must be a boolean")


def _validate_layout(signal: dict[str, Any]) -> None:
    layout = signal.get("layout")
    if not isinstance(layout, dict):
        raise ConfigError("signal.layout must be a mapping")
    if layout.get("mode") not in {"minor_only", "main_road"}:
        raise ConfigError("signal.layout.mode must be minor_only or main_road")
    if layout.get("mode") == "main_road" and layout.get(
        "main_road_direction"
    ) not in {"NS", "EW"}:
        raise ConfigError(
            "signal.layout.main_road_direction must be NS or EW "
            "in main_road mode"
        )


def _validate_conflict_matrix(signal: dict[str, Any]) -> None:
    matrix = signal.get("conflict_matrix")
    if not isinstance(matrix, dict):
        raise ConfigError("signal.conflict_matrix must be a mapping")

    for phase_id, others in matrix.items():
        if not isinstance(phase_id, str) or not phase_id.strip():
            raise ConfigError(
                "signal.conflict_matrix keys must be non-empty strings"
            )
        if not isinstance(others, list):
            raise ConfigError("signal.conflict_matrix values must be lists")

    for phase_id, others in matrix.items():
        if phase_id in others:
            raise ConfigError(
                f"signal.conflict_matrix: phase {phase_id!r} "
                "conflicts with itself"
            )
        for other in others:
            if other not in matrix:
                raise ConfigError(
                    f"signal.conflict_matrix references unknown phase {other!r}"
                )
            if phase_id not in matrix[other]:
                raise ConfigError(
                    f"signal.conflict_matrix is not symmetric: "
                    f"{phase_id!r} -> {other!r}"
                )


def _validate_phases(phases: list[Any]) -> None:
    seen_phase_ids: set[str] = set()
    for item in phases:
        if not isinstance(item, dict):
            raise ConfigError("signal.phases entries must be mappings")
        phase_id = item.get("phase_id")
        if not isinstance(phase_id, str) or not phase_id.strip():
            raise ConfigError(
                "signal.phases[].phase_id must be a non-empty string"
            )
        if phase_id in seen_phase_ids:
            raise ConfigError(
                f"signal.phases contains duplicate phase_id {phase_id!r}; "
                "each phase must be defined exactly once"
            )
        seen_phase_ids.add(phase_id)
        movement_ids = item.get("movement_ids")
        if not isinstance(movement_ids, list) or not movement_ids:
            raise ConfigError(
                "signal.phases[].movement_ids must be a non-empty list"
            )
        for movement_id in movement_ids:
            if not isinstance(movement_id, str) or not movement_id.strip():
                raise ConfigError(
                    "signal.phases[].movement_ids entries must be "
                    "non-empty strings"
                )
        _require_non_negative_number(
            item.get("min_green_s"), "signal.phases[].min_green_s"
        )
        _require_positive_number(
            item.get("max_green_s"), "signal.phases[].max_green_s"
        )
        if item.get("min_green_s") > item.get("max_green_s"):
            raise ConfigError(
                "signal.phases[].min_green_s cannot exceed max_green_s"
            )
        _require_non_negative_number(
            item.get("yellow_s"), "signal.phases[].yellow_s"
        )
        _require_non_negative_number(
            item.get("all_red_s", 0.0), "signal.phases[].all_red_s"
        )


def _validate_score_ordering(optimization: dict[str, Any]) -> None:
    """Surface the P0/P1/P2/P3 weight guardrails at config-load time.

    The ordering rules live in ``scoring.ScoreConfig``; building one here turns
    its ``ValueError`` into a ``ConfigError`` so an unsafe weight combination
    is rejected before the runtime is assembled.
    """
    demand = optimization["demand"]
    spillback = optimization["spillback"]
    try:
        ScoreConfig(
            max_wait_target_s=optimization["max_wait_target_s"],
            wait_urgency_start_s=optimization["wait_urgency_start_s"],
            wait_urgency_max_score=optimization["wait_urgency_max_score"],
            green_wave_score=optimization["green_wave_score"],
            demand_vehicle_weight=demand["vehicle_weight"],
            demand_wait_weight=demand["wait_weight"],
            n_max=demand["n_max"],
            spillback_penalty=spillback["penalty"],
            o_high=spillback["o_high"],
            o_low=spillback["o_low"],
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _validate_green_wave(green_wave: dict[str, Any]) -> None:
    if not isinstance(green_wave.get("enabled"), bool):
        raise ConfigError("green_wave.enabled must be a boolean")
    if green_wave["enabled"]:
        main_phase = green_wave.get("main_through_phase")
        if not isinstance(main_phase, str) or not main_phase.strip():
            raise ConfigError(
                "green_wave.main_through_phase must be a non-empty string "
                "when enabled"
            )
    _require_non_negative_number(
        green_wave.get("band_start_s"), "green_wave.band_start_s"
    )
    _require_positive_number(
        green_wave.get("band_end_s"), "green_wave.band_end_s"
    )
    if green_wave.get("band_start_s") >= green_wave.get("band_end_s"):
        raise ConfigError("green_wave.band_start_s must be less than band_end_s")
    _require_positive_number(
        green_wave.get("period_s"), "green_wave.period_s"
    )


def _require_positive_number(value: Any, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        raise ConfigError(f"{field_name} must be a positive number")


def _require_non_negative_number(value: Any, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
    ):
        raise ConfigError(f"{field_name} must be a non-negative number")