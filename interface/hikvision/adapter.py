"""应用层：相位指示 / 状态查询 / 接管摘除命令 + 状态解析（软请求）。"""
from __future__ import annotations

from dataclasses import dataclass

from interface.hikvision.protocol import Frame, ProtocolConfig


@dataclass(frozen=True)
class PhaseIndication:
    """向信号机下发的相位指示（软请求）。

    只指示「目标相位」，灯色/配时由信号机内部裁决。
    """

    phase_number: int            # 信号机内部相位编号（经 phase_mapping 映射）
    reason: str = ""             # 决策理由（仅用于日志，不下发）
    all_red_required: bool = False  # 决策层请求全红（P1 拥堵 / 最大绿硬上限）


def _command(cmd: int, config: ProtocolConfig) -> Frame:
    """构造单命令字帧（无参数）。"""
    return Frame(address=config.address, control=config.control, data=bytes([cmd]))


def build_phase_indication(ind: PhaseIndication, config: ProtocolConfig) -> Frame:
    """构造相位指示帧。

    信息域 = ``[cmd_phase_control, phase_number, flag]``，flag 的 bit0 表示 all-red。
    """
    if not 0 <= ind.phase_number <= 0xFF:
        raise ValueError(f"phase_number out of byte range: {ind.phase_number}")
    flag = config.flag_all_red if ind.all_red_required else 0x00
    data = bytes([config.cmd_phase_control, ind.phase_number, flag])
    return Frame(address=config.address, control=config.control, data=data)


def build_status_query(config: ProtocolConfig) -> Frame:
    """构造状态查询帧（兼作心跳轮询）。"""
    return _command(config.cmd_status_query, config)


def build_takeover(config: ProtocolConfig) -> Frame:
    """构造交权 / 接管帧（请求远程控制权）。"""
    return _command(config.cmd_takeover, config)


def build_detach(config: ProtocolConfig) -> Frame:
    """构造摘除 / 退回本地帧（释放远程控制权）。"""
    return _command(config.cmd_detach, config)


@dataclass(frozen=True)
class SignalStatus:
    """从信号机状态响应解析出的字段。

    字段集待确认（响应帧结构以海康官方协议为准），当前只保留原始字节，
    并提供占位的相位编号读取。
    """

    raw: bytes

    @property
    def current_phase_number(self) -> int | None:
        """当前信号机相位编号。

        响应 data = ``[cmd_status_query 回显, phase_number, ...]``（TBD）。
        TODO：先校验 data[0] 的命令回显与状态查询一致，再取相位编号，避免误读外来帧。
        """
        return self.raw[1] if len(self.raw) >= 2 else None
