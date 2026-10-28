from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from traffic_control.config_loader import (
    ConfigError,
    load_config,
    validate_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"


def test_loads_default_config() -> None:
    config = load_config(DEFAULT_CONFIG)

    assert config["project"]["name"] == "smart-traffic-control"
    assert config["runtime"]["mode"] == "simulation"


def test_rejects_invalid_runtime_mode() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["runtime"]["mode"] = "live"

    with pytest.raises(ConfigError, match="runtime.mode"):
        validate_config(config)


def test_rejects_negative_vehicle_length() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["queue_estimation"]["vehicle_length_m"] = -1

    with pytest.raises(ConfigError, match="vehicle_length_m"):
        validate_config(config)


def test_rejects_boolean_as_numeric_parameter() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["optimization"]["max_wait_target_s"] = True

    with pytest.raises(ConfigError, match="max_wait_target_s"):
        validate_config(config)


def test_rejects_deployment_without_reviewed_phase_definitions() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["runtime"]["mode"] = "deployment"
    config["signal"]["phases"] = []

    with pytest.raises(ConfigError, match="reviewed signal phase definitions"):
        validate_config(config)


def test_simulation_allows_empty_phases() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["phases"] = []

    validate_config(config)


def test_rejects_phase_min_green_exceeding_max() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["phases"] = [
        {
            "phase_id": "P_N",
            "movement_ids": ["north_through", "north_left"],
            "min_green_s": 70,
            "max_green_s": 60,
            "yellow_s": 3,
            "all_red_s": 1,
        }
    ]

    with pytest.raises(ConfigError, match="min_green_s"):
        validate_config(config)

def test_rejects_invalid_safety_flag() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["safety"]["require_config_validation"] = "yes"

    with pytest.raises(ConfigError, match="require_config_validation"):
        validate_config(config)


def test_rejects_invalid_config_version() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["project"]["config_version"] = 0

    with pytest.raises(ConfigError, match="config_version"):
        validate_config(config)


def test_rejects_invalid_layout_mode() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["layout"]["mode"] = "ring"

    with pytest.raises(ConfigError, match="layout.mode"):
        validate_config(config)


def test_rejects_main_road_without_direction() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["layout"]["mode"] = "main_road"
    config["signal"]["layout"]["main_road_direction"] = None

    with pytest.raises(ConfigError, match="main_road_direction"):
        validate_config(config)


def test_rejects_asymmetric_conflict_matrix() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["conflict_matrix"]["P_W"] = []

    with pytest.raises(ConfigError, match="not symmetric"):
        validate_config(config)


def test_rejects_self_conflict_in_matrix() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["conflict_matrix"]["P_N"] = ["P_N"]

    with pytest.raises(ConfigError, match="conflicts with itself"):
        validate_config(config)


def test_rejects_unknown_conflict_phase() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["signal"]["conflict_matrix"]["P_N"] = ["P_X"]

    with pytest.raises(ConfigError, match="unknown phase"):
        validate_config(config)


def test_rejects_wrong_priority_order() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["optimization"]["priorities"] = list(
        reversed(config["optimization"]["priorities"])
    )

    with pytest.raises(ConfigError, match="priorities"):
        validate_config(config)


def test_rejects_invalid_spillback_thresholds() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["optimization"]["spillback"]["o_low"] = 0.9

    with pytest.raises(ConfigError, match="spillback"):
        validate_config(config)


def test_rejects_invalid_demand_weight() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["optimization"]["demand"]["vehicle_weight"] = 1.5

    with pytest.raises(ConfigError, match="vehicle_weight"):
        validate_config(config)


def test_rejects_invalid_green_wave_band() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["green_wave"]["band_start_s"] = 50.0

    with pytest.raises(ConfigError, match="band_start_s"):
        validate_config(config)


def test_rejects_enabled_green_wave_without_phase() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["green_wave"]["enabled"] = True
    config["green_wave"]["main_through_phase"] = None

    with pytest.raises(ConfigError, match="main_through_phase"):
        validate_config(config)


def test_rejects_duplicate_phase_id() -> None:
    config = load_config(DEFAULT_CONFIG)
    phase = {
        "phase_id": "P_N",
        "movement_ids": ["north_through", "north_left"],
        "min_green_s": 10,
        "max_green_s": 60,
        "yellow_s": 3,
        "all_red_s": 1,
    }
    config["signal"]["phases"] = [phase, dict(phase)]

    with pytest.raises(ConfigError, match="duplicate phase_id"):
        validate_config(config)


def test_rejects_weights_breaking_priority_order() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["optimization"]["demand"]["vehicle_weight"] = 1.0
    config["optimization"]["demand"]["wait_weight"] = 1.0
    config["optimization"]["green_wave_score"] = 0.5

    with pytest.raises(ConfigError, match="P2 outranks P3"):
        validate_config(config)