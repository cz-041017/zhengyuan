from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class SignalColor(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"


class ControllerMode(str, Enum):
    NORMAL = "normal"
    CLEARANCE = "clearance"
    ALL_RED = "all_red"
    FAULT = "fault"


@dataclass(frozen=True)
class VehicleObservation:
    """A vehicle detected by the vision system at a specific time."""

    track_id: int
    lane_id: str
    observed_at: float
    speed_mps: float | None = None

    def __post_init__(self) -> None:
        if self.track_id < 0:
            raise ValueError("track_id must be non-negative")
        if not self.lane_id.strip():
            raise ValueError("lane_id must not be empty")
        if self.observed_at < 0:
            raise ValueError("observed_at must be non-negative")
        if self.speed_mps is not None and self.speed_mps < 0:
            raise ValueError("speed_mps must be non-negative")


@dataclass(frozen=True)
class LaneState:
    """Estimated traffic demand and waiting status for one lane."""

    lane_id: str
    vehicle_count: int
    queue_length_m: float
    oldest_wait_s: float
    downstream_blocked: bool = False
    last_updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.lane_id.strip():
            raise ValueError("lane_id must not be empty")
        if self.vehicle_count < 0:
            raise ValueError("vehicle_count must be non-negative")
        if self.queue_length_m < 0:
            raise ValueError("queue_length_m must be non-negative")
        if self.oldest_wait_s < 0:
            raise ValueError("oldest_wait_s must be non-negative")
        if self.last_updated_at < 0:
            raise ValueError("last_updated_at must be non-negative")


@dataclass(frozen=True)
class SignalPhase:
    """A configured, legally compatible set of traffic movements."""

    phase_id: str
    movement_ids: frozenset[str]
    min_green_s: float
    max_green_s: float
    yellow_s: float
    all_red_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.phase_id.strip():
            raise ValueError("phase_id must not be empty")
        if not self.movement_ids:
            raise ValueError("a phase must contain at least one movement")
        if self.min_green_s < 0 or self.max_green_s <= 0:
            raise ValueError("green durations must be valid")
        if self.min_green_s > self.max_green_s:
            raise ValueError("min_green_s cannot exceed max_green_s")
        if self.yellow_s < 0 or self.all_red_s < 0:
            raise ValueError("clearance durations must be non-negative")


@dataclass(frozen=True)
class ControllerState:
    """Current logical state of the simulated signal controller."""

    mode: ControllerMode
    current_phase_id: str | None
    phase_started_at: float
    signal_colors: Mapping[str, SignalColor] = field(default_factory=dict)


@dataclass(frozen=True)
class PhaseDecision:
    """The decision layer's proposed next phase, not a direct light command."""

    selected_phase_id: str | None
    reason: str
    scores: Mapping[str, float] = field(default_factory=dict)
    constraints_applied: tuple[str, ...] = ()
    feasible: bool = True