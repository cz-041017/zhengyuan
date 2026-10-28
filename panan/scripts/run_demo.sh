#!/usr/bin/env bash
# 一键可视化仿真（便于展示；实时窗口默认长时间运行，可随时关闭窗口手动停止）。
#
# 用法（仓库根目录）：
#   bash panan/scripts/run_demo.sh                # 实时窗口：自适应决策层控灯（长时运行，手动关闭停止）
#   bash panan/scripts/run_demo.sh fixed           # 实时窗口：固定配时对照
#   bash panan/scripts/run_demo.sh gif             # 离屏生成 GIF 动画（自适应，180s）
#   bash panan/scripts/run_demo.sh gif fixed       # 离屏生成 GIF（固定配时）
#
# 前置：export PYTHONPATH=/usr/share/sumo/tools（或 $SUMO_TOOLS）
set -euo pipefail

cd "$(dirname "$0")/../.."          # 切到仓库根目录
SUMO_TOOLS="${SUMO_TOOLS:-/usr/share/sumo/tools}"
export PYTHONPATH="$SUMO_TOOLS"

MODE="${1:-adaptive}"       # adaptive | fixed | gif
MODE2="${2:-}"

# 解析控制器与是否生成 GIF
CTRL="adaptive"
GIF=""
END="86400"                 # 实时窗口默认长时运行（可手动关闭停止）
DELAY="100"
case "$MODE" in
  fixed)  CTRL="fixed";;
  gif)    GIF="panan/results/demo.gif"; END="180"; [ "$MODE2" = "fixed" ] && CTRL="fixed";;
esac

# 需求不存在、或路网比需求新（改过路网）→ 重新生成需求
if [ ! -f panan/demand/panan_routes.rou.xml ] || [ panan/network/panan.net.xml -nt panan/demand/panan_routes.rou.xml ]; then
  echo "==> 生成随机 OD 需求（路网已变更或需求缺失）"
  python3 "$SUMO_TOOLS/randomTrips.py" -n panan/network/panan.net.xml \
    -o panan/demand/panan_trips.xml -e 3600 --period 1.5 --fringe-factor 5 --min-distance 300 --seed 42
  echo "==> 生成奉庆线额外车流"
  .venv/bin/python panan/scripts/gen_fengqing_flow.py --count 400 --out panan/demand/fengqing_trips.xml
  duarouter -n panan/network/panan.net.xml -r panan/demand/panan_trips.xml,panan/demand/fengqing_trips.xml \
    -o panan/demand/panan_routes.rou.xml --seed 42 --ignore-errors
fi

echo "==> 可视化仿真：controller=$CTRL $( [ -n "$GIF" ] && echo "→ GIF $GIF ($END s)" || echo "→ 实时窗口 $END s, delay=$DELAY ms" )"
if [ -n "$GIF" ]; then
  .venv/bin/python panan/scripts/demo.py --gif "$GIF" --controller "$CTRL" --end-time "$END" --every 2 --fps 6
else
  .venv/bin/python panan/scripts/demo.py --controller "$CTRL" --end-time "$END" --delay-ms "$DELAY"
fi
