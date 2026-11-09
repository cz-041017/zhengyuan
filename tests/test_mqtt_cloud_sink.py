"""Tests for MqttCloudSink (docs/logging-data-contract.md §5).

The MQTT client is injected as a fake so no real broker / network is involved;
we assert topic routing, auth/TLS wiring, queue-on-disconnect, and close
idempotency.
"""
from __future__ import annotations

import json

from traffic_control.structured_log import (
    EVENT_ALARM,
    EVENT_DECISION_CHANGE,
    MqttCloudSink,
)


class _FakeInfo:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc


class _FakeClient:
    """Minimal paho.mqtt.client.Client double.

    Records auth/TLS/reconnect wiring and published messages; ``on_connect`` /
    ``on_disconnect`` are settable attributes (mirroring paho's callback API).
    """

    def __init__(self, publish_rc: int = 0) -> None:
        self.username = None
        self.password = None
        self.tls_enabled = False
        self.reconnect_delay = None
        self.on_connect = None
        self.on_disconnect = None
        self.published: list[tuple[str, str, int]] = []
        self.publish_rc = publish_rc
        self.loop_stopped = False
        self.disconnected = False

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.username = username
        self.password = password

    def tls_set(self, *args, **kwargs) -> None:
        self.tls_enabled = True

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def publish(self, topic: str, payload: str | None = None, qos: int = 0) -> _FakeInfo:
        self.published.append((topic, payload or "", qos))
        return _FakeInfo(self.publish_rc)

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True


def _make_sink(client: _FakeClient, **kwargs) -> MqttCloudSink:
    return MqttCloudSink(
        "panan_1",
        host="mqtt.example.com",
        client=client,
        **kwargs,
    )


def _connect(sink: MqttCloudSink, client: _FakeClient) -> None:
    sink._on_connect(client, None, None, 0, None)


def test_topic_routing_event_and_alarm() -> None:
    client = _FakeClient()
    sink = _make_sink(client)
    _connect(sink, client)

    sink.publish_event(EVENT_DECISION_CHANGE, {"to_phase": "P_N"})
    sink.publish_event(EVENT_ALARM, {"alarm_type": "failsafe"})

    assert client.published[0][0] == "traffic-control/panan_1/event/decision_change"
    assert client.published[1][0] == "traffic-control/panan_1/alarm"


def test_publish_serializes_payload_and_uses_qos1() -> None:
    client = _FakeClient()
    sink = _make_sink(client)
    _connect(sink, client)

    sink.publish_event(EVENT_DECISION_CHANGE, {"to_phase": "P_N", "tick_id": 7})

    topic, msg, qos = client.published[0]
    assert qos == 1
    assert json.loads(msg) == {"to_phase": "P_N", "tick_id": 7}


def test_queues_when_disconnected_and_flushes_on_connect() -> None:
    client = _FakeClient()
    sink = _make_sink(client)
    # Starts disconnected: events are queued, not published.
    sink.publish_event(EVENT_DECISION_CHANGE, {"to_phase": "P_MT"})
    assert client.published == []

    _connect(sink, client)  # on_connect -> flush

    assert len(client.published) == 1
    assert client.published[0][0].endswith("/event/decision_change")


def test_disconnect_stops_publishing_and_queues() -> None:
    client = _FakeClient()
    sink = _make_sink(client)
    _connect(sink, client)
    sink._on_disconnect(client, None, None, 0, None)

    sink.publish_event(EVENT_DECISION_CHANGE, {"to_phase": "P_E"})
    assert client.published == []


def test_auth_and_tls_wiring_applied_to_injected_client() -> None:
    client = _FakeClient()
    _make_sink(
        client,
        username="traffic-control",
        password="secret",
        tls=True,
    )

    assert client.username == "traffic-control"
    assert client.password == "secret"
    assert client.tls_enabled is True
    assert client.reconnect_delay == (1, 30)


def test_close_is_idempotent() -> None:
    client = _FakeClient()
    sink = _make_sink(client)
    sink.close()
    sink.close()  # second close must not raise

    assert client.loop_stopped is True
    assert client.disconnected is True


def test_requeues_when_publish_returns_error_rc() -> None:
    client = _FakeClient(publish_rc=4)  # MQTT_ERR_NO_CONN
    sink = _make_sink(client)
    _connect(sink, client)

    sink.publish_event(EVENT_DECISION_CHANGE, {"to_phase": "P_S"})

    # rc != 0 -> the message was dropped by paho, so it must be re-queued.
    assert len(sink._queue) == 1


def test_publish_serializes_deterministically() -> None:
    # sort_keys must be applied: same content, different key order -> identical bytes.
    client_a, client_b = _FakeClient(), _FakeClient()
    sink_a, sink_b = _make_sink(client_a), _make_sink(client_b)
    _connect(sink_a, client_a)
    _connect(sink_b, client_b)

    sink_a.publish_event(EVENT_DECISION_CHANGE, {"b": 2, "a": 1})
    sink_b.publish_event(EVENT_DECISION_CHANGE, {"a": 1, "b": 2})

    assert client_a.published[0][1] == client_b.published[0][1]
    assert json.loads(client_a.published[0][1]) == {"a": 1, "b": 2}
