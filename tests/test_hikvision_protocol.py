"""海康 XHJ-CW-GA-HK3 控制接口层协议测试。"""
from __future__ import annotations

import pytest

from interface.hikvision.adapter import (
    PhaseIndication,
    build_phase_indication,
)
from interface.hikvision.controller_link import ControllerLink
from interface.hikvision.heartbeat import Heartbeat
from interface.hikvision.protocol import (
    Frame,
    ProtocolConfig,
    crc16_ccitt,
    decode_frame,
    encode_frame,
)
from interface.hikvision.transports import NullTransport


def test_crc_known_value():
    # CRC-16/CCITT-FALSE 标准校验值
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_encode_decode_roundtrip():
    cfg = ProtocolConfig(address=0x01)
    frame = Frame(address=0x01, control=0x00, data=bytes([0x30, 0x02, 0x00]))
    raw = encode_frame(frame, cfg)
    assert raw[0] == 0x7E and raw[-1] == 0x7E
    assert decode_frame(raw, cfg) == frame


def test_stuffing_roundtrip_escapable_bytes():
    # 数据里含 0x7E / 0x7D 时需转义且能还原
    data = bytes([0x7E, 0x7D, 0x00, 0x7E])
    frame = Frame(address=0x01, control=0x00, data=data)
    raw = encode_frame(frame)
    assert decode_frame(raw).data == data


def test_crc_mismatch_raises():
    # 手工构造一帧，CRC 故意写错（body 内无 0x7E/0x7D，避免转义干扰）
    body = bytes([0x01, 0x00, 0x30, 0x01, 0x00])
    bad = bytes([0x7E]) + body + b"\x00\x00" + bytes([0x7E])
    with pytest.raises(ValueError, match="CRC mismatch"):
        decode_frame(bad)


def test_phase_indication_frame():
    cfg = ProtocolConfig(address=0x01)
    ind = PhaseIndication(phase_number=3, all_red_required=False)
    frame = build_phase_indication(ind, cfg)
    assert frame.address == 0x01
    assert frame.data == bytes([cfg.cmd_phase_control, 0x03, 0x00])


def test_phase_indication_all_red_flag():
    cfg = ProtocolConfig(address=0x01)
    ind = PhaseIndication(phase_number=0, all_red_required=True)
    frame = build_phase_indication(ind, cfg)
    assert frame.data[-1] == cfg.flag_all_red


def test_phase_indication_rejects_out_of_range():
    cfg = ProtocolConfig()
    with pytest.raises(ValueError):
        build_phase_indication(PhaseIndication(phase_number=256), cfg)


def test_heartbeat_link_lost():
    hb = Heartbeat(max_missed=3)
    for _ in range(3):
        hb.record(False)
    assert hb.link_lost
    hb.record(True)
    assert not hb.link_lost


def test_controller_link_request_phase_via_null():
    link = ControllerLink(
        transport=NullTransport(),
        protocol=ProtocolConfig(address=0x01),
        phase_mapping={"P_N": 1, "P_MT": 2},
        special_phases={"all_red": 0},
    )
    link.open()
    link.request_phase("P_N", reason="score")
    assert len(link.transport.sent) == 1
    decoded = decode_frame(link.transport.sent[0], link.protocol)
    assert decoded.data[0] == link.protocol.cmd_phase_control
    assert decoded.data[1] == 1  # 映射后的相位编号


def test_controller_link_unknown_phase_raises():
    link = ControllerLink(
        transport=NullTransport(),
        protocol=ProtocolConfig(),
        phase_mapping={"P_N": 1},
        special_phases={"all_red": 0},
    )
    with pytest.raises(KeyError):
        link.request_phase("P_E")


def test_controller_link_all_red_required_none_phase():
    link = ControllerLink(
        transport=NullTransport(),
        protocol=ProtocolConfig(),
        phase_mapping={},
        special_phases={"all_red": 0},
    )
    link.open()
    link.request_phase(None, all_red_required=True)
    decoded = decode_frame(link.transport.sent[0], link.protocol)
    assert decoded.data[0] == link.protocol.cmd_phase_control
    assert decoded.data[1] == 0  # all_red 编号


def test_controller_link_takeover_detach_sends_frames():
    link = ControllerLink(
        transport=NullTransport(),
        protocol=ProtocolConfig(),
        phase_mapping={},
        special_phases={},
    )
    link.open()
    link.request_takeover()
    link.request_detach()
    assert len(link.transport.sent) == 2
    assert decode_frame(link.transport.sent[0]).data[0] == link.protocol.cmd_takeover
    assert decode_frame(link.transport.sent[1]).data[0] == link.protocol.cmd_detach
