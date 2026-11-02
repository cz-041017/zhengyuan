"""接管 / 摘除（交权）状态。"""
from __future__ import annotations

from enum import Enum


class LinkMode(str, Enum):
    LOCAL = "local"      # 信号机本地自动运行
    REMOTE = "remote"    # 上位机远程控制（已接管）
    FAULT = "fault"      # 链路故障（应回退本地并报警）


class TakeoverController:
    """维护上位机侧的远程控制意图。

    ``mode`` 反映上位机**已下发请求**后的状态（乐观）。信号机是否实际授权
    需待状态响应确认（响应结构 TBD，见 ``controller_link.poll`` 的 TODO）。
    链路故障由 :meth:`mark_fault` 标记，调用方应据此回退本地并报警。
    """

    def __init__(self):
        self._mode = LinkMode.LOCAL

    @property
    def mode(self) -> LinkMode:
        return self._mode

    def request_takeover(self) -> None:
        self._mode = LinkMode.REMOTE

    def request_detach(self) -> None:
        self._mode = LinkMode.LOCAL

    def mark_fault(self) -> None:
        self._mode = LinkMode.FAULT
