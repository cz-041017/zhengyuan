"""生成仿真演示动画：用 sumo-gui 离屏渲染逐帧截图，可合成 GIF。

用法：
    PYTHONPATH=/usr/share/sumo/tools .venv/bin/python sumo/scripts/capture_demo.py \
        --end-time 180 --every 3 --out-dir sumo/results/demo --gif sumo/results/demo.gif
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sumo_bridge import run_simulation  # noqa: E402

HERE = Path(__file__).parent
ROOT = HERE.parent.parent


def make_gif(frames: list[Path], out: Path, fps: int = 10) -> None:
    try:
        from PIL import Image
    except ImportError:
        print("未安装 Pillow，跳过 GIF 合成（已保存逐帧 PNG）。", flush=True)
        return
    imgs = [Image.open(p).convert("RGB") for p in frames]
    imgs[0].save(
        out,
        save_all=True,
        append_images=imgs[1:],
        duration=int(1000 / fps),
        loop=0,
    )
    print(f"GIF saved: {out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="生成仿真演示动画")
    ap.add_argument("--end-time", type=float, default=180.0)
    ap.add_argument("--every", type=float, default=3.0, help="截图间隔(仿真秒)")
    ap.add_argument("--out-dir", default=str(ROOT / "sumo/results/demo"))
    ap.add_argument("--gif", default=None, help="GIF 输出路径")
    ap.add_argument("--fps", type=int, default=10)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    run_simulation(
        ROOT / "sumo/config/grid.sumocfg",
        ROOT / "sumo/config/adaptive_base.yaml",
        controller="adaptive",
        end_time=args.end_time,
        gui=True,
        delay_ms=0.0,
        screenshot_dir=out_dir,
        screenshot_every_s=args.every,
        # 居中放大到约 3×3 路口视野，便于看清车流与信号灯。
        view_offset=(720.0, 720.0),
        view_zoom=300.0,
    )

    frames = sorted(out_dir.glob("frame_*.png"))
    print(f"frames: {len(frames)}", flush=True)
    if args.gif and frames:
        make_gif(frames, Path(args.gif), args.fps)


if __name__ == "__main__":
    main()
