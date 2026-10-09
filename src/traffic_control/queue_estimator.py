from __future__ import annotations

from dataclasses import dataclass

from traffic_control.models import LaneState, VehicleObservation


@dataclass
class _TrackedVehicle:
    lane_id: str
    first_seen_at: float
    last_seen_at: float
    speed_mps: float | None


class QueueEstimator:
    """Estimate per-lane demand from vehicle tracking observations.

    The first observation time is used as a waiting-time proxy.
    It is not a substitute for detecting when a vehicle actually stops.
    """

    def __init__(
        self,
        queue_speed_threshold_mps: float = 0.5,
        vehicle_length_m: float = 5.0,
        stopped_gap_m: float = 2.0,
        stale_after_s: float = 3.0,
    ) -> None:
        if queue_speed_threshold_mps < 0:
            raise ValueError("queue speed threshold must be non-negative")
        if vehicle_length_m <= 0:
            raise ValueError("vehicle length must be positive")
        if stopped_gap_m < 0:
            raise ValueError("stopped gap must be non-negative")
        if stale_after_s <= 0:
            raise ValueError("stale timeout must be positive")

        self.queue_speed_threshold_mps = queue_speed_threshold_mps
        self.vehicle_length_m = vehicle_length_m
        self.stopped_gap_m = stopped_gap_m
        self.stale_after_s = stale_after_s
        self._vehicles: dict[int, _TrackedVehicle] = {}

    def update(
        self,
        observations: list[VehicleObservation],
        now: float,
        downstream_blocked_lanes: set[str] | None = None,
    ) -> dict[str, LaneState]:
        """Update tracked vehicles and return current per-lane states."""
        if now < 0:
            raise ValueError("now must be non-negative")

        blocked_lanes = downstream_blocked_lanes or set()

        for obs in observations:
            if obs.observed_at > now:
                raise ValueError("observation time cannot be in the future")

            tracked = self._vehicles.get(obs.track_id)

            if tracked is None:
                self._vehicles[obs.track_id] = _TrackedVehicle(
                    lane_id=obs.lane_id,
                    first_seen_at=obs.observed_at,
                    last_seen_at=obs.observed_at,
                    speed_mps=obs.speed_mps,
                )
            else:
                # Keep the original first-seen time for waiting estimation.
                # If a tracker ID changes lanes, update its current lane.
                tracked.lane_id = obs.lane_id
                tracked.last_seen_at = max(
                    tracked.last_seen_at, obs.observed_at
                )
                if obs.speed_mps is not None:
                    tracked.speed_mps = obs.speed_mps

        # Remove tracks that have disappeared for too long.
        stale_ids = [
            track_id
            for track_id, vehicle in self._vehicles.items()
            if now - vehicle.last_seen_at > self.stale_after_s
        ]
        for track_id in stale_ids:
            del self._vehicles[track_id]

        lane_vehicles: dict[str, list[_TrackedVehicle]] = {}
        for vehicle in self._vehicles.values():
            lane_vehicles.setdefault(vehicle.lane_id, []).append(vehicle)

        result: dict[str, LaneState] = {}

        for lane_id, vehicles in lane_vehicles.items():
            waiting_vehicles = [
                vehicle
                for vehicle in vehicles
                if vehicle.speed_mps is not None
                and vehicle.speed_mps <= self.queue_speed_threshold_mps
            ]

            # Without speed measurements, treat queue length as unknown
            # rather than assuming every detected vehicle is stopped.
            queue_length_m = (
                len(waiting_vehicles)
                * (self.vehicle_length_m + self.stopped_gap_m)
            )

            oldest_wait_s = max(
                (now - vehicle.first_seen_at for vehicle in vehicles),
                default=0.0,
            )

            result[lane_id] = LaneState(
                lane_id=lane_id,
                vehicle_count=len(vehicles),
                queue_length_m=queue_length_m,
                oldest_wait_s=oldest_wait_s,
                downstream_blocked=lane_id in blocked_lanes,
                last_updated_at=now,
            )

        return result