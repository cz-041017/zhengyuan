"""一次权重调优迭代：改权重 → 压测 → 24h 仿真 → 全部记录到数据中台（可回溯）。

流程（对应 goal 里的"压测-仿真-总结-调优-压测"循环）：
    1. new iteration（记录 note + git commit）
    2. 修改 configs/default.yaml 的 demand 权重（vehicle_weight / wait_weight）
    3. ingest-config（参数快照）
    4. 跑 bench（L1 压测，不变量 + 性能）
    5. ingest-bench
    6. 跑 24h 仿真（自适应）
    7. ingest-sim

用法（仓库根目录）：
    .venv/bin/python panan/scripts/tune_iteration.py \
        --vehicle-weight 0.7 --wait-weight 0.3 --note "第1组：加重车流权重"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
CONFIG = REPO / "configs" / "default.yaml"
BENCH = REPO / "traffic-control-handoff" / "bench" / "run_benchmark.py"
SUMO_CFG = REPO / "panan" / "config" / "panan.sumocfg"
PY = REPO / ".venv" / "bin" / "python"


def run(cmd: list[str], **kw) -> None:
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, cwd=REPO, **kw)
    if r.returncode != 0:
        print(f"  失败：exit={r.returncode}", file=sys.stderr)


def platform_cmd(*args) -> list[str]:
    return [str(PY), "-m", "data_platform", *args]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle-weight", type=float, required=True)
    ap.add_argument("--wait-weight", type=float, required=True)
    ap.add_argument("--note", default=None)
    ap.add_argument("--end-time", type=float, default=86400.0)
    ap.add_argument("--skip-bench", action="store_true", help="跳过压测（只改权重+仿真）")
    ap.add_argument("--skip-sim", action="store_true", help="跳过仿真（只改权重+压测）")
    args = ap.parse_args()

    vw = args.vehicle_weight
    ww = args.wait_weight
    note = args.note or f"权重 vehicle={vw} wait={ww}"

    # 1. 改权重
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    cfg["optimization"]["demand"]["vehicle_weight"] = vw
    cfg["optimization"]["demand"]["wait_weight"] = ww
    CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"已改权重：vehicle_weight={vw} wait_weight={ww}")

    # 2. 开迭代 + 快照
    it = subprocess.check_output(
        platform_cmd("new", "--note", note), text=True
    ).strip()
    print(f"iteration_id={it}")
    run(platform_cmd("ingest-config", it, str(CONFIG.relative_to(REPO))))

    # 3. 压测
    if not args.skip_bench:
        bench_json = REPO / "panan" / "results" / f"bench_it{it}.json"
        run(
            [str(PY), str(BENCH.relative_to(REPO)), "--json-out", str(bench_json.relative_to(REPO))],
            env={**os.environ, "PYTHONPATH": f"{BENCH.parent}:{REPO}"},
        )
        run(platform_cmd("ingest-bench", it, str(bench_json.relative_to(REPO))))

    # 4. 仿真
    if not args.skip_sim:
        csv_path = REPO / "panan" / "results" / f"metrics_it{it}.csv"
        run(
            [
                str(PY), "panan/scripts/panan_bridge.py",
                "--sumocfg", str(SUMO_CFG.relative_to(REPO)),
                "--config", str(CONFIG.relative_to(REPO)),
                "--controller", "adaptive",
                "--end-time", str(args.end_time),
                "--csv", str(csv_path.relative_to(REPO)),
            ],
            env={**os.environ, "PYTHONPATH": f"/usr/share/sumo/tools:{REPO}"},
        )
        run(platform_cmd("ingest-sim", it, str(csv_path.relative_to(REPO))))

    print(f"迭代 {it} 完成：{note}")


if __name__ == "__main__":
    main()
