"""批量运行仿真实验（基线 / 固定配时对比 / 参数扫描）。

输出：
- sumo/results/experiments.json  每组的摘要指标
- sumo/results/metrics/*.csv     每组的逐秒指标
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from sumo_bridge import run_simulation, summarize  # noqa: E402

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
SUMO_CFG = ROOT / "sumo/config/grid.sumocfg"
BASE_CONFIG = ROOT / "sumo/config/adaptive_base.yaml"
RESULTS = ROOT / "sumo/results"
METRICS = RESULTS / "metrics"


def _set_dotted(cfg: dict, path: str, value) -> None:
    keys = path.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value


def build_config(overrides: dict, out: Path) -> None:
    with BASE_CONFIG.open() as f:
        cfg = yaml.safe_load(f)

    min_green = overrides.pop("_min_green_s", None)
    max_green = overrides.pop("_max_green_s", None)
    for k, v in overrides.items():
        _set_dotted(cfg, k, v)

    if min_green is not None or max_green is not None:
        for phase in cfg["signal"]["phases"]:
            if min_green is not None:
                phase["min_green_s"] = float(min_green)
            if max_green is not None:
                phase["max_green_s"] = float(max_green)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def run_one(name: str, controller: str, overrides: dict, end_time: float,
            fixed_green_s: float | None = None) -> dict:
    cfg_path = RESULTS / f"cfg_{name}.yaml"
    if controller == "adaptive":
        build_config(overrides, cfg_path)
    else:
        cfg_path = BASE_CONFIG
    metrics = METRICS / f"{name}.csv"
    result = run_simulation(
        SUMO_CFG,
        cfg_path,
        controller=controller,
        fixed_green_s=fixed_green_s or 30.0,
        end_time=end_time,
        metrics_path=metrics,
    )
    summary = summarize(result.rows)
    # 各路口统计汇总
    n_junc = len(result.per_junction)
    tot_switch = sum(v["switches"] for v in result.per_junction.values())
    tot_p0 = sum(v["p0_preempts"] for v in result.per_junction.values())
    tot_conflict = sum(v["p0_p1_conflicts"] for v in result.per_junction.values())
    summary.update(
        {
            "name": name,
            "controller": controller,
            "junctions": n_junc,
            "total_switches": tot_switch,
            "total_p0_preempts": tot_p0,
            "total_p0_p1_conflicts": tot_conflict,
            "wall_s": round(result.elapsed_wall_s, 2),
        }
    )
    return summary


def main() -> None:
    end_time = 1800.0
    full_end = 3600.0
    experiments: list[dict] = []

    # ---- 固定配时对比 ----
    for g in (20.0, 30.0, 45.0):
        experiments.append(
            {"name": f"fixed_g{int(g)}", "ctrl": "fixed", "fixed": g}
        )

    # ---- 自适应基线 + 参数扫描 ----
    experiments.append({"name": "adaptive_baseline", "ctrl": "adaptive"})
    experiments.append(
        {"name": "maxgreen30", "ctrl": "adaptive", "over": {"_max_green_s": 30.0}}
    )
    experiments.append(
        {"name": "maxgreen45", "ctrl": "adaptive", "over": {"_max_green_s": 45.0}}
    )
    experiments.append(
        {
            "name": "wait_target90",
            "ctrl": "adaptive",
            "over": {"optimization.max_wait_target_s": 90.0},
        }
    )
    experiments.append(
        {
            "name": "wait_target150",
            "ctrl": "adaptive",
            "over": {"optimization.max_wait_target_s": 150.0},
        }
    )
    experiments.append(
        {
            "name": "weight_waitheavy",
            "ctrl": "adaptive",
            "over": {
                "optimization.demand.vehicle_weight": 0.4,
                "optimization.demand.wait_weight": 0.6,
            },
        }
    )
    experiments.append(
        {
            "name": "weight_vehheavy",
            "ctrl": "adaptive",
            "over": {
                "optimization.demand.vehicle_weight": 0.8,
                "optimization.demand.wait_weight": 0.2,
            },
        }
    )
    experiments.append(
        {"name": "mingreen15", "ctrl": "adaptive", "over": {"_min_green_s": 15.0}}
    )
    experiments.append(
        {
            "name": "countdown10",
            "ctrl": "adaptive",
            "over": {"countdown.display_duration_s": 10.0},
        }
    )

    results = []
    for exp in experiments:
        print(f"\n=== {exp['name']} ===", flush=True)
        summary = run_one(
            exp["name"],
            exp.get("ctrl", "adaptive"),
            exp.get("over", {}),
            end_time,
            fixed_green_s=exp.get("fixed"),
        )
        results.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    (RESULTS / "experiments.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nSaved", RESULTS / "experiments.json")


if __name__ == "__main__":
    main()
