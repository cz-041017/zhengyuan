"""生成磐安县 24 小时拟真需求（按小时流量曲线）。

依据（网络检索）：
- 磐安县环城北路（"三纵四横"主干道）日均车流量约 8500 辆次
  （磐安县住建局 2025-05-22 人大建议答复）。
- 磐安县为畅通型小县城，城市道路限速 ≤60 km/h（城区约 40~50 km/h）。
- 无更多公开的分向/分时数据时，按"最大压力"原则设计：高峰小时取日均的
  约 10%（约 850 veh/h），并叠加早/晚两个高峰。

小时系数曲线（0~23 点，峰值=0.70 对应约 850 veh/h），全天合计约 9100 辆。
车型：90% 小客车 / 5% 货车 / 5% 公交（磐安为小县城，以客车为主）。

用法：
    PYTHONPATH=/usr/share/sumo/tools .venv/bin/python \
        panan/scripts/gen_realistic_demand.py --out panan/demand/realistic_trips.xml
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import sumolib

# 0~23 点的小时流量系数（峰值 0.70 对应高峰小时 ~850 veh/h）
HOUR_COEF = [
    0.03, 0.02, 0.02, 0.02, 0.03, 0.06,  # 0-5 夜间
    0.15, 0.45, 0.65, 0.45, 0.40, 0.42,  # 6-11 早高峰及其回落
    0.40, 0.42, 0.45, 0.48, 0.55, 0.70,  # 12-17 平峰→晚高峰
    0.65, 0.40, 0.30, 0.22, 0.15, 0.08,  # 18-23 晚高峰回落→夜间
]

# 峰值小时流量（veh/h）。环城北路日均 8500 → 高峰约 850。
PEAK_FLOW_VEH_H = 850.0
PEAK_COEF = max(HOUR_COEF)  # 0.70

# 车型占比：小客车/货车/公交
CAR_FRAC = 0.90
TRUCK_FRAC = 0.05
BUS_FRAC = 0.05


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="panan/network/panan.net.xml")
    ap.add_argument("--out", default="panan/demand/realistic_trips.xml")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    net = sumolib.net.readNet(args.net)
    rng = random.Random(args.seed)

    # 可作起终点的普通边（function=""，排除内部边）
    edges = [
        e.getID()
        for e in net.getEdges()
        if e.getFunction() == "" and not e.getID().startswith(":")
    ]
    if len(edges) < 2:
        raise SystemExit("路网普通边太少，无法生成需求")

    # 每小时发车数 = PEAK_FLOW_VEH_H / PEAK_COEF * coef
    per_hour = [
        int(round(PEAK_FLOW_VEH_H / PEAK_COEF * c)) for c in HOUR_COEF
    ]

    # 先收集所有 trip，按 depart 排序（SUMO 要求 depart 单调递增，否则乱序被丢弃）
    trips: list[tuple[float, str, str, str]] = []
    for hour, n in enumerate(per_hour):
        for _ in range(n):
            dep = hour * 3600.0 + rng.uniform(0, 3600)
            fr = rng.choice(edges)
            to = rng.choice(edges)
            if to == fr:
                to = rng.choice(edges)
            roll = rng.random()
            if roll < CAR_FRAC:
                vt = "car"
            elif roll < CAR_FRAC + TRUCK_FRAC:
                vt = "truck"
            else:
                vt = "bus"
            trips.append((dep, fr, to, vt))
    trips.sort(key=lambda t: t[0])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        for idx, (dep, fr, to, vt) in enumerate(trips):
            f.write(
                f'    <trip id="r{idx}" depart="{dep:.2f}" '
                f'from="{fr}" to="{to}" type="{vt}"/>\n'
            )
        f.write("</routes>\n")

    total = sum(per_hour)
    print(f"已生成 {total} 个 trip（24h，峰值 {max(per_hour)} veh/h）-> {out}")
    print("小时分布:", per_hour)


if __name__ == "__main__":
    main()
