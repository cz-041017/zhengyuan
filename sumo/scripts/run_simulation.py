"""命令行入口：运行一次 SUMO 仿真实验。

用法示例：
    PYTHONPATH=/usr/share/sumo/tools python run_simulation.py \
        --sumocfg ../config/grid.sumocfg \
        --config ../../configs/default.yaml \
        --controller adaptive \
        --end-time 3600 \
        --metrics ../results/baseline_adaptive.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sumo_bridge import run_simulation, summarize


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a SUMO + decision-layer experiment")
    ap.add_argument("--sumocfg", required=True, help="SUMO config (.sumocfg)")
    ap.add_argument("--config", required=True, help="decision-layer YAML config")
    ap.add_argument(
        "--controller", choices=["adaptive", "fixed"], default="adaptive"
    )
    ap.add_argument("--fixed-green-s", type=float, default=30.0)
    ap.add_argument("--end-time", type=float, default=3600.0)
    ap.add_argument("--step-length", type=float, default=1.0)
    ap.add_argument("--metrics", required=True, help="output CSV path")
    ap.add_argument("--summary-json", default=None, help="optional summary JSON path")
    ap.add_argument(
        "--gui",
        action="store_true",
        help="launch sumo-gui for live visualization instead of headless sumo",
    )
    ap.add_argument(
        "--delay-ms",
        type=float,
        default=100.0,
        help="real-time delay per simulation step in GUI mode (default 100ms)",
    )
    args = ap.parse_args()

    result = run_simulation(
        args.sumocfg,
        args.config,
        controller=args.controller,
        fixed_green_s=args.fixed_green_s,
        end_time=args.end_time,
        step_length=args.step_length,
        metrics_path=Path(args.metrics),
        gui=args.gui,
        delay_ms=args.delay_ms,
    )

    summary = summarize(result.rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.summary_json:
        Path(args.summary_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
