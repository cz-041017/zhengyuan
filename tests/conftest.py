"""pytest 根配置：把项目根加入 sys.path，使 tests 可 import interface.hikvision。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from traffic_control.models import ControllerMode, ControllerState  # noqa: E402


@pytest.fixture
def boot():
    """Complete a fresh DecisionLayer's 15 s first power-on startup.

    ``boot(layer, now)`` runs the flash tick; the startup sequence finishes
    exactly at ``now``, so the next ``layer.tick(now, ...)`` takes the normal
    control path.
    """

    def _boot(layer, now: float) -> None:
        layer.tick(
            now - 15.0,
            ControllerState(ControllerMode.NORMAL, None, now - 15.0),
        )

    return _boot
