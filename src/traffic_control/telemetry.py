"""Telemetry boundary: alarms and runtime logs leave the decision layer here.

The decision layer must not know how telemetry is transported. It only emits
typed events through a ``TelemetrySink``. The default implementation is a no-op
(optionally writing to the local Python logger); a later data-interface layer
can inject an HTTP/MQTT sink without touching decision-layer code.

This module deliberately contains NO network code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Protocol


class AlarmLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlarmType(str, Enum):
    """Alarm categories the decision layer can raise."""

    FAILSAFE = "failsafe"  # data watchdog: all feeds dead -> hand over control
    PARTIAL = "partial"  # some feeds stale
    TAKEOVER = "takeover"  # control room took over a junction
    RELEASE = "release"  # control room released a junction
    STARTUP_SEQUENCE = "startup_sequence"  # 5s flash + 10s red requested
    FEEDBACK_TIMEOUT = "feedback_timeout"  # execution feedback not confirmed
    NO_FEASIBLE_PHASE = "no_feasible_phase"  # all non-blocked successors excluded


@dataclass(frozen=True)
class AlarmEvent:
    """One alarm emitted by the decision layer."""

    alarm_type: AlarmType
    junction_id: str | None
    now: float
    level: AlarmLevel = AlarmLevel.WARNING
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LogEvent:
    """One runtime log line emitted by the decision layer."""

    now: float
    message: str
    level: AlarmLevel = AlarmLevel.INFO
    junction_id: str | None = None


class TelemetrySink(Protocol):
    """Transport-agnostic telemetry boundary."""

    def report_alarm(self, event: AlarmEvent) -> None: ...

    def report_log(self, event: LogEvent) -> None: ...


class NullTelemetrySink:
    """Default sink: local logging only, never blocks, never raises."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("traffic_control.telemetry")

    def report_alarm(self, event: AlarmEvent) -> None:
        self._logger.warning(
            "alarm type=%s junction=%s now=%.1f level=%s detail=%s",
            event.alarm_type.value,
            event.junction_id,
            event.now,
            event.level.value,
            dict(event.detail),
        )

    def report_log(self, event: LogEvent) -> None:
        self._logger.info(
            "log now=%.1f junction=%s %s",
            event.now,
            event.junction_id,
            event.message,
        )


class CollectingTelemetrySink:
    """Test helper: records events in memory instead of emitting them."""

    def __init__(self) -> None:
        self.alarms: list[AlarmEvent] = []
        self.logs: list[LogEvent] = []

    def report_alarm(self, event: AlarmEvent) -> None:
        self.alarms.append(event)

    def report_log(self, event: LogEvent) -> None:
        self.logs.append(event)
