"""生产入口（骨架）：相机感知 → 决策层 → 海康信号机，软请求模式。

这是板子（RK3588）上实际运行的主程序。它把几块已经造好的组件串成一条
永不退出的控制回路：

    感知 JSON 文件 ──读──> 决策层 DecisionLayer.tick() ──> ControllerLink ──> 信号机
                                │                                     ▲
                                └────────── 心跳轮询 / 状态回读（TBD）──┘

边界说明（务必理解，不要破坏）：
  * 软请求：本程序只发「相位指示」，绝不直接下发灯色或配时。最小/最大绿、
    黄灯、全红、安全联锁由信号机内部裁决，信号机可拒绝或推迟切换。
  * 遇问题正常退出并告警：感知断流 → 决策层降级 FAILSAFE → 本程序请求全红
    并告警；链路丢失 → 心跳判故障 → 摘除交权；收到 SIGINT/SIGTERM →
    摘除 + 关闭传输 + 落日志，优雅退出。

用法（仓库根目录）::

    uv run python main.py --config configs/deployment.yaml

离线冒烟（不接真机，NullTransport + 临时感知文件）::

    uv run python main.py --config configs/deployment.yaml --max-ticks 5

上线前必须回填的占位项集中在 ``configs/deployment.yaml`` 与
``interface/hikvision/phase_mapping.yaml``，见各自的 TODO 注释。
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from interface.hikvision.controller_link import ControllerLink
from interface.hikvision.heartbeat import Heartbeat
from interface.hikvision.protocol import ProtocolConfig
from interface.hikvision.takeover import LinkMode
from interface.hikvision.transports import (
    NullTransport,
    SerialTransport,
    TcpTransport,
    Transport,
)
from traffic_control.decision_layer import DecisionLayer
from traffic_control.degradation import DegradationMode
from traffic_control.models import ControllerMode, ControllerState, VehicleObservation
from traffic_control.perception import (
    DownstreamReport,
    RawDetection,
    downstream_occupancy,
    normalize_detections,
)
from traffic_control.telemetry import NullTelemetrySink

log = logging.getLogger("traffic_control.main")


# --------------------------------------------------------------------------- #
# 信号机相位状态跟踪（软请求下的「本地乐观模型」）
# --------------------------------------------------------------------------- #
class SignalControllerDriver:
    """跟踪信号机当前相位，并把决策层的相位建议下发给信号机。

    真实相位 / 绿灯开始时刻由信号机状态响应回读（``ControllerLink.poll`` 的
    TODO，响应结构 TBD）。在结构确认前，这里用「乐观模型」：下发切换请求后
    就假设信号机在清空（黄灯 + 全红）结束后切到新相位。
    """

    def __init__(
        self,
        link: ControllerLink,
        phases: Mapping[str, Any],
        initial_phase: str,
    ) -> None:
        self.link = link
        self.phases = phases
        self.initial_phase = initial_phase
        self.current_phase_id: str | None = None
        self.phase_started_at: float = 0.0

    def state(self) -> ControllerState:
        """构造给决策层的控制器状态（NORMAL 模式）。"""
        return ControllerState(
            mode=ControllerMode.NORMAL,
            current_phase_id=self.current_phase_id,
            phase_started_at=self.phase_started_at,
        )

    def ensure_initial_green(self, now: float) -> None:
        """启动序列结束后，由调用方选第一个绿灯（决策层不提议首相位）。"""
        if self.current_phase_id is not None:
            return
        self.current_phase_id = self.initial_phase
        self.phase_started_at = now
        self.link.request_phase(self.initial_phase, reason="initial_green")

    def apply(self, decision: Any, now: float) -> None:
        """把决策层的 PhaseDecision 下发给信号机（软请求，仅在切换时发）。"""
        proposed = decision.selected_phase_id
        all_red_required = "all_red_required" in decision.constraints_applied

        # no_feasible_successor / 最大绿硬上限：请求全红（P1 拥堵时不可行）。
        if not decision.feasible and all_red_required:
            self.link.request_phase(None, reason=decision.reason, all_red_required=True)
            return

        if decision.feasible and proposed is not None and proposed != self.current_phase_id:
            self.link.request_phase(
                proposed, reason=decision.reason, all_red_required=all_red_required
            )
            # 乐观：清空（黄 + 全红）结束后切到新相位。
            phase = self.phases.get(proposed)
            clearance = 0.0
            if phase is not None:
                clearance = getattr(phase, "yellow_s", 0.0) + getattr(phase, "all_red_s", 0.0)
            self.current_phase_id = proposed
            self.phase_started_at = now + clearance
            log.info("switch -> %s (reason=%s)", proposed, decision.reason)


# --------------------------------------------------------------------------- #
# 感知输入读取（JSON 文件快照）
# --------------------------------------------------------------------------- #
def read_perception(
    path: Path, now: float
) -> tuple[tuple[VehicleObservation, ...], dict[str, float | None]]:
    """读取一帧感知 JSON，转换为决策层输入。

    文件缺失 / 损坏 / 不新鲜时不抛异常，而是返回空输入：决策层把「无数据」
    与「没车」区分开，会走降级（FAILSAFE）并告警——这正是「遇问题正常退出
    并告警」所要的行为。感知侧断流 ≠ 没车。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return (), {}

    frame_at = float(raw.get("observed_at", now))

    # 车辆检测 → VehicleObservation（lane_id 会做规范 ID 校验，拼错快速失败）。
    detections = raw.get("detections", [])
    observations: Sequence[VehicleObservation] = normalize_detections(
        RawDetection(
            track_id=int(d["track_id"]),
            lane_id=str(d["lane_id"]),
            observed_at=float(d.get("observed_at", frame_at)),
            speed_mps=(
                float(d["speed_mps"]) if d.get("speed_mps") is not None else None
            ),
            confidence=(
                float(d["confidence"]) if d.get("confidence") is not None else None
            ),
        )
        for d in detections
    )

    # 下游占用 → {lane_id: occupancy|None}（P1 溢流保护）。
    reports = raw.get("downstream", [])
    downstream = downstream_occupancy(
        DownstreamReport(
            lane_id=str(r["lane_id"]),
            occupancy=(float(r["occupancy"]) if r.get("occupancy") is not None else None),
            observed_at=float(r.get("observed_at", frame_at)),
        )
        for r in reports
    )

    return tuple(observations), downstream


# --------------------------------------------------------------------------- #
# 传输 / 链路构建
# --------------------------------------------------------------------------- #
def build_transport(cfg: Mapping[str, Any]) -> Transport:
    t = cfg["type"]
    if t is None or t == "null":  # YAML `type: null` 解析为 None
        return NullTransport()
    if t == "serial":
        s = cfg.get("serial", {})
        return SerialTransport(
            port=s.get("port", "/dev/ttyS0"),
            baudrate=int(s.get("baudrate", 9600)),
            parity=s.get("parity", "none"),
        )
    if t == "tcp":
        c = cfg.get("tcp", {})
        return TcpTransport(host=c.get("host", "127.0.0.1"), port=int(c.get("port", 6000)))
    raise ValueError(f"unsupported transport type: {t!r}")


def build_link(cfg: Mapping[str, Any]) -> ControllerLink:
    mapping_path = Path(cfg["phase_mapping"])
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    hb = cfg.get("heartbeat", {})
    return ControllerLink(
        transport=build_transport(cfg["transport"]),
        protocol=ProtocolConfig(address=int(mapping.get("controller_address", 0x01))),
        phase_mapping=mapping["phases"],
        special_phases=mapping.get("special", {}),
        heartbeat=Heartbeat(
            interval_s=float(hb.get("interval_s", 1.0)),
            timeout_s=float(hb.get("timeout_s", 3.0)),
            max_missed=int(hb.get("max_missed", 3)),
        ),
    )


# --------------------------------------------------------------------------- #
# 指挥中心接管 / 摘除（占位：命令文件）
# --------------------------------------------------------------------------- #
def check_control_room(
    layer: DecisionLayer, link: ControllerLink, cmd_path: Path | None, now: float
) -> None:
    """按命令文件执行指挥中心接管/摘除（占位，跨进程通信方式待定）。

    命令文件形如 ``{"command": "takeover"|"release"}``，由指挥中心写入。
    决策层接管后完全静默（不再提案），信号机交回本地自动运行。
    """
    if cmd_path is None:
        return
    try:
        cmd = json.loads(cmd_path.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if cmd == "takeover" and not layer.manual_override:
        layer.takeover(now)
        link.request_takeover()
        log.warning("control room takeover")
    elif cmd == "release" and layer.manual_override:
        layer.release(now)
        log.info("control room release")


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
def run_loop(
    layer: DecisionLayer,
    link: ControllerLink,
    driver: SignalControllerDriver,
    perception_path: Path,
    tick_period_s: float,
    heartbeat_interval_s: float,
    cmd_path: Path | None,
    max_ticks: int | None,
) -> None:
    ticks = 0
    last_poll = 0.0
    prev_degradation: DegradationMode | None = None

    while True:
        now = time.time()

        check_control_room(layer, link, cmd_path, now)

        observations, downstream = read_perception(perception_path, now)
        result = layer.tick(now, driver.state(), observations, downstream)

        # 启动序列结束后选首绿；决策层不提议「第一个相位」。
        if result.startup is None and driver.current_phase_id is None:
            driver.ensure_initial_green(now)
        else:
            driver.apply(result.decision, now)

        # FAILSAFE（感知断流）→ 请求全红，交给信号机本地兜底；只在进入态发一次。
        if result.degradation is DegradationMode.FAILSAFE:
            if prev_degradation is not DegradationMode.FAILSAFE:
                link.request_phase(None, reason="failsafe", all_red_required=True)
                log.error("FAILSAFE: perception dead — requested all-red")
        prev_degradation = result.degradation

        # 心跳 / 状态轮询。
        if now - last_poll >= heartbeat_interval_s:
            last_poll = now
            link.poll()
            if link.mode is LinkMode.FAULT:
                log.error("link fault — signal controller unreachable")

        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(max(0.0, tick_period_s - (time.time() - now)))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="smart-traffic-control 生产入口")
    p.add_argument("--config", default="configs/deployment.yaml", help="部署配置 YAML")
    p.add_argument("--decision-config", default=None, help="覆盖部署配置里的 decision_config")
    p.add_argument("--max-ticks", type=int, default=None, help="跑 N 个 tick 后停止（离线冒烟用）")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    decision_config = args.decision_config or cfg["decision_config"]

    sink = NullTelemetrySink(logger=logging.getLogger("traffic_control.telemetry"))
    layer = DecisionLayer.from_yaml(decision_config, sink=sink)

    link = build_link(cfg["link"])
    link.open()
    log.info("transport opened (%s)", cfg["link"]["transport"]["type"])

    # 请求远程控制权（乐观；是否授权待状态响应确认，TBD）。
    link.request_takeover()

    phases = dict(layer.runtime.phases)
    initial_phase = cfg.get("initial_phase") or next(iter(phases))
    driver = SignalControllerDriver(link, phases, initial_phase)

    # 优雅退出：摘除交权 + 关闭传输。
    def stop(signum: int, _frame: object) -> None:
        log.info("signal %s received — shutting down", signum)
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass  # 非主线程 / 平台不支持时忽略

    cmd_path = cfg.get("control_room", {}).get("command_path")
    try:
        run_loop(
            layer=layer,
            link=link,
            driver=driver,
            perception_path=Path(cfg["perception"]["input_path"]),
            tick_period_s=float(cfg["tick_period_s"]),
            heartbeat_interval_s=float(
                cfg["link"]["heartbeat"].get("interval_s", 1.0)
            ),
            cmd_path=Path(cmd_path) if cmd_path else None,
            max_ticks=args.max_ticks,
        )
    except KeyboardInterrupt:
        pass
    finally:
        log.info("detaching from signal controller")
        try:
            link.request_detach()
        finally:
            link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
