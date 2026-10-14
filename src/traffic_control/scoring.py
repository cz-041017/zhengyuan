from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ScoreConfig:
    """Tunable parameters for the P0–P3 decision score.

    All numeric values are INITIAL values pending simulation calibration and
    must never be treated as certified. Clearance timings (yellow + all-red)
    and minimum/maximum green live on the audited signal phases, not here.
    """

    max_wait_target_s: float = 120.0
    wait_urgency_start_s: float = 80.0
    wait_urgency_max_score: float = 5.0
    green_wave_score: float = 2.0
    demand_vehicle_weight: float = 0.6
    demand_wait_weight: float = 0.4
    n_max: float = 20.0
    spillback_penalty: float = -1000.0
    o_high: float = 0.8
    o_low: float = 0.5

    def __post_init__(self) -> None:
        if self.max_wait_target_s <= 0:
            raise ValueError("max_wait_target_s must be positive")
        if not 0 <= self.wait_urgency_start_s < self.max_wait_target_s:
            raise ValueError(
                "wait_urgency_start_s must be within [0, max_wait_target_s)"
            )
        if self.wait_urgency_max_score <= 0:
            raise ValueError("wait_urgency_max_score must be positive")
        if self.green_wave_score < 0:
            raise ValueError("green_wave_score must be non-negative")
        if not (0 <= self.demand_vehicle_weight <= 1):
            raise ValueError("demand_vehicle_weight must be within [0, 1]")
        if not (0 <= self.demand_wait_weight <= 1):
            raise ValueError("demand_wait_weight must be within [0, 1]")
        if self.demand_vehicle_weight + self.demand_wait_weight <= 0:
            raise ValueError("at least one demand weight must be positive")
        if self.n_max <= 0:
            raise ValueError("n_max must be positive")
        if self.spillback_penalty >= 0:
            raise ValueError("spillback_penalty must be negative")
        if not (0 <= self.o_low < self.o_high):
            raise ValueError("o_low must satisfy 0 <= o_low < o_high")


def aggregate_demand(
    vehicle_counts: Iterable[int], oldest_waits_s: Iterable[float]
) -> tuple[int, float]:
    """Aggregate per-movement demand into per-group demand.

    Per design §4.1: group vehicle count is the sum, group wait is the max.
    """
    counts = list(vehicle_counts)
    waits = list(oldest_waits_s)
    if len(counts) != len(waits):
        raise ValueError("vehicle_counts and oldest_waits_s must align")
    return sum(counts), max(waits, default=0.0)


class Scorer:
    """Compute the P0–P3 phase score.

    Score is used only to rank phases inside the feasible set F(t); the hard
    constraints (safety interlock, min/max green, P0 deadline preemption and
    P1 spillback exclusion) are enforced by the phase state machine, not by
    score.
    """

    def __init__(self, config: ScoreConfig) -> None:
        self._config = config

    @property
    def max_wait_target_s(self) -> float:
        """The P0 single-direction maximum wait target (seconds)."""
        return self._config.max_wait_target_s

    # -- P3 demand ---------------------------------------------------------
    def demand_score(self, vehicle_count: int, oldest_wait_s: float) -> float:
        cfg = self._config
        n_term = min(max(vehicle_count, 0) / cfg.n_max, 1.0)
        w_term = min(max(oldest_wait_s, 0.0) / cfg.max_wait_target_s, 1.0)
        return (
            cfg.demand_vehicle_weight * n_term + cfg.demand_wait_weight * w_term
        )

    # -- P2 green wave -----------------------------------------------------
    def green_wave_score(self, green_wave_active: bool) -> float:
        return self._config.green_wave_score if green_wave_active else 0.0

    # -- P0 wait urgency (soft early warning) ------------------------------
    def wait_urgency_score(self, oldest_wait_s: float) -> float:
        cfg = self._config
        span = cfg.max_wait_target_s - cfg.wait_urgency_start_s
        x = (oldest_wait_s - cfg.wait_urgency_start_s) / span
        if x <= 0:
            return 0.0
        x = min(x, 1.0)
        return cfg.wait_urgency_max_score * x * x

    # -- P1 spillback (defense-in-depth penalty) ---------------------------
    def spillback_score(self, downstream_blocked: bool) -> float:
        return self._config.spillback_penalty if downstream_blocked else 0.0

    # -- combined ----------------------------------------------------------
    def phase_score(
        self,
        vehicle_count: int,
        oldest_wait_s: float,
        *,
        green_wave_active: bool = False,
        downstream_blocked: bool = False,
    ) -> float:
        return (
            self.demand_score(vehicle_count, oldest_wait_s)
            + self.green_wave_score(green_wave_active)
            + self.wait_urgency_score(oldest_wait_s)
            + self.spillback_score(downstream_blocked)
        )


class SpillbackMonitor:
    """Downstream-occupancy hysteresis for P1 spillback protection.

    A direction becomes blocked when occupancy reaches ``o_high`` and stays
    blocked until it drops to ``o_low`` (o_low < o_high). An unknown
    observation (None) is treated as blocked, never as clear.
    """

    def __init__(self, o_high: float, o_low: float) -> None:
        if not (0 <= o_low < o_high):
            raise ValueError("o_low must satisfy 0 <= o_low < o_high")
        self._o_high = o_high
        self._o_low = o_low
        self._blocked: dict[str, bool] = {}

    def update(self, direction_id: str, occupancy: float | None) -> bool:
        if not direction_id.strip():
            raise ValueError("direction_id must not be empty")

        if occupancy is None:
            # No data must never be interpreted as "downstream clear".
            self._blocked[direction_id] = True
            return True

        if occupancy < 0:
            raise ValueError("occupancy must be non-negative or None")

        was_blocked = self._blocked.get(direction_id, False)
        if was_blocked:
            if occupancy <= self._o_low:
                self._blocked[direction_id] = False
        else:
            if occupancy >= self._o_high:
                self._blocked[direction_id] = True

        return self._blocked[direction_id]

    def is_blocked(self, direction_id: str) -> bool:
        return self._blocked.get(direction_id, False)
