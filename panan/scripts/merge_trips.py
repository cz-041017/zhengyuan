"""合并多个 trip 文件并按 depart 排序。

SUMO/duarouter 要求路由文件按 depart 升序，否则乱序车辆会被丢弃。
randomTrips 和奉庆线 trip 各自递增，但直接拼接会乱序，所以先合并排序再路由。

用法：
    .venv/bin/python panan/scripts/merge_trips.py 输出.xml 输入1.xml 输入2.xml ...
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: merge_trips.py 输出.xml 输入1.xml [输入2.xml ...]", file=sys.stderr)
        sys.exit(1)
    out = sys.argv[1]
    inputs = sys.argv[2:]
    trips: list[tuple[float, ET.Element]] = []
    for f in inputs:
        tree = ET.parse(f)
        for el in tree.getroot():
            if el.tag in ("trip", "flow", "vehicle"):
                trips.append((float(el.get("depart", 0)), el))
    trips.sort(key=lambda x: x[0])
    root = ET.Element("routes")
    for _, el in trips:
        root.append(el)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    print(f"合并排序 {len(trips)} 个 trip（来自 {len(inputs)} 个文件）-> {out}")


if __name__ == "__main__":
    main()
