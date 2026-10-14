from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


class DegradationMode(str, Enum):
    """Operating mode derived from observation freshness (design §8)."""

    NORMAL = "normal"  # all phases fresh -> adaptive
    PARTIAL = "partial"  # some stale -> adaptive with conservative waits
    FAILSAFE = "failsafe"  # no fresh data -> fixed-time / flash (GB 25280)


@dataclass(frozen=True)
class Freshness:
    fresh: frozenset[str]
    stale: frozenset[str]


class DataFreshness:
    """Per-phase observation freshness.

    A phase is fresh only when it has reported within ``timeout_s``. A phase
    that has never reported, or whose report has expired, is stale — stale
    means UNKNOWN and must never be treated as "empty".
    """

    def __init__(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._timeout_s = timeout_s

    def assess(
        self,
        now: float,
        phase_ids: Iterable[str],
        last_seen: Mapping[str, float | None],
    ) -> Freshness:
        ids = set(phase_ids)
        fresh: set[str] = set()
        for phase_id in ids:
            seen_at = last_seen.get(phase_id)
            if seen_at is not None and 0 <= now - seen_at <= self._timeout_s:
                fresh.add(phase_id)
        return Freshness(fresh=frozenset(fresh), stale=frozenset(ids - fresh))


class DegradationController:
    """Map observation freshness to an operating mode (design §8)."""

    def __init__(self, timeout_s: float) -> None:
        self._freshness = DataFreshness(timeout_s)

    def decide(
        self,
        now: float,
        phase_ids: Iterable[str],
        last_seen: Mapping[str, float | None],
    ) -> DegradationMode:
        freshness = self._freshness.assess(now, phase_ids, last_seen)
        if not freshness.fresh:
            return DegradationMode.FAILSAFE
        if freshness.stale:
            return DegradationMode.PARTIAL
        return DegradationMode.NORMAL


def conservative_oldest_wait(
    now: float,
    last_seen_at: float | None,
    observed_wait_s: float,
    timeout_s: float,
) -> float:
    """Resolve the wait to feed the scorer and P0 preemption.

    Fresh data uses the observed wait. Stale or missing data is treated
    conservatively: the direction is assumed to have been waiting since its
    last observation (or since startup if never observed), so it is still
    served through the P0 budget instead of being silently treated as empty.
    """
    if last_seen_at is not None and 0 <= now - last_seen_at <= timeout_s:
        return observed_wait_s
    base = last_seen_at if last_seen_at is not None else 0.0
    return max(observed_wait_s, now - base)
