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
from datetime import datetime
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
from traffic_control.phasing import ConflictMatrix, phase_groups
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
    lane_dir: dict[tuple[str, str], str] = field(default_factory=dict)  # (in_lane,out_lane)->direction
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
            edges_with_angle: list[tuple[str, float]] = []
            for edge in incoming:
                eid = edge.getID()
                info.edge_to_junction[eid] = jid
                info.edge_length[eid] = edge.getLength()
                dx, dy = _edge_heading(edge)
                edges_with_angle.append((eid, math.degrees(math.atan2(dy, dx))))
                topo.incoming_lanes += edge.getLaneNumber()
            # 统一分配「互不相同」的来向字母，绝不把两条来路合并进同一相位
            for eid, appr in assign_approaches(edges_with_angle).items():
                topo.edge_approach[eid] = appr
                topo.approach_edges.setdefault(appr, []).append(eid)
            for conn in node.getConnections():
                in_edge = conn.getFrom().getID()
                out_edge = conn.getTo().getID()
                in_lane = conn.getFromLane().getID()
                out_lane = conn.getToLane().getID()
                d = conn.getDirection()
                topo.conn_dir[(in_edge, out_edge)] = d
                topo.lane_dir[(in_lane, out_lane)] = d
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


# 方向锚点：车辆朝向角(deg) -> 来向字母（朝西=东来E，朝南=北来N，朝东=西来W，朝北=南来S）
_APPROACH_ANCHORS = [("E", -180.0), ("N", -90.0), ("W", 0.0), ("S", 90.0)]


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def assign_approaches(edges: list[tuple[str, float]]) -> dict[str, str]:
    """给一个路口的所有驶入边分配**互不相同**的来向字母（N/E/S/W），绝不合并。

    ``edges`` 为 [(edge_id, heading_deg)]，heading_deg = atan2(dy,dx) ∈ (-180,180]。
    按朝向角排序后，贪心把每条边分配给「最近且尚未被占用」的方向锚点，从而保证
    4 边 → 4 相位、3 边 → 3 相位，避免不同来向被合并进同一相位导致冲突车流同时放行
    （这是引发交通事故的严重安全问题）。
    """
    if not edges:
        return {}
    used: set[str] = set()
    result: dict[str, str] = {}
    for eid, ang in sorted(edges, key=lambda x: x[1]):
        best: str | None = None
        best_d = 1e18
        for letter, anchor in _APPROACH_ANCHORS:
            if letter in used:
                continue
            d = _angle_diff(ang, anchor)
            if d < best_d:
                best_d = d
                best = letter
        if best is None:  # 超过 4 条驶入边（罕见）：退回象限分类兜底
            best = _approach_of(
                math.cos(math.radians(ang)), math.sin(math.radians(ang))
            )
        used.add(best)
        result[eid] = best
    return result


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
    link_right: list[bool] = field(default_factory=list)  # 与 link_approach 对齐，标记右转 link
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
            base = ["r"] * len(self.link_approach)
        elif mode == "yellow":
            appr = self.phase_approach[phase_id]
            base = ["y" if a == appr else "r" for a in self.link_approach]
        else:
            appr = self.phase_approach[phase_id]
            base = ["G" if a == appr else "r" for a in self.link_approach]
        # 右转 link 始终 'g'（让行绿灯），不受红灯控制
        s = "".join("g" if right else b for b, right in zip(base, self.link_right))
        traci.trafficlight.setRedYellowGreenState(self.junction_id, s)


def build_junction_phases(
    topo: JunctionTopo,
    groups: tuple[Any, ...],
) -> tuple[dict[str, SignalPhase], dict[str, str], dict[str, str]]:
    """按 config 的相位分组（phase_groups）为每个路口构建相位。

    ``groups`` 来自 ``traffic_control.phasing.phase_groups(mode)``。模式A(minor_only)
    给出 P_N/P_E/P_S/P_W，movement_ids 为 {north_through,north_left} 等（直行+左转合并）。
    三岔路口：只保留该路口实际存在的来向对应相位，自动降为 3 相位（去掉缺失方向）。
    """
    # phase_id -> 来向字母（"P_N" -> "N"）
    phase_approach = {g.phase_id: g.phase_id.split("_")[-1] for g in groups}
    approach_phase = {a: p for p, a in phase_approach.items()}
    appr_movements = {phase_approach[g.phase_id]: g.movement_ids for g in groups}

    phases: dict[str, SignalPhase] = {}
    for appr in sorted(topo.approach_edges.keys()):
        pid = approach_phase.get(appr)
        if pid is None:
            continue  # 该来向不在 config 相位分组内（一般不会发生）
        phases[pid] = SignalPhase(
            phase_id=pid,
            movement_ids=appr_movements[appr],
            min_green_s=DEFAULT_MIN_GREEN,
            max_green_s=DEFAULT_MAX_GREEN,
            yellow_s=DEFAULT_YELLOW,
            all_red_s=DEFAULT_ALL_RED,
        )
    return phases, phase_approach, approach_phase


# ---------------------------------------------------------------------------
# 主仿真
# ---------------------------------------------------------------------------
@dataclass
class SimulationResult:
    rows: list[dict[str, Any]]
    per_junction: dict[str, dict[str, Any]]
    capacity: dict[str, dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)


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

        # config 的相位分组（模式A minor_only → P_N/P_E/P_S/P_W；三岔自动降为3相位）
        layout = self.config["signal"]["layout"]
        groups = phase_groups(layout["mode"], layout.get("main_road_direction"))
        # config 的冲突矩阵（存档用；本桥接为独占绿灯，冲突语义已隐含在相位设计中）
        self.conflict_matrix = ConflictMatrix(
            {k: frozenset(v) for k, v in self.config["signal"]["conflict_matrix"].items()}
        )
        appr_movements = {g.phase_id.split("_")[-1]: g.movement_ids for g in groups}

        tls_ids = [t for t in traci.trafficlight.getIDList() if t in self.netinfo.junctions]

        # 初始相位错开：按 junction id 哈希。
        for jid in sorted(tls_ids):
            topo = self.netinfo.junctions[jid]
            phases, phase_approach, approach_phase = build_junction_phases(topo, groups)
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
            link_right: list[bool] = []
            for link in links:
                if not link or len(link[0]) < 3:
                    # 退化 link（空元组/内联占位）：标记 'X'，永远显示红灯
                    link_approach.append("X")
                    link_right.append(False)
                    continue
                in_lane, out_lane, _ = link[0]
                d = topo.lane_dir.get((in_lane, out_lane), "")
                link_right.append(d in ("r", "R"))
                in_edge = in_lane.rsplit("_", 1)[0]
                appr = topo.edge_approach.get(in_edge)
                if appr is None:
                    # 兜底：sumolib 未分类的驶入边，按朝向分类并补入相位
                    dx, dy = _edge_heading_from_traci(traci, in_edge)
                    appr = _approach_of(dx, dy)
                    topo.edge_approach[in_edge] = appr
                    topo.approach_edges.setdefault(appr, []).append(in_edge)
                    if appr in approach_phase and approach_phase[appr] not in phases:
                        pid = approach_phase[appr]
                        phases[pid] = SignalPhase(
                            phase_id=pid,
                            movement_ids=appr_movements.get(appr, frozenset()),
                            min_green_s=DEFAULT_MIN_GREEN,
                            max_green_s=DEFAULT_MAX_GREEN,
                            yellow_s=DEFAULT_YELLOW,
                            all_red_s=DEFAULT_ALL_RED,
                        )
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
                link_right=link_right,
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

        # 指标累计：平均车均延误 / 平均排队长度 / 通行能力 / 冲突点数量
        last_time_loss: dict[str, float] = {}   # vid -> 最近一次 timeLoss（到达时取最终延误）
        total_delay = 0.0
        delay_count = 0
        queue_sum = 0.0
        conflict_sum = 0

        total_steps = int(round(end_time / step_length))
        arrived_total = 0
        for _ in range(total_steps):
            try:
                traci.simulationStep()
            except Exception:
                # 用户手动关闭了 sumo-gui 窗口 / 主动中断
                print("仿真已手动停止", flush=True)
                break
            now = traci.simulation.getTime()
            arrived_total += traci.simulation.getArrivedNumber()

            veh_ids = traci.vehicle.getIDList()
            current_ids = set(veh_ids)
            # 检测本步「消失」的车辆（到达终点），累计其最终延误 timeLoss
            for vid in list(last_time_loss.keys()):
                if vid not in current_ids:
                    total_delay += last_time_loss[vid]
                    delay_count += 1
                    del last_time_loss[vid]

            vehicles: dict[str, dict[str, Any]] = {}
            total_wait = 0.0
            halting = 0
            queue_veh = 0
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
                last_time_loss[vid] = traci.vehicle.getTimeLoss(vid)
                if vid not in route_chain:
                    route = list(traci.vehicle.getRoute(vid))
                    route_chain[vid] = {route[i]: route[i + 1] for i in range(len(route) - 1)}
                vehicles[vid] = {"track_id": track_registry[vid], "road": road, "speed": speed, "next": route_chain[vid]}
                total_wait += wt
                speed_sum += speed
                if speed < 0.1:
                    halting += 1
                    if road in self.netinfo.edge_to_junction:
                        queue_veh += 1
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

            # 累计冲突/碰撞次数与排队长度
            conflict_sum += traci.simulation.getCollidingVehiclesNumber()
            queue_sum += queue_veh

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
            try:
                traci.simulationStep()
            except Exception:
                pass

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

        # 全网汇总指标（供 CSV 追加）
        duration = rows[-1]["time"] if rows else end_time
        summary = {
            "arrived": arrived_total,
            "duration_s": duration,
            "avg_delay_s": round(total_delay / delay_count, 3) if delay_count else 0.0,
            "avg_queue_veh": round(queue_sum / total_steps, 3),
            "capacity_veh_per_h": round(arrived_total / duration * 3600.0, 2),
            "conflicts": int(conflict_sum),
        }
        return SimulationResult(rows, per_junction, capacity, summary)


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


METRICS_CSV_HEADER = [
    "timestamp", "controller", "config", "net", "seed",
    "duration_s", "arrived",
    "avg_delay_s", "avg_queue_veh", "capacity_veh_per_h", "conflicts",
]


def append_metrics_csv(csv_path: str | Path, row: dict[str, Any]) -> None:
    """把一次仿真运行的汇总指标追加到 CSV（首次写入表头，之后每次追加一行）。"""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.is_file() and csv_path.stat().st_size > 0
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=METRICS_CSV_HEADER)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in METRICS_CSV_HEADER})


def _net_basename_from_cfg(sumocfg: str | Path) -> str:
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(str(sumocfg))
        return Path(tree.getroot().find(".//net-file").get("value")).name
    except Exception:
        return ""


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
    ap.add_argument("--csv", default=None, help="把本次运行的 4 项指标追加到该 CSV（每次一行）")
    ap.add_argument("--seed", type=int, default=42, help="需求种子（仅记录到 CSV 元信息）")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--delay-ms", type=float, default=100.0)
    args = ap.parse_args()

    result = run_simulation(
        args.sumocfg, args.config, controller=args.controller, fixed_green_s=args.fixed_green_s,
        end_time=args.end_time, step_length=args.step_length,
        metrics_path=Path(args.metrics) if args.metrics else None, gui=args.gui, delay_ms=args.delay_ms,
    )

    s = result.summary
    summary = {
        "duration_s": s["duration_s"],
        "arrived": s["arrived"],
        "avg_delay_s": s["avg_delay_s"],
        "avg_queue_veh": s["avg_queue_veh"],
        "capacity_veh_per_h": s["capacity_veh_per_h"],
        "conflicts": s["conflicts"],
        "wall_s": round(result.elapsed_wall_s, 1),
        "n_junctions": len(result.per_junction),
    }
    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_json).write_text(json.dumps({"summary": summary, "per_junction": result.per_junction, "capacity": result.capacity}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 追加到指标 CSV（每次运行一行，便于后续可视化）
    if args.csv:
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "controller": args.controller,
            "config": Path(args.config).name,
            "net": _net_basename_from_cfg(args.sumocfg),
            "seed": args.seed,
            "duration_s": s["duration_s"],
            "arrived": s["arrived"],
            "avg_delay_s": s["avg_delay_s"],
            "avg_queue_veh": s["avg_queue_veh"],
            "capacity_veh_per_h": s["capacity_veh_per_h"],
            "conflicts": s["conflicts"],
        }
        append_metrics_csv(args.csv, row)
        print(f"已追加到 CSV: {args.csv}", flush=True)


if __name__ == "__main__":
    main()
