#!/usr/bin/env bash
# 独立运行仿真验证 —— 封装标准参数，结果追加到 panan/results/metrics.csv。
#
# 用法（仓库根目录）：
#   bash panan/scripts/run_eval.sh adaptive          # 自适应决策层，3600s
#   bash panan/scripts/run_eval.sh fixed             # 固定配时对照，3600s
#   bash panan/scripts/run_eval.sh adaptive 1800      # 自定义时长(秒)
#   bash panan/scripts/run_eval.sh adaptive 3600 regen   # 改了路网后：重新生成需求再跑
#
# 前置：
#   - 路网 panan/network/panan.net.xml（你手动绘制的那份）
#   - 依赖已装：.venv（pyyaml + traffic_control 可导入）
#   - SUMO Python 绑定：export PYTHONPATH=/usr/share/sumo/tools（或 $SUMO_TOOLS）
set -euo pipefail

cd "$(dirname "$0")/../.."          # 切到仓库根目录
SUMO_TOOLS="${SUMO_TOOLS:-/usr/share/sumo/tools}"
export PYTHONPATH="$SUMO_TOOLS"

CTRL="${1:-adaptive}"
END="${2:-3600}"
REGEN="${3:-}"

# 改过路网后 / 需求缺失 / 路网比需求新 → 重新生成需求（seed=42，含奉庆线 400 辆）
if [ "$REGEN" = "regen" ] || [ ! -f panan/demand/panan_routes.rou.xml ] || [ panan/network/panan.net.xml -nt panan/demand/panan_routes.rou.xml ]; then
  echo "==> 生成随机 OD 需求（路网已变更或需求缺失）"
  python3 "$SUMO_TOOLS/randomTrips.py" -n panan/network/panan.net.xml \
    -o panan/demand/panan_trips.xml -e "$END" --period 1.5 --fringe-factor 5 --min-distance 300 --seed 42
  echo "==> 生成奉庆线额外车流"
  .venv/bin/python panan/scripts/gen_fengqing_flow.py --count 400 --out panan/demand/fengqing_trips.xml
  duarouter -n panan/network/panan.net.xml -r panan/demand/panan_trips.xml,panan/demand/fengqing_trips.xml \
    -o panan/demand/panan_routes.rou.xml --seed 42 --ignore-errors
fi

echo "==> 运行仿真：controller=$CTRL end=$END"
if [ "$CTRL" = "fixed" ]; then
  .venv/bin/python panan/scripts/panan_bridge.py \
    --sumocfg panan/config/panan.sumocfg --config configs/default.yaml \
    --controller fixed --fixed-green-s 30 --end-time "$END" \
    --csv panan/results/metrics.csv
else
  .venv/bin/python panan/scripts/panan_bridge.py \
    --sumocfg panan/config/panan.sumocfg --config configs/default.yaml \
    --controller adaptive --end-time "$END" \
    --csv panan/results/metrics.csv
fi
