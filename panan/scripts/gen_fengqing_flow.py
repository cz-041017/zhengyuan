"""生成「从奉庆线来」的额外车流，注入到目标路口 cluster_4178171527_...。

奉庆线驶入目标路口的边是 787068627#2，其上游是 787068627#1。
本脚本生成一批 trip：起点=奉庆线上游 787068627#1，终点=路网随机边，
让这些车辆沿奉庆线驶入目标路口，形成明显的车流。

用法：
    PYTHONPATH=/usr/share/sumo/tools .venv/bin/python panan/scripts/gen_fengqing_flow.py \
        --count 400 --out panan/demand/fengqing_trips.xml
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=400)
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
             and e.getID() != FENGQING_APPROACH]
    rng = random.Random(args.seed)
    destinations = [rng.choice(edges) for _ in range(args.count)]

    # depart 均匀分布
    span = args.end - args.begin
    departs = [args.begin + span * i / args.count for i in range(args.count)]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        for i, (dep, dst) in enumerate(zip(departs, destinations)):
            f.write(
                f'    <trip id="fq{i}" depart="{dep:.2f}" '
                f'from="{FENGQING_UPSTREAM}" to="{dst}"/>\n'
            )
        f.write("</routes>\n")
    print(f"已生成 {args.count} 个奉庆线 trip -> {out}")


if __name__ == "__main__":
    main()
