"""SUMO <-> 逻辑决策层 桥接层（仿真台核心）。

职责：
1. 解析 9×9 网格路网，建立「SUMO 车道/流向 -> 决策层 canonical movement id」映射；
2. 每个仿真步从 SUMO 读取车辆状态，构造感知层输入（VehicleObservation），
   经 QueueEstimator 估计排队，再聚合为每相位 PhaseDemand；
3. 调用本项目 PhaseStateMachine 得到 PhaseDecision，映射为 SUMO 信号灯相位；
4. 用 SUMO 原生 yellow/all-red 语义实现黄灯 + 全红清空，回传执行反馈；
5. 采集等待时间、排队、速度、行程、P0 违规等指标。

依赖：sumolib / libsumo（SUMO Python 工具，PYTHONPATH=/usr/share/sumo/tools）。
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traffic_control.config_loader import load_config
from traffic_control.degradation import DegradationMode
from traffic_control.feedback import FeedbackMonitor, FeedbackStatus
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    PhaseDecision,
    SignalPhase,
    VehicleObservation,
)
from traffic_control.phasing import phase_groups
from traffic_control.queue_estimator import QueueEstimator
from traffic_control.runtime import build_runtime
from traffic_control.safety import SafetyGuard
from traffic_control.scoring import aggregate_demand
from traffic_control.state_machine import PhaseDemand, PhaseStateMachine

# ---------------------------------------------------------------------------
# 相位 <-> 路口来向 映射（模式 A：minor_only，四相位）
# ---------------------------------------------------------------------------
PHASE_APPROACH = {"P_N": "N", "P_E": "E", "P_S": "S", "P_W": "W"}
APPROACH_NAME = {"N": "north", "E": "east", "S": "south", "W": "west"}


def canonical_movement(approach: str, direction: str) -> str:
    """SUMO 连接方向 -> 决策层 canonical lane id（右转折入直行）。"""
    move = "left" if direction == "l" else "through"
    return f"{APPROACH_NAME[approach]}_{move}"


@dataclass
class NetInfo:
    """由 sumolib 解析路网得到的静态拓扑信息。"""

    edge_to_junction: dict[str, str] = field(default_factory=dict)  # edge -> TLS 节点
    junction_approach: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # junction -> {in_edge: approach}
    junction_dir: dict[str, dict[tuple[str, str], str]] = field(
        default_factory=dict
    )  # junction -> {(in_edge, out_edge): dir}
    junction_downstream: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=dict
    )  # junction -> {approach: {through/left: out_edge}}
    approach_edges: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, net_path: str | Path) -> "NetInfo":
        import sumolib  # 延迟导入，避免非 SUMO 环境报错

        net = sumolib.net.readNet(str(net_path))
        info = cls()

        for node in net.getNodes():
            if node.getType() != "traffic_light":
                continue
            incoming = node.getIncoming()
            if not incoming:
                continue
            jid = node.getID()
            for edge in incoming:
                eid = edge.getID()
                info.edge_to_junction[eid] = jid
                info.approach_edges.add(eid)
                dx, dy = _edge_heading(edge)
                info.junction_approach.setdefault(jid, {})[eid] = _approach_of(
                    dx, dy
                )

            # 逐连接记录方向，并推导每个来向的直行/左转下游边。
            dirs: dict[tuple[str, str], str] = {}
            downstream: dict[str, dict[str, str]] = {}
            for conn in node.getConnections():
                in_edge = conn.getFrom().getID()
                out_edge = conn.getTo().getID()
                d = conn.getDirection()
                dirs[(in_edge, out_edge)] = d
                approach = info.junction_approach[jid][in_edge]
                if d == "s":
                    downstream.setdefault(approach, {})["through"] = out_edge
                elif d == "l":
                    downstream.setdefault(approach, {})["left"] = out_edge
            info.junction_dir[jid] = dirs
            info.junction_downstream[jid] = downstream
        return info


def _edge_heading(edge: Any) -> tuple[float, float]:
    shape = edge.getShape()
    (x0, y0), (x1, y1) = shape[0], shape[-1]
    return x1 - x0, y1 - y0


def _approach_of(dx: float, dy: float) -> str:
    """由驶入边朝向推断来向：朝东 -> 西侧来车(W)，朝南 -> 北侧来车(N)。"""
    if abs(dx) >= abs(dy):
        return "W" if dx > 0 else "E"
    return "S" if dy > 0 else "N"


# ---------------------------------------------------------------------------
# 单个路口控制器
# ---------------------------------------------------------------------------
@dataclass
class JunctionController:
    junction_id: str
    netinfo: NetInfo
    runtime: Any
    phases: dict[str, SignalPhase]
    state_machine: PhaseStateMachine
    feedback: FeedbackMonitor
    queue_estimator: QueueEstimator
    spillback: Any
    safety: SafetyGuard
    config: dict[str, Any]
    state: ControllerState
    link_approach: list[str]  # 与 SUMO TLS 状态串对齐的每个 link 的来向

    clearance: dict[str, Any] | None = None
    last_decision: PhaseDecision | None = None
    feedback_status: str = FeedbackStatus.IDLE.value
    # 统计
    switches: int = 0
    p0_preempts: int = 0
    p0_p1_conflicts: int = 0
    max_green_hits: int = 0
    countdown_switches: int = 0

    def apply_tls(self, traci: Any, mode: str, phase_id: str | None) -> None:
        """把 (green/yellow/all_red, phase) 映射为 SUMO 红黄绿状态串。"""
        if mode == "all_red":
            state_str = "r" * len(self.link_approach)
        elif mode == "yellow":
            appr = PHASE_APPROACH[phase_id]
            state_str = "".join(
                "y" if a == appr else "r" for a in self.link_approach
            )
        else:  # green
            appr = PHASE_APPROACH[phase_id]
            state_str = "".join(
                "G" if a == appr else "r" for a in self.link_approach
            )
        traci.trafficlight.setRedYellowGreenState(self.junction_id, state_str)


def _fresh_state(state: ControllerState, **overrides: Any) -> ControllerState:
    d = {
        "mode": state.mode,
        "current_phase_id": state.current_phase_id,
        "phase_started_at": state.phase_started_at,
        "signal_colors": state.signal_colors,
    }
    d.update(overrides)
    return ControllerState(**d)


def _compute_demands(
    ctrl: JunctionController,
    observations: list[VehicleObservation],
    now: float,
    traci: Any,
) -> dict[str, PhaseDemand]:
    """观测 -> QueueEstimator -> 每相位 PhaseDemand。"""
    lane_states = ctrl.queue_estimator.update(observations, now)

    # P1：下游占用（按 movement 维度喂给 SpillbackMonitor）。
    downstream = ctrl.netinfo.junction_downstream.get(ctrl.junction_id, {})
    for approach, edges in downstream.items():
        for move in ("through", "left"):
            out_edge = edges.get(move)
            if out_edge is None:
                continue
            occ = traci.edge.getLastStepOccupancy(out_edge)
            lane_id = canonical_movement(approach, "l" if move == "left" else "s")
            ctrl.spillback.update(lane_id, occ)

    demands: dict[str, PhaseDemand] = {}
    for phase_id, phase in ctrl.phases.items():
        counts = [
            lane_states[m].vehicle_count if m in lane_states else 0
            for m in phase.movement_ids
        ]
        waits = [
            lane_states[m].oldest_wait_s if m in lane_states else 0.0
            for m in phase.movement_ids
        ]
        blocked = any(
            ctrl.spillback.is_blocked(m) for m in phase.movement_ids
        )
        vehicle_count, oldest_wait = aggregate_demand(counts, waits)
        demands[phase_id] = PhaseDemand(
            vehicle_count=vehicle_count,
            oldest_wait_s=oldest_wait,
            downstream_blocked=blocked,
            green_wave_active=False,
        )
    return demands


# ---------------------------------------------------------------------------
# 主仿真
# ---------------------------------------------------------------------------
@dataclass
class SimulationResult:
    config: dict[str, Any]
    rows: list[dict[str, Any]]
    per_junction: dict[str, dict[str, Any]]


class Simulation:
    def __init__(
        self,
        net_path: str | Path,
        config: dict[str, Any],
        *,
        controller: str = "adaptive",
        fixed_green_s: float = 30.0,
    ) -> None:
        self.netinfo = NetInfo.load(net_path)
        self.config = config
        self.controller = controller
        self.fixed_green_s = fixed_green_s
        self.runtimes: dict[str, Any] = {}
        self.controllers: dict[str, JunctionController] = {}

    # -- 装配 ---------------------------------------------------------------
    def _build_controllers(self, traci: Any) -> None:
        runtime = build_runtime(self.config)
        phases = runtime.phases
        queue_cfg = self.config["queue_estimation"]

        tls_ids = traci.trafficlight.getIDList()
        for jid in tls_ids:
            if jid not in self.netinfo.junction_approach:
                continue  # 无控制 link 的边界桩节点，跳过
            # 每个路口独立装配一套决策层运行时。
            rt = build_runtime(self.config)
            self.runtimes[jid] = rt

            links = traci.trafficlight.getControlledLinks(jid)
            link_approach: list[str] = []
            for link in links:
                in_lane, out_lane, _ = link[0]
                in_edge = _edge_of_lane(in_lane)
                out_edge = _edge_of_lane(out_lane)
                appr = self.netinfo.junction_approach[jid].get(in_edge)
                if appr is None:
                    # 内联 lane 或无映射，回退按出边推断
                    appr = self._approach_from_link(jid, in_edge, out_edge)
                link_approach.append(appr)

            qe = QueueEstimator(
                queue_speed_threshold_mps=queue_cfg["queue_speed_threshold_mps"],
                vehicle_length_m=queue_cfg["vehicle_length_m"],
                stopped_gap_m=queue_cfg["stopped_gap_m"],
                stale_after_s=queue_cfg["stale_after_s"],
            )
            guard = SafetyGuard(phases)

            # 初始相位按 (行+列) 错开，模拟已经运行一段时间。
            phase_ids = list(phases.keys())
            row, col = _junction_rc(jid)
            initial_phase = phase_ids[(row + col) % len(phase_ids)]

            self.controllers[jid] = JunctionController(
                junction_id=jid,
                netinfo=self.netinfo,
                runtime=rt,
                phases=phases,
                state_machine=rt.state_machine,
                feedback=rt.feedback_monitor,
                queue_estimator=qe,
                spillback=rt.spillback_monitor,
                safety=guard,
                config=self.config,
                state=ControllerState(
                    mode=ControllerMode.NORMAL,
                    current_phase_id=initial_phase,
                    phase_started_at=-20.0,
                ),
                link_approach=link_approach,
            )
            # 初始绿灯。
            self.controllers[jid].apply_tls(traci, "green", initial_phase)

    def _approach_from_link(
        self, jid: str, in_edge: str, out_edge: str
    ) -> str:
        # 正常网格中每个 link 的 in_edge 都已映射，此分支是防御性兜底：
        # 找得到就返回，找不到则 fail-fast，绝不静默返回错误来向污染 TLS 状态。
        known = self.netinfo.junction_approach.get(jid, {})
        if in_edge in known:
            return known[in_edge]
        raise RuntimeError(
            f"unmapped link {in_edge}->{out_edge} at junction {jid}"
        )

    # -- 单步 ---------------------------------------------------------------
    def _step_adaptive(
        self, traci: Any, now: float, vehicles: dict[str, dict[str, Any]]
    ) -> None:
        for jid, ctrl in self.controllers.items():
            obs = self._observations_for(ctrl, now, vehicles)
            demands = _compute_demands(ctrl, obs, now, traci)

            if ctrl.state.mode == ControllerMode.NORMAL:
                decision = ctrl.state_machine.tick(ctrl.state, now, demands)
                ctrl.last_decision = decision
                if (
                    decision.selected_phase_id is not None
                    and decision.selected_phase_id != ctrl.state.current_phase_id
                ):
                    # 安全守卫二次校验（防御性）。
                    check = ctrl.safety.check_candidate(
                        decision.selected_phase_id, ctrl.state, now
                    )
                    if not check.allowed:
                        # 不应发生：记录并维持现状。
                        ctrl.apply_tls(
                            traci, "green", ctrl.state.current_phase_id
                        )
                        continue
                    old_phase = ctrl.phases[ctrl.state.current_phase_id]
                    ctrl.clearance = {
                        "target": decision.selected_phase_id,
                        "stage": "yellow",
                        "started": now,
                        "yellow_s": old_phase.yellow_s,
                        "all_red_s": old_phase.all_red_s,
                    }
                    ctrl.state = _fresh_state(
                        ctrl.state, mode=ControllerMode.CLEARANCE
                    )
                    ctrl.feedback.observe(decision, ctrl.state, now)
                    ctrl.switches += 1
                    _bump_reason(ctrl, decision)
                    ctrl.apply_tls(
                        traci, "yellow", ctrl.state.current_phase_id
                    )
                else:
                    if decision.reason == "p0_wait_preempt":
                        ctrl.p0_preempts += 1
                    elif decision.reason == "p0_p1_conflict":
                        ctrl.p0_p1_conflicts += 1
                    ctrl.apply_tls(traci, "green", ctrl.state.current_phase_id)
            else:
                # 黄灯 / 全红清空推进。
                ctrl.feedback.observe(
                    PhaseDecision(None, "clearance_in_progress"),
                    ctrl.state,
                    now,
                )
                clr = ctrl.clearance
                assert clr is not None
                old_phase = ctrl.state.current_phase_id
                elapsed = now - clr["started"]
                if clr["stage"] == "yellow":
                    if elapsed >= clr["yellow_s"]:
                        clr["stage"] = "all_red"
                        clr["started"] = now
                        ctrl.state = _fresh_state(
                            ctrl.state, mode=ControllerMode.ALL_RED
                        )
                        ctrl.apply_tls(traci, "all_red", None)
                    else:
                        ctrl.apply_tls(traci, "yellow", old_phase)
                else:  # all_red
                    if elapsed >= clr["all_red_s"]:
                        target = clr["target"]
                        ctrl.state = ControllerState(
                            mode=ControllerMode.NORMAL,
                            current_phase_id=target,
                            phase_started_at=now,
                        )
                        ctrl.clearance = None
                        ctrl.feedback.observe(
                            PhaseDecision(None, "switch_complete"),
                            ctrl.state,
                            now,
                        )
                        ctrl.apply_tls(traci, "green", target)
                    else:
                        ctrl.apply_tls(traci, "all_red", None)

    def _step_fixed(
        self, traci: Any, now: float, vehicles: dict[str, dict[str, Any]]
    ) -> None:
        # 固定配时：四相位轮换，每相位 fixed_green_s 绿 + 3s 黄 + 1s 全红。
        yellow = 3.0
        all_red = 1.0
        cycle = 4 * (self.fixed_green_s + yellow + all_red)
        t = now % cycle
        phase_ids = list(self.config_phase_ids())
        n = len(phase_ids)
        seg = self.fixed_green_s + yellow + all_red
        idx = min(int(t // seg), n - 1)
        in_seg = t - idx * seg
        if in_seg < self.fixed_green_s:
            mode, pid = "green", phase_ids[idx]
        elif in_seg < self.fixed_green_s + yellow:
            mode, pid = "yellow", phase_ids[idx]
        else:
            mode, pid = "all_red", None
        for ctrl in self.controllers.values():
            ctrl.apply_tls(traci, mode, pid)

    def config_phase_ids(self) -> list[str]:
        return [g.phase_id for g in phase_groups("minor_only")]

    # -- 车辆观测 -----------------------------------------------------------
    def _observations_for(
        self,
        ctrl: JunctionController,
        now: float,
        vehicles: dict[str, dict[str, Any]],
    ) -> list[VehicleObservation]:
        obs: list[VehicleObservation] = []
        for vid, v in vehicles.items():
            road = v["road"]
            if road not in self.netinfo.edge_to_junction:
                continue
            if self.netinfo.edge_to_junction[road] != ctrl.junction_id:
                continue
            nxt = v["next"].get(road)
            if nxt is None:
                continue
            d = self.netinfo.junction_dir.get(ctrl.junction_id, {}).get(
                (road, nxt)
            )
            if d is None:
                continue
            appr = self.netinfo.junction_approach[ctrl.junction_id][road]
            lane = canonical_movement(appr, d)
            obs.append(
                VehicleObservation(
                    track_id=v["track_id"],
                    lane_id=lane,
                    observed_at=now,
                    speed_mps=v["speed"],
                )
            )
        return obs

    # -- 主循环 -------------------------------------------------------------
    def run(
        self,
        traci: Any,
        end_time: float,
        step_length: float = 1.0,
        *,
        log_every_s: float = 60.0,
        metrics_path: Path | None = None,
        screenshot_dir: Path | None = None,
        screenshot_every_s: float | None = None,
        view_zoom: float | None = None,
        view_offset: tuple[float, float] | None = None,
    ) -> SimulationResult:
        self._build_controllers(traci)
        if view_zoom is not None or view_offset is not None:
            try:
                if view_offset is not None:
                    traci.gui.setOffset("View #0", *view_offset)
                if view_zoom is not None:
                    traci.gui.setZoom("View #0", view_zoom)
            except Exception:
                pass  # 无 GUI（headless sumo）时忽略
        route_chain: dict[str, dict[str, str]] = {}
        track_registry: dict[str, int] = {}
        next_track = 0

        max_wait_target = self.config["optimization"]["max_wait_target_s"]
        rows: list[dict[str, Any]] = []
        last_log = -1e9
        last_shot = -1e9

        if screenshot_dir is not None:
            screenshot_dir.mkdir(parents=True, exist_ok=True)

        total_steps = int(round(end_time / step_length))
        arrived_total = 0
        for _ in range(total_steps):
            traci.simulationStep()
            now = traci.simulation.getTime()
            arrived_total += traci.simulation.getArrivedNumber()

            veh_ids = traci.vehicle.getIDList()
            vehicles: dict[str, dict[str, Any]] = {}
            total_wait = 0.0
            halting = 0
            speed_sum = 0.0
            p0_violations = 0
            max_wait = 0.0

            for vid in veh_ids:
                if vid not in track_registry:
                    track_registry[vid] = next_track
                    next_track += 1
                road = traci.vehicle.getRoadID(vid)
                speed = traci.vehicle.getSpeed(vid)
                wt = traci.vehicle.getWaitingTime(vid)
                if vid not in route_chain:
                    route = list(traci.vehicle.getRoute(vid))
                    route_chain[vid] = {
                        route[i]: route[i + 1] for i in range(len(route) - 1)
                    }
                vehicles[vid] = {
                    "track_id": track_registry[vid],
                    "road": road,
                    "speed": speed,
                    "next": route_chain[vid],
                }
                total_wait += wt
                speed_sum += speed
                if speed < 0.1:
                    halting += 1
                if wt > max_wait_target:
                    p0_violations += 1
                if wt > max_wait:
                    max_wait = wt

            if self.controller == "adaptive":
                self._step_adaptive(traci, now, vehicles)
            else:
                self._step_fixed(traci, now, vehicles)

            n = len(veh_ids)
            rows.append(
                {
                    "time": now,
                    "n_active": n,
                    "n_halting": halting,
                    "total_waiting_time": round(total_wait, 2),
                    "mean_speed": round(speed_sum / n, 3) if n else 0.0,
                    "p0_violations": p0_violations,
                    "max_waiting_time": round(max_wait, 2),
                    "arrived": arrived_total,
                }
            )

            if now - last_log >= log_every_s:
                last_log = now
                print(
                    f"[t={now:6.0f}s] active={n:5d} halting={halting:5d} "
                    f"mean_wait={total_wait/max(1,n):6.2f}s "
                    f"speed={speed_sum/max(1,n):5.2f} P0>{max_wait_target:g}={p0_violations}",
                    flush=True,
                )

            # 截图在「下一次 simulationStep」时落盘，因此这里请求的是当前状态，
            # 循环结束后再补一步把最后一张冲刷出来。
            if screenshot_dir is not None and screenshot_every_s is not None:
                if now - last_shot >= screenshot_every_s - 1e-9:
                    traci.gui.screenshot(
                        "View #0",
                        str(screenshot_dir / f"frame_{int(round(now)):05d}.png"),
                    )
                    last_shot = now

        if screenshot_dir is not None:
            # 冲刷最后一张截图。
            traci.simulationStep()

        if metrics_path is not None:
            self._write_metrics(metrics_path, rows)
        per_junction = {
            jid: {
                "switches": c.switches,
                "p0_preempts": c.p0_preempts,
                "p0_p1_conflicts": c.p0_p1_conflicts,
            }
            for jid, c in self.controllers.items()
        }
        return SimulationResult(self.config, rows, per_junction)

    @staticmethod
    def _write_metrics(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


def _edge_of_lane(lane_id: str) -> str:
    return lane_id.rsplit("_", 1)[0]


def _junction_rc(jid: str) -> tuple[int, int]:
    # "A0" -> (0, 0)；字母为行，数字为列。
    return ord(jid[0]) - ord("A"), int(jid[1:])


def _bump_reason(ctrl: JunctionController, decision: PhaseDecision) -> None:
    if decision.reason == "p0_wait_preempt":
        ctrl.p0_preempts += 1
    elif decision.reason == "max_green_reached":
        ctrl.max_green_hits += 1
    elif decision.reason == "countdown_complete":
        ctrl.countdown_switches += 1


def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    """把逐秒指标汇总为实验对比用的摘要。"""
    if not rows:
        return {}
    n = len(rows)
    active = [r["n_active"] for r in rows]
    halting = [r["n_halting"] for r in rows]
    waits = [r["total_waiting_time"] for r in rows]
    speeds = [r["mean_speed"] for r in rows]
    p0 = [r["p0_violations"] for r in rows]
    maxw = [r["max_waiting_time"] for r in rows]

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    # 平均每车等待时间：总等待秒数 / 活跃车辆数（时间平均）。
    denom = sum(active)
    return {
        "duration_s": rows[-1]["time"],
        "arrived": rows[-1]["arrived"],
        "mean_active": round(mean(active), 2),
        "mean_halting": round(mean(halting), 2),
        "mean_waiting_time_s": round(sum(waits) / denom, 3) if denom else 0.0,
        "mean_speed_mps": round(mean(speeds), 3),
        "p0_violation_peak": max(p0),
        "p0_violation_mean": round(mean([float(x) for x in p0]), 3),
        "max_waiting_time_s": round(max(maxw), 2),
    }


def run_simulation(
    sumocfg: str | Path,
    config_path: str | Path,
    *,
    controller: str = "adaptive",
    fixed_green_s: float = 30.0,
    end_time: float = 3600.0,
    step_length: float = 1.0,
    metrics_path: Path | None = None,
    gui: bool = False,
    delay_ms: float = 100.0,
    screenshot_dir: Path | None = None,
    screenshot_every_s: float | None = None,
    view_zoom: float | None = None,
    view_offset: tuple[float, float] | None = None,
) -> SimulationResult:
    """启动 SUMO 并运行一次仿真实验，返回逐秒指标与各路口统计。

    ``gui=True`` 时用 ``sumo-gui`` + 子进程 ``traci`` 启动可视化窗口，
    ``delay_ms`` 控制每仿真步之间的真实时间（默认 100ms，即约 10 帧/秒），
    便于实时观察信号灯与车流。
    """
    net_path = _net_path_from_cfg(sumocfg)
    config = load_config(config_path)

    t0 = time.time()
    if gui:
        import traci

        traci.start(
            [
                "sumo-gui",
                "-c",
                str(sumocfg),
                "--start",
                "--delay",
                str(delay_ms),
                "--window-size",
                "1280,800",
                "--no-warnings",
                "true",
            ]
        )
    else:
        import libsumo as traci

        traci.start(
            [
                "sumo",
                "-c",
                str(sumocfg),
                "--step-length",
                str(step_length),
                "--start",
                "--quit-on-end",
                "--no-warnings",
                "true",
            ]
        )
    sim = Simulation(net_path, config, controller=controller, fixed_green_s=fixed_green_s)
    try:
        result = sim.run(
            traci,
            end_time,
            step_length,
            metrics_path=metrics_path,
            screenshot_dir=screenshot_dir,
            screenshot_every_s=screenshot_every_s,
            view_zoom=view_zoom,
            view_offset=view_offset,
        )
    finally:
        try:
            traci.close()
        except Exception:
            # GUI 窗口被用户手动关闭时 close 可能抛异常，忽略。
            pass
    result.elapsed_wall_s = time.time() - t0
    return result


def _net_path_from_cfg(sumocfg: str | Path) -> str:
    """从 .sumocfg 里解析 net-file 的相对路径。"""
    import xml.etree.ElementTree as ET

    tree = ET.parse(str(sumocfg))
    root = tree.getroot()
    net = root.find(".//net-file")
    value = net.get("value")
    return str((Path(sumocfg).parent / value).resolve())
