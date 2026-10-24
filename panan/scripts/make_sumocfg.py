"""为真实 OSM 路网生成 .sumocfg（配合 fetch_and_build.sh 使用）。

用法：
    .venv/bin/python panan/scripts/make_sumocfg.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # 仓库根目录


def main() -> None:
    net = ROOT / "panan/network/panan.net.xml"
    routes = ROOT / "panan/demand/routes.rou.xml"
    if not net.exists() or not routes.exists():
        raise SystemExit("请先运行 fetch_and_build.sh 生成 panan.net.xml 与 routes.rou.xml")

    cfg = ROOT / "panan/config/panan.sumocfg"
    cfg.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/sumoConfiguration.xsd">\n'
        "    <input>\n"
        '        <net-file value="../network/panan.net.xml"/>\n'
        '        <route-files value="../demand/routes.rou.xml"/>\n'
        "    </input>\n"
        "    <time>\n"
        '        <begin value="0"/>\n'
        '        <end value="3600"/>\n'
        '        <step-length value="1.0"/>\n'
        "    </time>\n"
        "    <processing>\n"
        '        <ignore-route-errors value="true"/>\n'
        "    </processing>\n"
        "    <report>\n"
        '        <no-warnings value="true"/>\n'
        "    </report>\n"
        "</configuration>\n",
        encoding="utf-8",
    )
    print(f"已生成 {cfg}")


if __name__ == "__main__":
    main()
