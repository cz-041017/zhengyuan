"""panan_bridge.py —— 通用路网 <-> 逻辑决策层 桥接层（接口层）。

与 sumo_bridge.py（9×9 专用，假定四相位 P_N/P_E/P_S/P_W）不同，本模块面向
**任意拓扑的真实路网**：
  * 按每个信号灯路口的实际来向（按朝向角聚类到 N/E/S/W 象限）动态生成相位；
  * 三岔路口 -> 3 相位，四岔路口 -> 4 相位，自动适配；
  * 只控制 OSM/net 里标记为 traffic_light 的路口，其余交给 SUMO 默认（优先/环岛）；
  * 复用逻辑决策层组件（Scorer / PhaseStateMachine / QueueEstimator /
    SpillbackMonitor / FeedbackMonitor / SafetyGuard），**不修改逻辑层代码**。

指标（每个信号灯路口）：
  * 理论最大承载车流 = 进口道车道数 × 饱和流率（1800 veh/h/车道）
  * 实际通过车流 = 每小时实际驶离路口的车辆数
  * 平均通过路口时间 = 车辆进入进口道到驶离路口的平均时长
  * 平均等待时间 = 路口各进口道车辆的平均停等时长
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traffic_control.config_loader import load_config
from traffic_control.feedback import FeedbackMonitor
from traffic_control.models import (
    ControllerMode,
    ControllerState,
    PhaseDecision,
    SignalPhase,
    VehicleObservation,
)
from traffic_control.queue_estimator import QueueEstimator
from traffic_control.runtime import build_machine_config, build_score_config
from traffic_control.safety import SafetyGuard
from traffic_control.scoring import Scorer, SpillbackMonitor, aggregate_demand
from traffic_control.state_machine import PhaseDemand, PhaseStateMachine

APPROACH_NAME = {"N": "north", "E": "east", "S": "south", "W": "west"}
SATURATION_FLOW_VPH = 1800.0  # 每车道饱和流率（veh/h）
DETECT_DIST_M = 60.0  # 距停止线多近算「进入路口区域」（用于通过时间）

# 相位配时默认值（可调，非审核值）
DEFAULT_MIN_GREEN = 10.0
DEFAULT_MAX_GREEN = 60.0
DEFAULT_YELLOW = 3.0
DEFAULT_ALL_RED = 1.0


# ---------------------------------------------------------------------------
# 静态拓扑
# ---------------------------------------------------------------------------
@dataclass
class JunctionTopo:
    junction_id: str
    edge_approach: dict[str, str] = field(default_factory=dict)      # edge -> 来向
    approach_edges: dict[str, list[str]] = field(default_factory=dict)  # 来向 -> [edge]
    conn_dir: dict[tuple[str, str], str] = field(default_factory=dict)
    downstream: dict[str, dict[str, str]] = field(default_factory=dict)  # 来向 -> {through,left}
    incoming_lanes: int = 0


@dataclass
class NetInfo:
    edge_to_junction: dict[str, str] = field(default_factory=dict)
    junctions: dict[str, JunctionTopo] = field(default_factory=dict)
    edge_length: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, net_path: str | Path) -> "NetInfo":
        import sumolib

        net = sumolib.net.readNet(str(net_path))
        info = cls()
        for node in net.getNodes():
            if node.getType() != "traffic_light":
                continue
            incoming = node.getIncoming()
            if not incoming:
                continue
            jid = node.getID()
            topo = JunctionTopo(jid)
            for edge in incoming:
                eid = edge.getID()
                info.edge_to_junction[eid] = jid
                info.edge_length[eid] = edge.getLength()
                dx, dy = _edge_heading(edge)
                appr = _approach_of(dx, dy)
                topo.edge_approach[eid] = appr
                topo.approach_edges.setdefault(appr, []).append(eid)
                topo.incoming_lanes += edge.getLaneNumber()
            for conn in node.getConnections():
                in_edge = conn.getFrom().getID()
                out_edge = conn.getTo().getID()
                d = conn.getDirection()
                topo.conn_dir[(in_edge, out_edge)] = d
                appr = topo.edge_approach[in_edge]
                if d == "s":
                    topo.downstream.setdefault(appr, {})["through"] = out_edge
                elif d == "l":
                    topo.downstream.setdefault(appr, {})["left"] = out_edge
            info.junctions[jid] = topo
        return info


def _edge_heading(edge: Any) -> tuple[float, float]:
    """驶入边在**接近路口处**的朝向（用最后一段折线，对弯曲道路更准确）。"""
    shape = edge.getShape()
    if len(shape) >= 2:
        (x0, y0), (x1, y1) = shape[-2], shape[-1]
    else:  # 退化边（长度≈0）：用首尾点，兜底
        (x0, y0), (x1, y1) = shape[0], shape[-1]
    return x1 - x0, y1 - y0


def _approach_of(dx: float, dy: float) -> str:
    """按朝向角主分量把驶入边归到 N/E/S/W 象限（来向）。"""
    if abs(dx) >= abs(dy):
        return "W" if dx > 0 else "E"
    return "S" if dy > 0 else "N"


def canonical_movement(approach: str, direction: str) -> str:
    move = "left" if direction == "l" else "through"
    return f"{APPROACH_NAME[approach]}_{move}"


# ---------------------------------------------------------------------------
# 单路口控制器（动态相位）
# ---------------------------------------------------------------------------
@dataclass
class JunctionController:
    junction_id: str
    topo: JunctionTopo
    phases: dict[str, SignalPhase]
    phase_approach: dict[str, str]          # phase_id -> 来向
    approach_phase: dict[str, str]          # 来向 -> phase_id
    state_machine: PhaseStateMachine
    feedback: FeedbackMonitor
    queue_estimator: QueueEstimator
    spillback: SpillbackMonitor
    safety: SafetyGuard
    state: ControllerState
    link_approach: list[str]                # 与 TLS 状态串对齐
    clearance: dict[str, Any] | None = None
    # 统计
    switches: int = 0
    p0_preempts: int = 0
    p0_p1_conflicts: int = 0
    crossings: int = 0
    crossing_time_sum: float = 0.0
    wait_time_sum: float = 0.0
    wait_samples: int = 0

    def apply_tls(self, traci: Any, mode: str, phase_id: str | None) -> None:
        if mode == "all_red":
            s = "r" * len(self.link_approach)
        elif mode == "yellow":
            appr = self.phase_approach[phase_id]
            s = "".join("y" if a == appr else "r" for a in self.link_approach)
        else:
            appr = self.phase_approach[phase_id]
            s = "".join("G" if a == appr else "r" for a in self.link_approach)
        traci.trafficlight.setRedYellowGreenState(self.junction_id, s)


def build_junction_phases(topo: JunctionTopo) -> tuple[dict[str, SignalPhase], dict[str, str], dict[str, str]]:
    approaches = sorted(topo.approach_edges.keys())
    phases: dict[str, SignalPhase] = {}
    phase_approach: dict[str, str] = {}
    approach_phase: dict[str, str] = {}
    for appr in approaches:
        pid = f"P_{appr}"
        movements = frozenset(
            {f"{APPROACH_NAME[appr]}_through", f"{APPROACH_NAME[appr]}_left"}
        )
        phases[pid] = SignalPhase(
            phase_id=pid,
            movement_ids=movements,
            min_green_s=DEFAULT_MIN_GREEN,
            max_green_s=DEFAULT_MAX_GREEN,
            yellow_s=DEFAULT_YELLOW,
            all_red_s=DEFAULT_ALL_RED,
        )
        phase_approach[pid] = appr
        approach_phase[appr] = pid
    return phases, phase_approach, approach_phase


# ---------------------------------------------------------------------------
# 主仿真
# ---------------------------------------------------------------------------
@dataclass
class SimulationResult:
    rows: list[dict[str, Any]]
    per_junction: dict[str, dict[str, Any]]
    capacity: dict[str, dict[str, Any]]


class Simulation:
    def __init__(self, net_path: str | Path, config: dict[str, Any], *, controller: str = "adaptive", fixed_green_s: float = 30.0) -> None:
        self.netinfo = NetInfo.load(net_path)
        self.config = config
        self.controller = controller
        self.fixed_green_s = fixed_green_s
        self.controllers: dict[str, JunctionController] = {}

    # -- 装配 ---------------------------------------------------------------
    def _build_controllers(self, traci: Any) -> None:
        score_cfg = build_score_config(self.config)
        machine_cfg = build_machine_config(self.config)
        queue_cfg = self.config["queue_estimation"]

        tls_ids = [t for t in traci.trafficlight.getIDList() if t in self.netinfo.junctions]

        # 初始相位错开：按 junction id 哈希。
        for jid in sorted(tls_ids):
            topo = self.netinfo.junctions[jid]
            phases, phase_approach, approach_phase = build_junction_phases(topo)
            scorer = Scorer(score_cfg)
            spillback = SpillbackMonitor(score_cfg.o_high, score_cfg.o_low)
            feedback = FeedbackMonitor(self.config["runtime"]["confirm_timeout_s"])
            sm = PhaseStateMachine(phases, scorer, machine_cfg)
            qe = QueueEstimator(
                queue_speed_threshold_mps=queue_cfg["queue_speed_threshold_mps"],
                vehicle_length_m=queue_cfg["vehicle_length_m"],
                stopped_gap_m=queue_cfg["stopped_gap_m"],
                stale_after_s=queue_cfg["stale_after_s"],
            )
            guard = SafetyGuard(phases)

            links = traci.trafficlight.getControlledLinks(jid)
            link_approach: list[str] = []
            for link in links:
                in_lane, out_lane, _ = link[0]
                in_edge = in_lane.rsplit("_", 1)[0]
                appr = topo.edge_approach.get(in_edge)
                if appr is None:
                    # 兜底：按驶入边朝向（极少触发）
                    dx, dy = _edge_heading_from_traci(traci, in_edge)
                    appr = _approach_of(dx, dy)
                    topo.edge_approach[in_edge] = appr
                    topo.approach_edges.setdefault(appr, []).append(in_edge)
                    # 相位已按初始 approaches 构建，这里补进 phase 若缺失
                    if appr not in approach_phase:
                        pid = f"P_{appr}"
                        movements = frozenset({f"{APPROACH_NAME[appr]}_through", f"{APPROACH_NAME[appr]}_left"})
                        phases[pid] = SignalPhase(pid, movements, DEFAULT_MIN_GREEN, DEFAULT_MAX_GREEN, DEFAULT_YELLOW, DEFAULT_ALL_RED)
                        phase_approach[pid] = appr
                        approach_phase[appr] = pid
                        # 重新装配状态机
                        sm = PhaseStateMachine(phases, scorer, machine_cfg)
                        guard = SafetyGuard(phases)
                link_approach.append(appr)

            phase_ids = list(phases.keys())
            h = abs(hash(jid)) % len(phase_ids)
            initial = phase_ids[h]

            self.controllers[jid] = JunctionController(
                junction_id=jid,
                topo=topo,
                phases=phases,
                phase_approach=phase_approach,
                approach_phase=approach_phase,
                state_machine=sm,
                feedback=feedback,
                queue_estimator=qe,
                spillback=spillback,
                safety=guard,
                state=ControllerState(ControllerMode.NORMAL, initial, -20.0),
                link_approach=link_approach,
            )
            self.controllers[jid].apply_tls(traci, "green", initial)

    # -- 单步 ---------------------------------------------------------------
    def _step_adaptive(self, traci: Any, now: float, vehicles: dict[str, dict[str, Any]]) -> None:
        for jid, ctrl in self.controllers.items():
            obs = self._observations_for(ctrl, now, vehicles)
            lane_states = ctrl.queue_estimator.update(obs, now)

            # P1 下游占用
            for appr, edges in ctrl.topo.downstream.items():
                for move in ("through", "left"):
                    out_edge = edges.get(move)
                    if out_edge is None:
                        continue
                    occ = traci.edge.getLastStepOccupancy(out_edge)
                    lid = canonical_movement(appr, "l" if move == "left" else "s")
                    ctrl.spillback.update(lid, occ)

            demands: dict[str, PhaseDemand] = {}
            for pid, phase in ctrl.phases.items():
                counts = [lane_states[m].vehicle_count if m in lane_states else 0 for m in phase.movement_ids]
                waits = [lane_states[m].oldest_wait_s if m in lane_states else 0.0 for m in phase.movement_ids]
                blocked = any(ctrl.spillback.is_blocked(m) for m in phase.movement_ids)
                vc, ow = aggregate_demand(counts, waits)
                demands[pid] = PhaseDemand(vc, ow, blocked, False)

            if ctrl.state.mode == ControllerMode.NORMAL:
                decision = ctrl.state_machine.tick(ctrl.state, now, demands)
                if decision.selected_phase_id is not None and decision.selected_phase_id != ctrl.state.current_phase_id:
                    check = ctrl.safety.check_candidate(decision.selected_phase_id, ctrl.state, now)
                    if not check.allowed:
                        ctrl.apply_tls(traci, "green", ctrl.state.current_phase_id)
                        continue
                    old = ctrl.phases[ctrl.state.current_phase_id]
                    ctrl.clearance = {
                        "target": decision.selected_phase_id,
                        "stage": "yellow", "started": now,
                        "yellow_s": old.yellow_s, "all_red_s": old.all_red_s,
                    }
                    ctrl.state = ControllerState(ControllerMode.CLEARANCE, ctrl.state.current_phase_id, ctrl.state.phase_started_at)
                    ctrl.feedback.observe(decision, ctrl.state, now)
                    ctrl.switches += 1
                    if decision.reason == "p0_wait_preempt":
                        ctrl.p0_preempts += 1
                    ctrl.apply_tls(traci, "yellow", ctrl.state.current_phase_id)
                else:
                    ctrl.apply_tls(traci, "green", ctrl.state.current_phase_id)
            else:
                ctrl.feedback.observe(PhaseDecision(None, "clearance_in_progress"), ctrl.state, now)
                clr = ctrl.clearance
                if clr is None:
                    continue
                elapsed = now - clr["started"]
                old_phase = ctrl.state.current_phase_id
                if clr["stage"] == "yellow":
                    if elapsed >= clr["yellow_s"]:
                        clr["stage"] = "all_red"; clr["started"] = now
                        ctrl.state = ControllerState(ControllerMode.ALL_RED, old_phase, ctrl.state.phase_started_at)
                        ctrl.apply_tls(traci, "all_red", None)
                    else:
                        ctrl.apply_tls(traci, "yellow", old_phase)
                else:
                    if elapsed >= clr["all_red_s"]:
                        target = clr["target"]
                        ctrl.state = ControllerState(ControllerMode.NORMAL, target, now)
                        ctrl.clearance = None
                        ctrl.feedback.observe(PhaseDecision(None, "switch_complete"), ctrl.state, now)
                        ctrl.apply_tls(traci, "green", target)
                    else:
                        ctrl.apply_tls(traci, "all_red", None)

    def _step_fixed(self, traci: Any, now: float, vehicles: dict[str, dict[str, Any]]) -> None:
        """固定配时：按**每个路口自身的相位集合**轮换（3岔 3 相位 / 4岔 4 相位）。"""
        yellow, all_red = DEFAULT_YELLOW, DEFAULT_ALL_RED
        seg = self.fixed_green_s + yellow + all_red
        for ctrl in self.controllers.values():
            phase_ids = list(ctrl.phases.keys())
            n = len(phase_ids)
            if n == 0:
                continue
            t = now % (n * seg)
            idx = min(int(t // seg), n - 1)
            in_seg = t - idx * seg
            if in_seg < self.fixed_green_s:
                ctrl.apply_tls(traci, "green", phase_ids[idx])
            elif in_seg < self.fixed_green_s + yellow:
                ctrl.apply_tls(traci, "yellow", phase_ids[idx])
            else:
                ctrl.apply_tls(traci, "all_red", None)

    # -- 车辆观测 -----------------------------------------------------------
    def _observations_for(self, ctrl: JunctionController, now: float, vehicles: dict[str, dict[str, Any]]) -> list[VehicleObservation]:
        obs: list[VehicleObservation] = []
        for vid, v in vehicles.items():
            road = v["road"]
            if self.netinfo.edge_to_junction.get(road) != ctrl.junction_id:
                continue
            nxt = v["next"].get(road)
            if nxt is None:
                continue
            d = ctrl.topo.conn_dir.get((road, nxt))
            if d is None:
                continue
            appr = ctrl.topo.edge_approach[road]
            lane = canonical_movement(appr, d)
            obs.append(VehicleObservation(v["track_id"], lane, now, v["speed"]))
        return obs

    # -- 主循环 -------------------------------------------------------------
    def run(self, traci: Any, end_time: float, step_length: float = 1.0, *, log_every_s: float = 60.0, metrics_path: Path | None = None, screenshot_dir: Path | None = None, screenshot_every_s: float | None = None) -> SimulationResult:
        self._build_controllers(traci)
        route_chain: dict[str, dict[str, str]] = {}
        track_registry: dict[str, int] = {}
        next_track = 0
        max_wait_target = self.config["optimization"]["max_wait_target_s"]
        rows: list[dict[str, Any]] = []
        last_log = -1e9
        last_shot = -1e9
        if screenshot_dir is not None:
            screenshot_dir.mkdir(parents=True, exist_ok=True)

        # 通过时间跟踪
        entry_time: dict[str, dict[str, float]] = {}   # vid -> {junction -> entry}
        prev_junction: dict[str, str | None] = {}

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
                    route_chain[vid] = {route[i]: route[i + 1] for i in range(len(route) - 1)}
                vehicles[vid] = {"track_id": track_registry[vid], "road": road, "speed": speed, "next": route_chain[vid]}
                total_wait += wt
                speed_sum += speed
                if speed < 0.1:
                    halting += 1
                if wt > max_wait_target:
                    p0_violations += 1
                max_wait = max(max_wait, wt)

                # 通过/等待 归属到路口（仅在停止线检测区内计为「到达路口」）
                jid = self.netinfo.edge_to_junction.get(road)
                pj = prev_junction.get(vid)
                if pj is not None and pj != jid:
                    # 已驶离路口 pj：记录通过时间（进入检测区 -> 离开进口道）
                    ctrl = self.controllers.get(pj)
                    if ctrl is not None and pj in entry_time.get(vid, {}):
                        t0 = entry_time[vid].pop(pj)
                        ctrl.crossings += 1
                        ctrl.crossing_time_sum += now - t0
                if jid is not None:
                    ctrl = self.controllers.get(jid)
                    if ctrl is not None:
                        pos = traci.vehicle.getLanePosition(vid)
                        edge_len = self.netinfo.edge_length.get(road, 0.0)
                        dist_to_stop = edge_len - pos
                        if dist_to_stop <= DETECT_DIST_M:
                            entry_time.setdefault(vid, {}).setdefault(jid, now)
                            ctrl.wait_time_sum += wt
                            ctrl.wait_samples += 1
                prev_junction[vid] = jid

            if self.controller == "adaptive":
                self._step_adaptive(traci, now, vehicles)
            else:
                self._step_fixed(traci, now, vehicles)

            n = len(veh_ids)
            rows.append({
                "time": now, "n_active": n, "n_halting": halting,
                "total_waiting_time": round(total_wait, 2),
                "mean_speed": round(speed_sum / n, 3) if n else 0.0,
                "p0_violations": p0_violations,
                "max_waiting_time": round(max_wait, 2),
                "arrived": arrived_total,
            })

            if now - last_log >= log_every_s:
                last_log = now
                print(f"[t={now:6.0f}s] active={n:5d} halting={halting:5d} mean_wait={total_wait/max(1,n):6.2f}s speed={speed_sum/max(1,n):5.2f} P0>{max_wait_target:g}={p0_violations}", flush=True)

            if screenshot_dir is not None and screenshot_every_s is not None:
                if now - last_shot >= screenshot_every_s - 1e-9:
                    traci.gui.screenshot("View #0", str(screenshot_dir / f"frame_{int(round(now)):05d}.png"))
                    last_shot = now

        if screenshot_dir is not None:
            traci.simulationStep()

        if metrics_path is not None:
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            with metrics_path.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

        per_junction = {}
        for jid, c in self.controllers.items():
            per_junction[jid] = {
                "switches": c.switches,
                "p0_preempts": c.p0_preempts,
                "p0_p1_conflicts": c.p0_p1_conflicts,
                "crossings": c.crossings,
                "avg_crossing_time_s": round(c.crossing_time_sum / c.crossings, 2) if c.crossings else None,
                "avg_waiting_time_s": round(c.wait_time_sum / c.wait_samples, 2) if c.wait_samples else None,
            }
        capacity = {
            jid: {
                "approaches": sorted(t.approach_edges.keys()),
                "incoming_lanes": t.incoming_lanes,
                "max_flow_vph": round(t.incoming_lanes * SATURATION_FLOW_VPH, 0),
            }
            for jid, t in self.netinfo.junctions.items()
        }
        return SimulationResult(rows, per_junction, capacity)


def _edge_heading_from_traci(traci: Any, edge_id: str) -> tuple[float, float]:
    shape = traci.edge.getShape(edge_id)
    (x0, y0), (x1, y1) = shape[0], shape[-1]
    return x1 - x0, y1 - y0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run_simulation(sumocfg, config_path, *, controller="adaptive", fixed_green_s=30.0, end_time=3600.0, step_length=1.0, metrics_path=None, gui=False, delay_ms=100.0, screenshot_dir=None, screenshot_every_s=None, view_zoom=None, view_offset=None, close=True):
    import xml.etree.ElementTree as ET

    tree = ET.parse(str(sumocfg))
    net_file = tree.getroot().find(".//net-file").get("value")
    net_path = str((Path(sumocfg).parent / net_file).resolve())
    config = load_config(config_path)

    t0 = time.time()
    if gui:
        import traci
        traci.start(["sumo-gui", "-c", str(sumocfg), "--start", "--delay", str(delay_ms), "--window-size", "1280,800", "--no-warnings", "true"])
    else:
        import libsumo as traci
        traci.start(["sumo", "-c", str(sumocfg), "--step-length", str(step_length), "--start", "--quit-on-end", "--no-warnings", "true"])
    sim = Simulation(net_path, config, controller=controller, fixed_green_s=fixed_green_s)
    try:
        if view_offset is not None:
            try:
                traci.gui.setOffset("View #0", *view_offset)
            except Exception:
                pass
        if view_zoom is not None:
            try:
                traci.gui.setZoom("View #0", view_zoom)
            except Exception:
                pass
        result = sim.run(traci, end_time, step_length, metrics_path=metrics_path, screenshot_dir=screenshot_dir, screenshot_every_s=screenshot_every_s)
    finally:
        if close:
            try:
                traci.close()
            except Exception:
                pass
    result.elapsed_wall_s = time.time() - t0
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="通用路网 + 决策层 信号灯仿真")
    ap.add_argument("--sumocfg", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--controller", choices=["adaptive", "fixed"], default="adaptive")
    ap.add_argument("--fixed-green-s", type=float, default=30.0)
    ap.add_argument("--end-time", type=float, default=3600.0)
    ap.add_argument("--step-length", type=float, default=1.0)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--report-json", default=None)
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--delay-ms", type=float, default=100.0)
    args = ap.parse_args()

    result = run_simulation(
        args.sumocfg, args.config, controller=args.controller, fixed_green_s=args.fixed_green_s,
        end_time=args.end_time, step_length=args.step_length,
        metrics_path=Path(args.metrics) if args.metrics else None, gui=args.gui, delay_ms=args.delay_ms,
    )

    # 汇总
    n = len(result.rows)
    mean_active = sum(r["n_active"] for r in result.rows) / n if n else 0
    summary = {
        "duration_s": result.rows[-1]["time"] if result.rows else 0,
        "arrived": result.rows[-1]["arrived"] if result.rows else 0,
        "mean_active": round(mean_active, 1),
        "mean_waiting_time_s": round(sum(r["total_waiting_time"] for r in result.rows) / max(1, sum(r["n_active"] for r in result.rows)), 2),
        "max_waiting_time_s": max(r["max_waiting_time"] for r in result.rows),
        "wall_s": round(result.elapsed_wall_s, 1),
        "n_junctions": len(result.per_junction),
    }
    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_json).write_text(json.dumps({"summary": summary, "per_junction": result.per_junction, "capacity": result.capacity}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
