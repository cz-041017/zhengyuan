"""磐安县仿真 —— 一键可视化演示入口。

用法（仓库根目录，先 export PYTHONPATH=/usr/share/sumo/tools）：

    # 实时窗口（默认，需要图形界面）
    .venv/bin/python panan/scripts/demo.py

    # 实时窗口 + 自定义时长/速度
    .venv/bin/python panan/scripts/demo.py --end-time 600 --delay-ms 80

    # 离屏生成 GIF 动画（无图形界面也能用）
    .venv/bin/python panan/scripts/demo.py --gif panan/results/demo.gif --end-time 180

说明：自动使用 panan/config/panan.sumocfg 指向的路网（占位网格，或
fetch_and_build.sh 下载的真实磐安县路网）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from panan_bridge import run_simulation  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
SUMO_CFG = ROOT / "panan/config/panan.sumocfg"
CONFIG = ROOT / "panan/config/panan.yaml"


def make_gif(frames: list[Path], out: Path, fps: int) -> None:
    if not frames:
        print("未生成任何帧，跳过 GIF 合成。", flush=True)
        return
    try:
        from PIL import Image
    except ImportError:
        print("未安装 Pillow，跳过 GIF 合成（已保留逐帧 PNG）。", flush=True)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        imgs = [Image.open(p).convert("RGB") for p in frames]
        imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=int(1000 / fps), loop=0)
        print(f"GIF 已保存：{out}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"GIF 合成失败（已保留逐帧 PNG）：{exc}", flush=True)


def auto_view() -> tuple[float, float]:
    """在信号灯路口最密集的区域自动求视野中心（便于看清红绿灯）。

    对每个候选中心统计 1.5km 框内的信号灯数，取最密集者；否则回退到质心。
    """
    try:
        import sumolib

        from panan_bridge import NetInfo

        net = sumolib.net.readNet(str(ROOT / "panan/network/panan.net.xml"))
        ni = NetInfo.load(ROOT / "panan/network/panan.net.xml")
        coords = [net.getNode(jid).getCoord() for jid in ni.junctions]
        if not coords:
            return (0.0, 0.0)
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        R = 750.0  # 半框宽（米）
        best = (0, 0.0, 0.0)
        step = 200
        for cx in range(int(min(xs)) - int(R), int(max(xs)) + int(R) + 1, step):
            for cy in range(int(min(ys)) - int(R), int(max(ys)) + int(R) + 1, step):
                cnt = sum(1 for x, y in coords if abs(x - cx) < R and abs(y - cy) < R)
                if cnt > best[0]:
                    best = (cnt, float(cx), float(cy))
        if best[0] >= 2:
            return (best[1], best[2])
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    except Exception:
        return (0.0, 0.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="磐安县信号灯仿真可视化演示")
    ap.add_argument("--end-time", type=float, default=600.0)
    ap.add_argument("--delay-ms", type=float, default=100.0)
    ap.add_argument("--controller", choices=["adaptive", "fixed"], default="adaptive")
    ap.add_argument("--gif", default=None, help="输出 GIF 路径；指定后离屏生成动画")
    ap.add_argument("--every", type=float, default=3.0, help="GIF 每帧间隔(仿真秒)")
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--view-zoom", type=float, default=400.0)
    ap.add_argument("--view-center", default="auto", help="auto 或 'x,y'")
    args = ap.parse_args()

    if args.view_center == "auto":
        cx, cy = auto_view()
    else:
        cx, cy = (float(v) for v in args.view_center.split(","))

    if args.gif:
        frames_dir = ROOT / "panan/results/demo_frames"
        run_simulation(
            SUMO_CFG, CONFIG, controller=args.controller, end_time=args.end_time,
            gui=True, delay_ms=0.0, screenshot_dir=frames_dir, screenshot_every_s=args.every,
            view_offset=(cx, cy), view_zoom=args.view_zoom, close=False,
        )
        frames = sorted(frames_dir.glob("frame_*.png"))
        make_gif(frames, Path(args.gif), args.fps)
        # 离屏 GIF 模式：帧已全部落盘。清理 sumo-gui 子进程后直接退出，
        # 避免等待 GUI 优雅关闭（无显示器时可能阻塞）。
        import os
        import subprocess

        subprocess.run(["pkill", "-9", "-x", "sumo-gui"], check=False)
        os._exit(0)
    else:
        run_simulation(
            SUMO_CFG, CONFIG, controller=args.controller, end_time=args.end_time,
            gui=True, delay_ms=args.delay_ms,
            view_offset=(cx, cy), view_zoom=args.view_zoom,
        )


if __name__ == "__main__":
    main()
