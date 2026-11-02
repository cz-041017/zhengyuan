"""GB/T 20999-2017 帧编解码（数据链路层，HDLC 基础）。

依据：GB/T 20999-2017《交通信号控制机与上位机间的数据通信协议》
（数据链路层引用 GB/T 17547-1998 HDLC 高级数据链路控制规程）。

警告：本模块按标准的公开结构 + 标准 HDLC 实现，但**字节级细节**
（起始符字节数、命令字、CRC 多项式/初值、地址域宽度、波特率）必须对照
海康官方《XHJ-CW-GA-HK3 通信协议》确认后方可上线。所有待确认常量集中在
:class:`ProtocolConfig`，不要散落到业务代码里。
"""
from __future__ import annotations

from dataclasses import dataclass

# HDLC 帧定界与字节转义（GB/T 17547-1998）
FLAG = 0x7E
ESCAPE = 0x7D
ESCAPE_XOR = 0x20


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE（poly 0x1021, init 0xFFFF, 大端传输）。

    校验值：``crc16_ccitt(b"123456789") == 0x29B1``。
    待确认是否为本机采用的校验多项式/初值。
    """
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def _stuff(data: bytes) -> bytes:
    """发送前转义：FLAG/ESCAPE 字节前插入 ESCAPE 并异或 0x20。"""
    out = bytearray()
    for b in data:
        if b in (FLAG, ESCAPE):
            out.append(ESCAPE)
            out.append(b ^ ESCAPE_XOR)
        else:
            out.append(b)
    return bytes(out)


def _unstuff(data: bytes) -> bytes:
    """接收后反转义。"""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == ESCAPE:
            if i + 1 >= n:
                raise ValueError("frame ends inside an escape sequence")
            out.append(data[i + 1] ^ ESCAPE_XOR)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


@dataclass(frozen=True)
class ProtocolConfig:
    """待确认的协议常量（对照海康官方文档回填）。

    集中于此，方便上线前一次性替换，避免散落在各处。
    """

    address: int = 0x01            # 信号机地址（源站号），范围 0x01-0xEF
    control: int = 0x00            # 控制域（帧类型/序号，TBD）
    crc_init: int = 0xFFFF         # CRC 初值
    # 应用层命令字（TBD，须对照海康官方《XHJ-CW-GA-HK3 通信协议》确认）
    cmd_phase_control: int = 0x30  # 相位指示 / 手动步进
    cmd_status_query: int = 0x31   # 状态查询（兼作心跳）
    cmd_takeover: int = 0x32       # 交权 / 接管
    cmd_detach: int = 0x33         # 摘除 / 退回本地
    # 相位指示的附加标志字节（bit0 = all-red 请求）
    flag_all_red: int = 0x01


@dataclass(frozen=True)
class Frame:
    """GB/T 20999-2017 数据帧（去掉首尾 FLAG 与 CRC 后的核心字段）。"""

    address: int      # 信号机地址
    control: int      # 控制域
    data: bytes       # 信息域：命令字 + 参数


def encode_frame(frame: Frame, config: ProtocolConfig | None = None) -> bytes:
    """编码一帧：``FLAG + stuffed(address|control|data|CRC) + FLAG``。"""
    cfg = config or ProtocolConfig()
    body = bytes([frame.address, frame.control]) + frame.data
    crc = crc16_ccitt(body, cfg.crc_init)
    payload = body + crc.to_bytes(2, "big")
    return bytes([FLAG]) + _stuff(payload) + bytes([FLAG])


def decode_frame(raw: bytes, config: ProtocolConfig | None = None) -> Frame:
    """解码一帧：剥 FLAG → 反转义 → 校验 CRC → 拆字段。"""
    cfg = config or ProtocolConfig()
    data = raw.strip(bytes([FLAG]))  # 去掉首尾 FLAG（含可能的填充字节）
    if not data:
        raise ValueError("empty frame")
    unstuffed = _unstuff(data)
    if len(unstuffed) < 4:  # address + control + crc(2)
        raise ValueError("frame too short")
    body = unstuffed[:-2]
    crc_recv = int.from_bytes(unstuffed[-2:], "big")
    crc_calc = crc16_ccitt(body, cfg.crc_init)
    if crc_recv != crc_calc:
        raise ValueError(
            f"CRC mismatch: received 0x{crc_recv:04X}, calculated 0x{crc_calc:04X}"
        )
    return Frame(address=body[0], control=body[1], data=body[2:])
