"""检查每个信号灯路口的相位分组，重点标出「被合并」的路口。

用法（仓库根目录）：
    PYTHONPATH=/usr/share/sumo/tools .venv/bin/python panan/scripts/inspect_phases.py

输出：每个路口的驶入边数 / 相位数，以及每条驶入边的朝向角与来向；
若「驶入边数 > 相位数」说明有两条来路被合并进了同一相位，会标 ⚠️。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sumolib  # noqa: E402

from traffic_control.config_loader import load_config  # noqa: E402
from traffic_control.phasing import phase_groups  # noqa: E402

from panan_bridge import NetInfo, build_junction_phases  # noqa: E402


def main() -> None:
    cfg = load_config("configs/default.yaml")
    groups = phase_groups(cfg["signal"]["layout"]["mode"], cfg["signal"]["layout"].get("main_road_direction"))
    ni = NetInfo.load("panan/network/panan.net.xml")
    net = sumolib.net.readNet("panan/network/panan.net.xml")

    rows = []
    for jid, topo in ni.junctions.items():
        phases, _pa, _ap = build_junction_phases(topo, groups)
        rows.append((jid, topo, len(topo.edge_approach), len(phases)))

    rows.sort(key=lambda r: (r[2] == r[3], r[0]))  # 合并的排前面

    print("=" * 80)
    print("信号灯路口相位分组检查（模式A minor_only：P_N/P_E/P_S/P_W，直行+左转合并）")
    merged = [r for r in rows if r[2] != r[3]]
    print(f"信号灯路口总数：{len(rows)}   被合并路口数：{len(merged)}")
    print("=" * 80)

    for jid, topo, n_in, n_ph in rows:
        flag = "⚠️ 合并" if n_in != n_ph else "  OK"
        print(f"\n[{flag}] {jid}   驶入边={n_in}  相位={n_ph}")

        # 按来向分组显示
        by_appr: dict[str, list[tuple[str, float]]] = {}
        for eid, appr in topo.edge_approach.items():
            e = net.getEdge(eid)
            shp = e.getShape()
            (x0, y0), (x1, y1) = shp[-2], shp[-1]
            ang = math.degrees(math.atan2(y1 - y0, x1 - x0))
            by_appr.setdefault(appr, []).append((eid, ang))

        for appr in sorted(by_appr):
            edges = by_appr[appr]
            for eid, ang in edges:
                print(f"    {eid:26s} 朝向={ang:7.1f}°  -> {appr}")
            if len(edges) > 1:
                print(f"      └─ ⚠️ 以上 {len(edges)} 条边被合并进同一相位 P_{appr}")


if __name__ == "__main__":
    main()
