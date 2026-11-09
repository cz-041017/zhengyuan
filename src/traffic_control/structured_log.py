"""Structured operational logging for the decision system.

Two consumers, one data source (docs/logging-data-contract.md):

* Every tick is recorded as a full JSONL snapshot locally — the RL trajectory
  and the audit trail.
* The cloud backend only receives *events* — phase switches, degradation /
  mode changes, execution-feedback failures, alarms — published on change.

The logging layer is best-effort by contract: it must never raise into the
control loop. Every sink catches and logs its own I/O errors.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from traffic_control.decision_layer import LayerResult
from traffic_control.degradation import DegradationMode
from traffic_control.feedback import FeedbackStatus
from traffic_control.models import ControllerMode, ControllerState
from traffic_control.telemetry import AlarmEvent, LogEvent

SCHEMA_VERSION = 1

logger = logging.getLogger("traffic_control.structured_log")

# Event types published to the cloud on change (docs §4).
EVENT_DECISION_CHANGE = "decision_change"
EVENT_DEGRADATION_CHANGE = "degradation_change"
EVENT_MODE_CHANGE = "mode_change"
EVENT_FEEDBACK_FAILURE = "feedback_failure"
EVENT_ALARM = "alarm"
EVENT_LOG = "log"


def _to_jsonable(obj: Any) -> Any:
    """Convert dataclasses / enums / collections to plain JSON-serializable data."""
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_jsonable(x) for x in obj]
    return obj


def _dumps(obj: Any) -> str:
    """Serialize to compact, deterministic JSON (sorted keys, UTF-8, no ASCII escapes)."""
    return json.dumps(
        _to_jsonable(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def build_tick_snapshot(
    tick_id: int,
    ts_wall: float,
    ts_mono: float,
    boot_id: str,
    junction_id: str,
    config_hash: str,
    controller_state: ControllerState,
    result: LayerResult,
) -> dict[str, Any]:
    """Assemble one full per-tick snapshot (schema v1, docs §3).

    ``reward`` is reserved for offline RL and always serialized as ``null``.
    Raw per-vehicle observations are intentionally NOT stored (deployment
    decision: 64 GB storage budget); only aggregated ``demands`` and
    ``lane_states`` are kept.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "tick_id": tick_id,
        "junction_id": junction_id,
        "ts_wall": ts_wall,
        "ts_mono": ts_mono,
        "boot_id": boot_id,
        "config_hash": config_hash,
        "mode": controller_state.mode.value,
        "state": {
            "controller": {
                "current_phase_id": controller_state.current_phase_id,
                "phase_started_at": controller_state.phase_started_at,
                "mode": controller_state.mode.value,
                "signal_colors": {
                    k: _to_jsonable(v) for k, v in controller_state.signal_colors.items()
                },
            },
            "degradation": result.degradation.value,
            "demands": {k: _to_jsonable(v) for k, v in result.demands.items()},
            "lane_states": {k: _to_jsonable(v) for k, v in result.lane_states.items()},
            "downstream_blocked": dict(result.downstream_blocked),
            "startup": result.startup.value if result.startup is not None else None,
            "active_crosswalks": sorted(result.active_crosswalks),
            "pedestrian_yellow_remaining_s": result.pedestrian_yellow_remaining_s,
        },
        "action": {
            "selected_phase_id": result.decision.selected_phase_id,
            "reason": result.decision.reason,
            "scores": {k: _to_jsonable(v) for k, v in result.decision.scores.items()},
            "constraints_applied": list(result.decision.constraints_applied),
            "feasible": result.decision.feasible,
            "feedback": result.feedback.value,
        },
        "reward": None,
    }


def compute_config_hash(config: Mapping[str, Any]) -> str:
    """Stable hash identifying the decision config that produced a trajectory.

    Hashes the *complete* config dict (not just ``TunableParameters``), because
    the phase set and its timings are part of the action space too — two configs
    that differ only in phase timing must not collide. Backend / offline RL uses
    this to bucket trajectories by policy (docs/logging-data-contract.md §3).
    """
    canonical = _dumps(config)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class JsonlFileSink:
    """Append-only JSONL writer with size/day rotation and retention.

    Best-effort: write errors are logged, never raised into the control loop.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        prefix: str = "tick",
        max_bytes_per_file: int = 50 * 1024 * 1024,
        retention_days: int = 30,
        max_total_bytes: int | None = None,
    ) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._max_bytes = max_bytes_per_file
        self._retention_days = retention_days
        self._max_total_bytes = max_total_bytes
        self._fh = None
        self._active_date: datetime.date | None = None
        self._last_purge = 0.0

    @property
    def active_path(self) -> Path:
        return self._dir / f"{self._prefix}.jsonl"

    def _open(self) -> None:
        self._fh = open(self.active_path, "a", encoding="utf-8")
        self._active_date = datetime.now(timezone.utc).date()

    def _rotate(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        active = self.active_path
        try:
            if active.exists() and active.stat().st_size > 0:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                active.rename(self._dir / f"{self._prefix}_{stamp}.jsonl")
        except OSError:
            # Best-effort: on rename failure keep appending to the active file.
            pass

    def write(self, record: Mapping[str, Any]) -> None:
        try:
            line = _dumps(record)
        except (TypeError, ValueError) as exc:
            logger.warning("structured_log: failed to serialize record: %s", exc)
            return
        try:
            today = datetime.now(timezone.utc).date()
            if self._fh is None:
                self._open()
            elif today != self._active_date:
                self._rotate()
                self._open()
            elif self.active_path.exists() and self.active_path.stat().st_size >= self._max_bytes:
                self._rotate()
                self._open()
            self._fh.write(line + "\n")
            self._fh.flush()
            self._purge_old(time.monotonic())
        except OSError as exc:
            logger.warning("structured_log: write failed: %s", exc)

    def _purge_old(self, now_mono: float) -> None:
        # Throttle to once per minute so we do not scan the directory every tick.
        if now_mono - self._last_purge < 60.0:
            return
        self._last_purge = now_mono
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        rotated = sorted(
            (p for p in self._dir.glob(f"{self._prefix}_*.jsonl")),
            key=lambda p: p.stat().st_mtime,
        )
        # Retention by age.
        for p in rotated:
            try:
                if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) < cutoff:
                    p.unlink()
            except OSError:
                continue
        # Hard overflow guard: delete oldest rotated files until under cap.
        if self._max_total_bytes is not None:
            remaining = [p for p in rotated if p.exists()]
            total = sum(p.stat().st_size for p in remaining)
            for p in remaining:  # already oldest-first
                if total <= self._max_total_bytes:
                    break
                try:
                    total -= p.stat().st_size
                    p.unlink()
                except OSError:
                    continue

    def flush(self) -> None:
        try:
            if self._fh is not None:
                self._fh.flush()
        except OSError as exc:
            logger.warning("structured_log: flush failed: %s", exc)

    def close(self) -> None:
        self.flush()
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


class BlackBox:
    """Bounded in-memory ring of the most recent snapshots (crash forensics)."""

    def __init__(self, max_ticks: int = 120) -> None:  # ~60 s at 2 Hz
        self._ring: deque[dict[str, Any]] = deque(maxlen=max_ticks)

    def record(self, snapshot: Mapping[str, Any]) -> None:
        self._ring.append(dict(snapshot))

    def __len__(self) -> int:
        return len(self._ring)

    def snapshots(self) -> list[dict[str, Any]]:
        return list(self._ring)

    def dump(self, path: str | Path) -> None:
        """Write the ring (oldest first) to ``path`` as JSONL. Best-effort."""
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for snap in self._ring:
                    fh.write(_dumps(snap) + "\n")
        except OSError as exc:
            logger.warning("structured_log: black box dump failed: %s", exc)


class CloudSink(ABC):
    """Transport-agnostic cloud event publisher (MQTT/HTTP)."""

    @abstractmethod
    def publish_event(self, event_type: str, payload: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def flush(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class NullCloudSink(CloudSink):
    """No-op cloud sink: discards events (used when cloud upload is disabled)."""

    def publish_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class MqttCloudSink(CloudSink):
    """MQTT cloud publisher (docs/logging-data-contract.md §5).

    * Events → ``traffic-control/{junction_id}/event/{event_type}``; alarms →
      ``traffic-control/{junction_id}/alarm`` (both QoS 1, retain=false).
    * Username/password auth (config-injected), optional TLS (port 8883).
    * Bounded outbound queue: while disconnected events are queued and flushed on
      reconnect; paho handles exponential-backoff reconnect.

    ``paho.mqtt`` is imported lazily so a junction that never uploads does not
    need the dependency. ``client`` is injectable for tests; when injected no
    connection is opened, but auth/TLS/reconnect wiring is still applied.
    Best-effort by contract: network/serialization errors are logged, never
    raised into the control loop.
    """

    def __init__(
        self,
        junction_id: str,
        *,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
        qos: int = 1,
        topic_prefix: str = "traffic-control",
        max_queue: int = 10000,
        keepalive_s: int = 60,
        client_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._junction_id = junction_id
        self._topic_prefix = topic_prefix.rstrip("/")
        self._qos = qos
        self._queue: deque[tuple[str, str]] = deque(maxlen=max_queue)
        self._connected = False

        if client is None:
            import paho.mqtt.client as mqtt  # lazy: only needed for cloud upload

            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id or f"traffic-{uuid.uuid4().hex[:8]}",
                protocol=mqtt.MQTTv311,
            )
            if username is not None:
                client.username_pw_set(username, password)
            if tls:
                client.tls_set()
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            # connect_async is non-blocking and never raises for an unreachable
            # broker; the network loop retries in the background with backoff.
            # This keeps logging best-effort: a down broker must not crash startup.
            client.connect_async(host, port, keepalive=keepalive_s)
            client.loop_start()
        else:
            # Test double: apply the same wiring so tests can assert config flow.
            if username is not None:
                client.username_pw_set(username, password)
            if tls:
                client.tls_set()
            client.reconnect_delay_set(min_delay=1, max_delay=30)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._client = client

    def _topic(self, event_type: str) -> str:
        if event_type == EVENT_ALARM:
            return f"{self._topic_prefix}/{self._junction_id}/alarm"
        return f"{self._topic_prefix}/{self._junction_id}/event/{event_type}"

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self._connected = True
        self.flush()

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self._connected = False

    def publish_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        topic = self._topic(event_type)
        try:
            msg = _dumps(payload)
        except (TypeError, ValueError) as exc:
            logger.warning("structured_log: mqtt serialize failed: %s", exc)
            return
        if self._connected:
            info = None
            try:
                info = self._client.publish(topic, msg, qos=self._qos)
            except Exception as exc:  # noqa: BLE001 — best-effort, never raise
                logger.warning("structured_log: mqtt publish failed: %s", exc)
            if info is not None and getattr(info, "rc", 0) != 0:
                # paho returns rc != 0 (e.g. MQTT_ERR_NO_CONN) when the message
                # was dropped; re-queue it for a later flush.
                self._queue.append((topic, msg))
        else:
            self._queue.append((topic, msg))

    def flush(self) -> None:
        while self._connected and self._queue:
            topic, msg = self._queue.popleft()
            try:
                self._client.publish(topic, msg, qos=self._qos)
            except Exception as exc:  # noqa: BLE001
                logger.warning("structured_log: mqtt flush failed: %s", exc)
                self._queue.appendleft((topic, msg))
                break

    def close(self) -> None:
        try:
            self._client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass


class LoggingPipeline:
    """Record every tick locally; publish events to the cloud on change.

    Implements the ``TelemetrySink`` protocol so decision-layer alarms and logs
    flow through the same pipeline (and the host loop reports heartbeat faults
    via ``publish_event``).
    """

    def __init__(
        self,
        junction_id: str,
        boot_id: str,
        config_hash: str,
        file_sink: JsonlFileSink | None = None,
        cloud_sink: CloudSink | None = None,
        black_box: BlackBox | None = None,
        crash_dump_path: str | Path | None = None,
    ) -> None:
        self._junction_id = junction_id
        self._boot_id = boot_id
        self._config_hash = config_hash
        self._file_sink = file_sink
        self._cloud_sink = cloud_sink
        self._black_box = black_box
        self._crash_dump_path = Path(crash_dump_path) if crash_dump_path else None
        self._tick_id = 0
        self._last_phase: str | None = None
        self._last_degradation: DegradationMode | None = None
        self._last_mode: ControllerMode | None = None
        self._last_feedback_failure = False

    def on_tick(
        self,
        controller_state: ControllerState,
        result: LayerResult,
        *,
        ts_wall: float | None = None,
        ts_mono: float | None = None,
    ) -> dict[str, Any]:
        """Record one tick and publish any change-triggered events.

        Returns the assembled snapshot so the caller can reuse it (e.g. tests).
        """
        tick_id = self._tick_id
        self._tick_id += 1
        if ts_wall is None:
            ts_wall = time.time()
        if ts_mono is None:
            ts_mono = time.monotonic()
        snapshot = build_tick_snapshot(
            tick_id,
            ts_wall,
            ts_mono,
            self._boot_id,
            self._junction_id,
            self._config_hash,
            controller_state,
            result,
        )
        self._record_local(snapshot)
        self._maybe_publish(snapshot, controller_state, result)
        return snapshot

    def _record_local(self, snapshot: Mapping[str, Any]) -> None:
        if self._file_sink is not None:
            self._file_sink.write(snapshot)
        if self._black_box is not None:
            self._black_box.record(snapshot)

    def _maybe_publish(
        self,
        snapshot: Mapping[str, Any],
        controller_state: ControllerState,
        result: LayerResult,
    ) -> None:
        if self._cloud_sink is None:
            return
        tick_id = snapshot["tick_id"]
        ts_wall = snapshot["ts_wall"]

        phase = result.decision.selected_phase_id
        if phase != self._last_phase:
            self._cloud_sink.publish_event(
                EVENT_DECISION_CHANGE,
                {
                    "tick_id": tick_id,
                    "ts_wall": ts_wall,
                    "from_phase": self._last_phase,
                    "to_phase": phase,
                    "reason": result.decision.reason,
                },
            )
            self._last_phase = phase

        if result.degradation != self._last_degradation:
            self._cloud_sink.publish_event(
                EVENT_DEGRADATION_CHANGE,
                {
                    "tick_id": tick_id,
                    "ts_wall": ts_wall,
                    "from_mode": self._last_degradation.value if self._last_degradation else None,
                    "to_mode": result.degradation.value,
                },
            )
            self._last_degradation = result.degradation

        if controller_state.mode != self._last_mode:
            self._cloud_sink.publish_event(
                EVENT_MODE_CHANGE,
                {
                    "tick_id": tick_id,
                    "ts_wall": ts_wall,
                    "from_mode": self._last_mode.value if self._last_mode else None,
                    "to_mode": controller_state.mode.value,
                },
            )
            self._last_mode = controller_state.mode

        if result.feedback == FeedbackStatus.FAILURE and not self._last_feedback_failure:
            self._cloud_sink.publish_event(
                EVENT_FEEDBACK_FAILURE,
                {
                    "tick_id": tick_id,
                    "ts_wall": ts_wall,
                    "current_phase_id": controller_state.current_phase_id,
                    "requested_phase_id": phase,
                },
            )
        self._last_feedback_failure = result.feedback == FeedbackStatus.FAILURE

    def report_alarm(self, event: AlarmEvent) -> None:
        data = _to_jsonable(event)
        data["ts_wall"] = data.pop("now")  # uniform envelope: ts_wall, not now
        data["tick_id"] = self._tick_id
        if self._file_sink is not None:
            self._file_sink.write(
                {"schema_version": SCHEMA_VERSION, "type": EVENT_ALARM, **data}
            )
        if self._cloud_sink is not None:
            self._cloud_sink.publish_event(EVENT_ALARM, data)

    def report_log(self, event: LogEvent) -> None:
        data = _to_jsonable(event)
        data["ts_wall"] = data.pop("now")
        data["tick_id"] = self._tick_id
        if self._file_sink is not None:
            self._file_sink.write(
                {"schema_version": SCHEMA_VERSION, "type": EVENT_LOG, **data}
            )

    def publish_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Record and publish a custom event (e.g. ``heartbeat_fault``) from the host loop."""
        data = _to_jsonable(payload)
        data.setdefault("tick_id", self._tick_id)
        data.setdefault("ts_wall", time.time())
        if self._file_sink is not None:
            self._file_sink.write(
                {"schema_version": SCHEMA_VERSION, "type": event_type, **data}
            )
        if self._cloud_sink is not None:
            self._cloud_sink.publish_event(event_type, data)

    def close(self) -> None:
        if self._file_sink is not None:
            self._file_sink.close()
        if self._cloud_sink is not None:
            self._cloud_sink.close()
        if self._black_box is not None and self._crash_dump_path is not None:
            self._black_box.dump(self._crash_dump_path)
