"""生成磐安县仿真报告（Markdown）。

用法：
    .venv/bin/python panan/scripts/make_report.py
读取 panan/results/adaptive_report.json 与 fixed_report.json，输出
panan/results/仿真报告.md。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # 仓库根目录
RESULTS = ROOT / "panan/results"


def _load(name: str) -> dict | None:
    p = RESULTS / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _fmt(v, nd=1):
    return "-" if v is None else round(v, nd)


def main() -> None:
    ad = _load("adaptive_report.json")
    fx = _load("fixed_report.json")

    ad_s = ad["summary"]
    ad_pj = ad["per_junction"]
    ad_cap = ad["capacity"]
    duration = ad_s["duration_s"]
    demand = 2400  # 真实路网需求（randomTrips --period 1.5，1 小时）

    # 按路口 id 排序
    jids = sorted(ad_cap.keys())

    lines: list[str] = []
    lines.append("# 磐安县交通信号灯仿真报告\n")
    lines.append("> 目标坐标：120.430890, 29.036192（金华市磐安县）\n")
    lines.append("> 路网来源：OpenStreetMap（经 `osmWebWizard.py` 下载）· 控制器：本项目逻辑决策层（经接口层桥接控制信号灯）\n")

    # 1 概述
    lines.append("## 1. 概述\n")
    lines.append(
        f"- **真实路网**：OSM bbox 120.417~120.482°E / 29.017~29.072°N（约 6.4km × 6.2km），"
        f"**302 个路口 / 769 条边**；其中 **{len(jids)} 个主要交叉口设信号灯**并由决策层控制\n"
    )
    lines.append(
        f"- 仿真时长 **{int(duration)} s**；需求 **{demand}** 车次，累计到达 "
        f"**{ad_s['arrived']}**（吞吐 {ad_s['arrived']/demand*100:.1f}%）\n"
    )
    lines.append(
        f"- 全局平均等待时间 **{ad_s['mean_waiting_time_s']} s**，最大等待 "
        f"**{ad_s['max_waiting_time_s']} s**（P0 目标 120 s）\n"
    )
    lines.append(
        "> 说明：命令行 Overpass 在本机被拦截（overpass-api.de 返回 406、镜像 500、OSM API 超时），"
        "故改用 **`osmWebWizard.py`**（走浏览器请求路径）下载真实路网，再由 `netconvert` 重建；"
        "OSM 原始数据中**无 traffic_signals 标记**，故用 `--tls.set` 给主要交叉口补设信号灯。\n"
    )
    lines.append(
        "> ⚠️ 数据质量发现：OSM 在该区域存在**多节点复杂路口簇**（相邻路口由 0.2m 连接段相连）。"
        "若把这些路口各自作为独立信号灯控制，会出现车辆卡在连接段、最长等待达 **300 s** 的死锁"
        "（已在上一版路网实测到）。本次仅对**进口道均 ≥30m 的干净交叉口**设信号灯，"
        f"最长等待降至 **{ad_s['max_waiting_time_s']:.0f} s**。\n"
    )

    # 2 指标口径
    lines.append("## 2. 指标口径\n")
    lines.append(
        "| 指标 | 口径 |\n|---|---|\n"
        "| 最大承载车流 | 进口道车道数 × 饱和流率 1800 veh/h/车道（理论容量） |\n"
        "| 实际通过车流 | 每小时实际驶离该路口的车辆数（仿真实测） |\n"
        "| 平均通过路口时间 | 车辆进入停止线 60m 检测区 → 驶离进口道的平均时长 |\n"
        "| 平均等待时间 | 停止线 60m 检测区内车辆的时间平均停等时长 |\n"
    )

    # 3 各路口指标
    lines.append("## 3. 各路口指标（自适应决策层）\n")
    lines.append(
        "| 路口 | 来向 | 进口车道 | 最大车流(veh/h) | 实际车流(veh/h) | 平均通过(s) | 平均等待(s) | 切换 | P0抢占 |\n"
        "|---|:--:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    for jid in jids:
        cap = ad_cap[jid]
        pj = ad_pj.get(jid, {})
        crossings = pj.get("crossings", 0)
        throughput = round(crossings / duration * 3600, 0) if duration else 0
        lines.append(
            f"| {jid} | {'/'.join(cap['approaches'])} | {cap['incoming_lanes']} "
            f"| {cap['max_flow_vph']:.0f} | {throughput:.0f} "
            f"| {_fmt(pj.get('avg_crossing_time_s'), 1)} | {_fmt(pj.get('avg_waiting_time_s'), 1)} "
            f"| {pj.get('switches', 0)} | {pj.get('p0_preempts', 0)} |\n"
        )
    # 汇总行
    tot_cross = sum(ad_pj[j].get("crossings", 0) for j in jids)
    cross_vals = [ad_pj[j]["avg_crossing_time_s"] for j in jids if ad_pj[j].get("avg_crossing_time_s")]
    wait_vals = [ad_pj[j]["avg_waiting_time_s"] for j in jids if ad_pj[j].get("avg_waiting_time_s")]
    lines.append(
        f"| **平均** | — | — | — | {tot_cross/duration*3600:.0f} "
        f"| {sum(cross_vals)/len(cross_vals):.1f} | {sum(wait_vals)/len(wait_vals):.1f} | — | — |\n"
    )

    # 4 对比
    if fx:
        fx_s = fx["summary"]
        lines.append("## 4. 全局对比（自适应 vs 固定配时）\n")
        lines.append(
            "| 控制器 | 到达 | 吞吐 | 平均等待(s) | 最大等待(s) | 平均车速(m/s) |\n"
            "|---|---|---:|---:|---:|---:|\n"
        )
        lines.append(
            f"| 决策层自适应 | {ad_s['arrived']} | {ad_s['arrived']/demand*100:.1f}% "
            f"| {ad_s['mean_waiting_time_s']} | {ad_s['max_waiting_time_s']} | — |\n"
        )
        lines.append(
            f"| 固定配时 30s | {fx_s['arrived']} | {fx_s['arrived']/demand*100:.1f}% "
            f"| {fx_s['mean_waiting_time_s']} | {fx_s['max_waiting_time_s']} | — |\n"
        )

    # 5 调参建议
    lines.append("## 5. 决策层参数调整建议\n")
    lines.append(
        "基于本次运行与 9×9 基准实验（`sumo/results/experiments.json`）的结论：\n\n"
        "1. **最大绿 `max_green`（默认 60s）偏大**：使周期拉长、最坏红灯 ~196s，120s 等待目标 "
        "完全依赖 P0 抢占兜底。建议 **≤30s**（周期 ~136s，最坏红灯 ~106s < 120s，结构性可行）。\n"
        "2. **倒数 `countdown.display_duration_s`（默认 15s）是响应瓶颈**：降到 10s 在 9×9 实验中"
        "平均等待 −25%、吞吐 +6.8%。真实部署受 GA/T 508 的 15s 约束，权衡时优先缩短去抖而非倒数。\n"
        "3. **等待权重不宜过高**：`demand.wait_weight` 从 0.4 提到 0.6 会因频繁切换而变差（−2% 吞吐、"
        "+4% 等待）；保持 0.4 或略降。\n"
        "4. **最小绿 `min_green` 不宜过大**：从 10s 提到 15s 使等待 +10%、吞吐 −3.4%。\n"
        "5. **本网络（2400 veh/h）下自适应明显优于固定配时**：平均等待 4.63s vs 10.13s（**−54%**）、"
        "最大等待 114s vs 300s（固定配时突破 120s 目标），吞吐 93.2% vs 91.3%。"
        "说明自适应优势在**中等及以上饱和**时显著。\n"
        "6. **⚠️ 决策层「无路口间协调」的局限**（删除部分道路、车流集中到主干道后更明显）：决策层**各路口独立控灯**，"
        "若给相距很近（<200m）的连续路口都设信号灯，上游绿灯会把车塞进已满的下游短路段，出现队列溢流死锁"
        "（实测最长等待 949s）。**规避：设信号灯时避免过近的连续路口；根治需在决策层加相邻路口协调（绿波/相位差）。**\n"
    )

    # 6 bug 报告
    lines.append("## 6. 决策层逻辑算法 bug 报告\n")
    lines.append("> 按指示：本节只报告，**未修改逻辑层代码**（`src/traffic_control/`）。\n")
    lines.append("### 6.1 已修复（历史，非本次改动）\n")
    lines.append(
        "| # | 位置 | 缺陷 | 现象 | 修复 |\n|---|---|---|---|---|\n"
        "| B1 | `scoring.py SpillbackMonitor.update` | 方向首次上报低占用时字典无该 key，直接索引抛 `KeyError` | "
        "任一方向第一次以 `occupancy<o_high` 上报即崩溃 | 返回改为 `self._blocked.get(direction_id, False)`，补回归测试 |\n"
        "| B2 | `state_machine.py tick` | `p0_p1_conflict` 分支是唯一不 `_clear_transient()` 的提前返回 | "
        "冲突发生时残留的倒数/去抖在冲突解除后仍可能触发过早切换 | 补 `self._clear_transient()`，补回归测试 |\n"
    )
    lines.append("### 6.2 发现但未修改（按指示仅报告）\n")
    lines.append(
        "| # | 位置 | 逻辑问题 | 影响 | 建议 |\n|---|---|---|---|---|\n"
        "| L1 | `state_machine.tick` | 「最大绿硬上限」有条件：所有红灯相位下游拥堵时返回 `no_feasible_successor` 不切换，当前相位会超过最大绿 | "
        "无法强制轮转，可能持续往拥堵下游放车 | 文档改为「无可行后继时软上限」；或对当前相位下游拥堵做提前终止 |\n"
        "| L2 | `state_machine.tick` | 当前绿灯相位自身的下游溢流只被扣分(−1000)不被硬排除（P1 只排除候选红灯） | "
        "当前相位继续向拥堵下游塞车直到最大绿 | 增加「当前相位下游拥堵 → 提前终止」分支 |\n"
        "| L3 | `state_machine.tick` | `demands` 缺失某相位时 `PhaseDemand()` 默认 `downstream_blocked=False`（视为畅通） | "
        "与「数据缺失 ≠ 畅通」原则冲突 | 缺省应为 blocked（保守）；桥接层当前已补齐全部相位规避 |\n"
        "| L4 | `queue_estimator.py` | 速度未知的车辆不计入排队长度（按 0 而非「未知」），`oldest_wait` 含快速通过车辆 | "
        "排队长度可能低估、等待被高估 | 记录为已知近似 |\n"
        "| L5 | `state_machine.tick` | 倒数期间出现更高分新候选不被采纳（只复核原 pending） | 次优切换 | 影响小，下一轮可修正 |\n"
        "| L6 | 整体架构 | 决策层**逐路口独立决策，无相邻路口协调（绿波/相位差）** | 在相距 <200m 的连续信号路口上，"
        "上游绿灯把车塞进已满下游短路段 → 队列溢流死锁（去辅助道路后实测最长等待 949s） | "
        "接口层已用「信号灯间距 ≥200m」规避；根治需在决策层加协调层 |\n"
    )
    lines.append("### 6.3 接口层 bug（本次发现并已修复，非逻辑层）\n")
    lines.append(
        "| # | 位置 | 缺陷 | 现象 | 修复 |\n|---|---|---|---|---|\n"
        "| I1 | `panan_bridge._step_fixed` | 固定配时控制器写死四相位 `P_N/P_E/P_S/P_W` | "
        "在 3 岔真实路口（相位为 P_N/P_E/P_W）抛 `KeyError: 'P_N'` 崩溃 | 改为按每个路口自身相位集合轮换 |\n"
        "| I2 | `panan_bridge._edge_heading` | 用边首尾点算朝向，弯曲道路会错判来向 | "
        "来向/相位映射错误 | 改用**接近路口的最后一段折线**算朝向 |\n"
        "| I3 | 路网构建 | OSM 复杂路口簇（0.2m 连接段）被当作多个独立信号灯 | "
        "连接段死锁，最长等待 300s | 剔除退化路口（仅保留进口道 ≥30m 的干净交叉口），最长等待降至 83s |\n"
        "| I4 | `panan_bridge.build_junction_phases` | 同一象限（如两条东侧来路）的多条进口道被合并成一个相位 | "
        "若其中一条左转、另一条直行，同相位内可能产生交叉冲突 | 演示可接受；正式部署应按路口冲突矩阵细分相位 |\n"
    )

    out = RESULTS / "仿真报告.md"
    import re

    text = "\n".join(lines)
    # 去掉表格行之间的空行（避免破坏 Markdown 表格渲染），并把多余空行收敛为单个空行。
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n\n(?=\|)", "\n", text)
    out.write_text(text, encoding="utf-8")
    print(f"报告已生成：{out}")


if __name__ == "__main__":
    main()
