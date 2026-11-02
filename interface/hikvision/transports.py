"""传输层：串口(RS-232/RS-485)、TCP 以太网、空实现（离线测试/仿真）。"""
from __future__ import annotations

import abc
import socket
from typing import Optional


class Transport(abc.ABC):
    """字节流传输抽象。"""

    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def send(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def receive(self, timeout: float) -> Optional[bytes]: ...

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...


class SerialTransport(Transport):
    """RS-232/RS-485 串口传输（pyserial 懒加载）。

    RK3588 的 /dev/ttySx 或 USB 转串口。波特率/校验位以海康官方为准（TBD）。
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 1.0,
        parity: str = "none",  # TBD：GB/T 20999-2017 注明 1 校验位，取值待海康官方确认
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.parity = parity
        self._ser = None

    def open(self) -> None:
        import serial  # 懒加载：未接串口的环境也能 import 本模块

        parity_map = {
            "none": serial.PARITY_NONE,
            "even": serial.PARITY_EVEN,
            "odd": serial.PARITY_ODD,
        }
        if self.parity not in parity_map:
            raise ValueError(f"unsupported parity: {self.parity!r}")
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=parity_map[self.parity],
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
        )

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def send(self, data: bytes) -> None:
        if self._ser is None:
            raise RuntimeError("serial transport not open")
        self._ser.write(data)

    def receive(self, timeout: float) -> Optional[bytes]:
        """读一帧：首 FLAG 之后到下一个 FLAG（stuffed 区不含 FLAG）。

        TODO：空闲填充 0x7E / 前导噪声会导致 read_until 分帧错位；上线前需
        改为带缓冲的状态机帧读取（与 TcpTransport 的 TODO 同理）。
        """
        if self._ser is None:
            raise RuntimeError("serial transport not open")
        self._ser.timeout = timeout
        first = self._ser.read_until(b"\x7e")
        if not first.endswith(b"\x7e"):
            return None  # 超时前未等到帧起始 FLAG
        rest = self._ser.read_until(b"\x7e")
        if not rest.endswith(b"\x7e"):
            return None  # 超时前未等到帧结束 FLAG
        return first + rest

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open


class TcpTransport(Transport):
    """以太网 TCP 传输。

    TODO（生产）: TCP 是字节流，一次 recv 可能返回半帧或多帧；上线前需在
    本类内加缓冲并按 FLAG 切帧。串口路径（RS-232）已用 read_until 切帧。
    """

    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("tcp transport not open")
        self._sock.sendall(data)

    def receive(self, timeout: float) -> Optional[bytes]:
        if self._sock is None:
            raise RuntimeError("tcp transport not open")
        self._sock.settimeout(timeout)
        try:
            return self._sock.recv(4096) or None
        except socket.timeout:
            return None

    @property
    def is_open(self) -> bool:
        return self._sock is not None


class NullTransport(Transport):
    """空传输：不回传任何数据，用于离线测试 / 仿真。"""

    def __init__(self):
        self._open = False
        self.sent: list[bytes] = []

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def receive(self, timeout: float) -> Optional[bytes]:
        return None

    @property
    def is_open(self) -> bool:
        return self._open
