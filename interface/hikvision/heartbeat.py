"""心跳与断线检测。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Heartbeat:
    """周期性状态查询充当心跳，连续无响应判定断线并请求回退本地。"""

    interval_s: float = 1.0
    timeout_s: float = 3.0
    max_missed: int = 3
    _missed: int = field(default=0, init=False, repr=False)

    def record(self, responded: bool) -> None:
        """记录一次轮询是否得到响应。"""
        self._missed = 0 if responded else self._missed + 1

    @property
    def link_lost(self) -> bool:
        """连续无响应达到阈值 → 链路丢失，应摘除交权并报警。"""
        return self._missed >= self.max_missed

    def reset(self) -> None:
        self._missed = 0
