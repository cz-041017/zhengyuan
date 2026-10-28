"""生成「从奉庆线来」的额外车流，注入到目标路口 cluster_4178171527_...。

奉庆线驶入目标路口的边是 787068627#2，其上游是 787068627#1。
本脚本生成两类 trip（起点都是奉庆线上游 787068627#1）：

1. ``--count``    常规奉庆线流量：终点=路网随机边（车辆沿奉庆线驶入路口）。
2. ``--to-e10``   **奉庆线 → E10 方向**专项流量：终点=-E10.29（月山路），
   车辆穿过路口后往 E10/月山路方向离开，形成明显的「奉庆线往 E10」车流。

用法：
    PYTHONPATH=/usr/share/sumo/tools .venv/bin/python panan/scripts/gen_fengqing_flow.py \
        --count 500 --to-e10 400 --out panan/demand/fengqing_trips.xml
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import sumolib

# 奉庆线驶入目标路口的边及其上游
FENGQING_UPSTREAM = "787068627#1"   # 车辆从这里出发，沿奉庆线驶入目标路口
FENGQING_APPROACH = "787068627#2"   # 直接驶入 cluster_4178171527 的边
EXCLUDE_PREFIXES = ("787068627", "787068635")  # 奉庆线自身各段，不作终点
# 奉庆线 → E10 方向：cluster 出边 -E10 -> -E10.29（月山路）
TO_E10_EDGE = "-E10.29"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=500, help="常规奉庆线流量（随机终点）")
    ap.add_argument("--to-e10", type=int, default=400, help="奉庆线→E10方向专项流量")
    ap.add_argument("--begin", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=3600.0)
    ap.add_argument("--out", default="panan/demand/fengqing_trips.xml")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    net = sumolib.net.readNet("panan/network/panan.net.xml")

    # 候选终点：排除奉庆线自身与起点
    edges = [e.getID() for e in net.getEdges()
             if e.getFunction() == "" and e.getID() != FENGQING_UPSTREAM
             and not e.getID().startswith(EXCLUDE_PREFIXES)
             and e.getID() != FENGQING_APPROACH
             and e.getID() != TO_E10_EDGE]
    rng = random.Random(args.seed)
    n_random = args.count
    n_e10 = args.to_e10
    total = n_random + n_e10
    destinations = [rng.choice(edges) for _ in range(n_random)]

    span = args.end - args.begin

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        idx = 0
        # 常规奉庆线流量（随机终点）：均匀分布 0..end
        for i, dst in enumerate(destinations):
            dep = args.begin + span * i / max(1, n_random)
            f.write(
                f'    <trip id="fq{idx}" depart="{dep:.2f}" '
                f'from="{FENGQING_UPSTREAM}" to="{dst}"/>\n'
            )
            idx += 1
        # 奉庆线 → E10 方向专项流量：也均匀分布，且与常规流量错开半格
        for i in range(n_e10):
            dep = args.begin + span * (i + 0.5) / max(1, n_e10)
            f.write(
                f'    <trip id="fqe{idx}" depart="{dep:.2f}" '
                f'from="{FENGQING_UPSTREAM}" to="{TO_E10_EDGE}"/>\n'
            )
            idx += 1
        f.write("</routes>\n")
    print(f"已生成：常规奉庆线 {n_random} 辆 + 奉庆线→E10 {n_e10} 辆 = {total} 辆（均 0-{args.end:.0f}s 均匀分布）-> {out}")


if __name__ == "__main__":
    main()
