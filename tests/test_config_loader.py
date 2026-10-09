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


def test_rejects_deployment_without_reviewed_signal_timings() -> None:
    config = load_config(DEFAULT_CONFIG)
    config["runtime"]["mode"] = "deployment"
    config["signal"]["phases"] = [
        {
            "phase_id": "NS",
            "movement_ids": ["north_south"],
        }
    ]

    with pytest.raises(ConfigError, match="transition timings"):
        validate_config(config)


def test_simulation_allows_unreviewed_signal_timings() -> None:
    config = load_config(DEFAULT_CONFIG)

    config["signal"]["transitions"]["yellow_s"] = None
    config["signal"]["transitions"]["all_red_s"] = None

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