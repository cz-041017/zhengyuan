from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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

    priorities = optimization.get("priorities")
    expected_priorities = [
        "downstream_spillback_prevention",
        "main_road_green_wave",
        "demand_score",
    ]
    if priorities != expected_priorities:
        raise ConfigError(
            "optimization.priorities must list the supported priorities "
            "in the configured order"
        )

    countdown = config["countdown"]
    _require_positive_number(
        countdown.get("display_duration_s"),
        "countdown.display_duration_s",
    )

    signal = config["signal"]
    phases = signal.get("phases")
    if not isinstance(phases, list):
        raise ConfigError("signal.phases must be a list")

    transitions = signal.get("transitions")
    if not isinstance(transitions, dict):
        raise ConfigError("signal.transitions must be a mapping")

    yellow_s = transitions.get("yellow_s")
    all_red_s = transitions.get("all_red_s")

    # None means that timing has not yet been reviewed.
    # A missing or unreviewed timing must not silently become zero.
    if yellow_s is not None:
        _require_positive_number(yellow_s, "signal.transitions.yellow_s")

    if all_red_s is not None:
        _require_non_negative_number(
            all_red_s,
            "signal.transitions.all_red_s",
        )

    if runtime["mode"] == "deployment":
        if not phases:
            raise ConfigError(
                "Deployment mode requires reviewed signal phase definitions"
            )
        if yellow_s is None or all_red_s is None:
            raise ConfigError(
                "Deployment mode requires reviewed transition timings"
            )
    safety = config["safety"]
    safety_flags = (
        "require_config_validation",
        "reject_unknown_phases",
        "reject_stale_observations",
    )

    for flag in safety_flags:
        if not isinstance(safety.get(flag), bool):
            raise ConfigError(f"safety.{flag} must be a boolean")

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