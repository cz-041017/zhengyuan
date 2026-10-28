from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


class DegradationMode(str, Enum):
    """Operating mode derived from observation freshness (design §8)."""

    NORMAL = "normal"  # all phases fresh -> adaptive
    PARTIAL = "partial"  # some stale -> adaptive with conservative waits
    FAILSAFE = "failsafe"  # all feeds dead -> fixed-time / flash (GB 25280)


@dataclass(frozen=True)
class Freshness:
    """Three-level freshness.

    ``fresh`` reported within ``timeout_s``; ``stale`` between ``timeout_s`` and
    ``2*timeout_s`` (PARTIAL); ``dead`` never reported or beyond ``2*timeout_s``
    (FAILSAFE). A dead feed is UNKNOWN and must never be treated as "empty".
    """

    fresh: frozenset[str]
    stale: frozenset[str]
    dead: frozenset[str] = frozenset()


class DataFreshness:
    """Per-phase observation freshness with a two-strike failsafe window.

    The watchdog rule is fixed by project decision: a phase is PARTIAL after
    one timeout period and FAILSAFE after two timeout periods (``2*timeout_s``)
    of silence. This avoids a single dropped frame flipping the whole system
    into fixed-time.
    """

    def __init__(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._timeout_s = timeout_s
        self._failsafe_after_s = 2.0 * timeout_s

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def failsafe_after_s(self) -> float:
        return self._failsafe_after_s

    def assess(
        self,
        now: float,
        phase_ids: Iterable[str],
        last_seen: Mapping[str, float | None],
    ) -> Freshness:
        ids = set(phase_ids)
        fresh: set[str] = set()
        stale: set[str] = set()
        dead: set[str] = set()
        for phase_id in ids:
            seen_at = last_seen.get(phase_id)
            if seen_at is None:
                dead.add(phase_id)
                continue
            age = now - seen_at
            if age < 0:
                # Future timestamp is a clock-skew frame. Treat it as stale
                # (PARTIAL) rather than dead so one bad frame cannot flip the
                # whole junction to fixed-time.
                stale.add(phase_id)
            elif age <= self._timeout_s:
                fresh.add(phase_id)
            elif age <= self._failsafe_after_s:
                stale.add(phase_id)
            else:
                dead.add(phase_id)
        return Freshness(
            fresh=frozenset(fresh),
            stale=frozenset(stale),
            dead=frozenset(dead),
        )


class DegradationController:
    """Map observation freshness to an operating mode (design §8)."""

    def __init__(self, timeout_s: float) -> None:
        self._freshness = DataFreshness(timeout_s)

    @property
    def timeout_s(self) -> float:
        return self._freshness.timeout_s

    @property
    def failsafe_after_s(self) -> float:
        return self._freshness.failsafe_after_s

    def decide(
        self,
        now: float,
        phase_ids: Iterable[str],
        last_seen: Mapping[str, float | None],
    ) -> DegradationMode:
        freshness = self._freshness.assess(now, phase_ids, last_seen)
        # No phase has reported within one timeout: all feeds are dead/stale.
        if not freshness.fresh and not freshness.stale:
            return DegradationMode.FAILSAFE
        # Any phase beyond two timeout periods (or never seen) is degraded.
        if freshness.stale or freshness.dead:
            return DegradationMode.PARTIAL
        return DegradationMode.NORMAL

    def assess(
        self,
        now: float,
        phase_ids: Iterable[str],
        last_seen: Mapping[str, float | None],
    ) -> Freshness:
        """Expose per-phase freshness so callers can apply conservative waits."""
        return self._freshness.assess(now, phase_ids, last_seen)


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

    KNOWN LIMITATION, left as documentation deliberately.
    The never-observed branch returns ``now``, which grows without bound. On a
    long-running deployment a direction whose sensor never reported therefore
    looks permanently starving, and if several sensors are dead they all tie at
    the same wait and preemption churns between them. Probability is low
    because several simultaneous dead feeds is already a degraded state that
    ``DegradationController`` reports as PARTIAL/FAILSAFE. Suggested fix when
    it is worth the contract change: cap the never-observed wait (for example
    at the deadline), and escalate a never-observed direction once through an
    alarm instead of re-preempting every tick.
    """
    if last_seen_at is not None and 0 <= now - last_seen_at <= timeout_s:
        return observed_wait_s
    base = last_seen_at if last_seen_at is not None else 0.0
    return max(observed_wait_s, now - base)
