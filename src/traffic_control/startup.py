"""GB-style startup sequence: 5 s yellow flash, then 10 s all-red.

The decision layer only models *when* the startup sequence is active and which
stage it is in. Driving the actual flash/red lamp colours is the control
interface / signal controller's job.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StartupStage(str, Enum):
    FLASH = "flash"  # 5 s yellow flash
    ALL_RED = "all_red"  # 10 s all-red
    DONE = "done"  # sequence finished, normal control may resume


@dataclass(frozen=True)
class StartupSequence:
    """Immutable record of one startup sequence."""

    started_at: float
    flash_s: float = 5.0
    all_red_s: float = 10.0

    def __post_init__(self) -> None:
        if self.flash_s < 0 or self.all_red_s < 0:
            raise ValueError("startup durations must be non-negative")

    @property
    def total_s(self) -> float:
        return self.flash_s + self.all_red_s

    def stage(self, now: float) -> StartupStage:
        elapsed = now - self.started_at
        if elapsed < -self.total_s:
            # Clock jumped backwards well past the sequence start. Do not flash
            # forever: treat the sequence as already finished.
            return StartupStage.DONE
        if elapsed < 0:
            # Mild clock skew: not started yet, keep flashing.
            return StartupStage.FLASH
        if elapsed < self.flash_s:
            return StartupStage.FLASH
        if elapsed < self.total_s:
            return StartupStage.ALL_RED
        return StartupStage.DONE

    def is_done(self, now: float) -> bool:
        return self.stage(now) is StartupStage.DONE

    def remaining_s(self, now: float) -> float:
        return max(0.0, self.total_s - (now - self.started_at))
