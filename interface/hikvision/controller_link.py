"""控制接口顶层：串联决策层 → 相位映射 → 协议 → 传输 → 心跳/接管。

软请求边界：只发相位指示，绝不直接下发灯色或配时。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from interface.hikvision.adapter import (
    PhaseIndication,
    SignalStatus,
    build_detach,
    build_phase_indication,
    build_status_query,
    build_takeover,
)
from interface.hikvision.heartbeat import Heartbeat
from interface.hikvision.protocol import ProtocolConfig, decode_frame, encode_frame
from interface.hikvision.takeover import LinkMode, TakeoverController
from interface.hikvision.transports import Transport


@dataclass
class ControllerLink:
    """RK3588 上位机 ↔ 海康 XHJ-CW-GA-HK3 信号机的控制接口。"""

    transport: Transport
    protocol: ProtocolConfig
    phase_mapping: Mapping[str, int]     # 决策层 phase_id → 信号机相位编号
    special_phases: Mapping[str, int]    # 特殊态（all_red/flash/off）→ 相位编号
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    takeover: TakeoverController = field(default_factory=TakeoverController)
    _requested_phase: int | None = field(default=None, init=False, repr=False)

    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self.transport.close()

    # ------------------------------------------------------------------ 相位指示
    def request_phase(
        self,
        phase_id: str | None,
        reason: str = "",
        all_red_required: bool = False,
    ) -> None:
        """把决策层 phase_id 映射成信号机相位编号并下发指示（软请求）。"""
        if phase_id is None:
            if all_red_required:
                phase_number = self.special_phases.get("all_red")
                if phase_number is None:
                    raise KeyError("all_red phase number not configured")
            else:
                return  # 决策层未选中相位，不发指示
        else:
            phase_number = self.phase_mapping.get(phase_id)
            if phase_number is None:
                raise KeyError(
                    f"no signal phase mapped for decision phase {phase_id!r}"
                )
        ind = PhaseIndication(
            phase_number=phase_number,
            reason=reason,
            all_red_required=all_red_required,
        )
        frame = build_phase_indication(ind, self.protocol)
        self._requested_phase = phase_number
        self.transport.send(encode_frame(frame, self.protocol))

    @property
    def last_requested_phase(self) -> int | None:
        """最近一次请求的信号机相位编号（用于反馈确认，见 :meth:`poll` 的 TODO）。"""
        return self._requested_phase

    # ------------------------------------------------------------------ 心跳/状态
    def poll(self) -> SignalStatus | None:
        """状态查询 + 心跳记账；连续无响应达到阈值时标记故障。"""
        self.transport.send(
            encode_frame(build_status_query(self.protocol), self.protocol)
        )
        raw = self.transport.receive(self.heartbeat.timeout_s)
        responded = raw is not None
        self.heartbeat.record(responded)
        if self.heartbeat.link_lost:
            self.takeover.mark_fault()
        if raw is None:
            return None
        status = SignalStatus(raw=decode_frame(raw, self.protocol).data)
        # TODO(反馈闭环)：状态响应结构 TBD，暂无法解析信号机是否已执行所请求相位。
        # 结构确认后，在此比较 status.current_phase_number 与 self._requested_phase，
        # 未确认则按 docs/decision-algorithm-design.md §7 进故障/回退流程。
        return status

    # ------------------------------------------------------------------ 接管/摘除
    def request_takeover(self) -> None:
        self.transport.send(encode_frame(build_takeover(self.protocol), self.protocol))
        self.takeover.request_takeover()

    def request_detach(self) -> None:
        self.transport.send(encode_frame(build_detach(self.protocol), self.protocol))
        self.takeover.request_detach()

    @property
    def mode(self) -> LinkMode:
        return self.takeover.mode
