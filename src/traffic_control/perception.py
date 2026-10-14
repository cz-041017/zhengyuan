from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from traffic_control.models import VehicleObservation
from traffic_control.phasing import (
    EAST_LEFT,
    EAST_THROUGH,
    NORTH_LEFT,
    NORTH_THROUGH,
    SOUTH_LEFT,
    SOUTH_THROUGH,
    WEST_LEFT,
    WEST_THROUGH,
)

# Canonical lane/movement ids understood by the decision layer.
CANONICAL_LANE_IDS = frozenset(
    {
        NORTH_THROUGH,
        NORTH_LEFT,
        EAST_THROUGH,
        EAST_LEFT,
        SOUTH_THROUGH,
        SOUTH_LEFT,
        WEST_THROUGH,
        WEST_LEFT,
    }
)


@dataclass(frozen=True)
class RawDetection:
    """A raw vehicle detection reported by the perception (YOLO11) layer.

    ``lane_id`` must be one of ``CANONICAL_LANE_IDS``. ``confidence`` is
    optional and reserved for future data-quality filtering; the decision
    layer currently ignores it.
    """

    track_id: int
    lane_id: str
    observed_at: float
    speed_mps: float | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.track_id < 0:
            raise ValueError("track_id must be non-negative")
        if not self.lane_id.strip():
            raise ValueError("lane_id must not be empty")
        if self.observed_at < 0:
            raise ValueError("observed_at must be non-negative")
        if self.speed_mps is not None and self.speed_mps < 0:
            raise ValueError("speed_mps must be non-negative")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class DownstreamReport:
    """Downstream link occupancy for P1 spillback protection.

    ``occupancy`` is a normalized value in [0, 1], or None when unknown.
    ``lane_id`` is the canonical movement id of the direction feeding that
    downstream link.
    """

    lane_id: str
    occupancy: float | None
    observed_at: float

    def __post_init__(self) -> None:
        if not self.lane_id.strip():
            raise ValueError("lane_id must not be empty")
        if self.occupancy is not None and not 0 <= self.occupancy <= 1:
            raise ValueError("occupancy must be within [0, 1] or None")
        if self.observed_at < 0:
            raise ValueError("observed_at must be non-negative")


@dataclass(frozen=True)
class PerceptionFrame:
    """One perception cycle: vehicle detections plus downstream occupancy."""

    observed_at: float
    detections: tuple[VehicleObservation, ...]
    downstream: tuple[DownstreamReport, ...] = ()


def normalize_detections(
    raw: Iterable[RawDetection],
) -> tuple[VehicleObservation, ...]:
    """Validate raw detections and convert them to decision-layer input.

    Raises ``ValueError`` when a ``lane_id`` is not a canonical movement id,
    so a camera-to-lane mapping mistake fails fast instead of silently
    misrouting vehicles.
    """
    detections: list[VehicleObservation] = []
    for detection in raw:
        if detection.lane_id not in CANONICAL_LANE_IDS:
            raise ValueError(
                f"unknown lane_id {detection.lane_id!r}; expected one of "
                f"{sorted(CANONICAL_LANE_IDS)}"
            )
        detections.append(
            VehicleObservation(
                track_id=detection.track_id,
                lane_id=detection.lane_id,
                observed_at=detection.observed_at,
                speed_mps=detection.speed_mps,
            )
        )
    return tuple(detections)


def downstream_occupancy(
    reports: Iterable[DownstreamReport],
) -> dict[str, float | None]:
    """Index downstream occupancy by canonical lane_id (None = unknown)."""
    return {report.lane_id: report.occupancy for report in reports}
