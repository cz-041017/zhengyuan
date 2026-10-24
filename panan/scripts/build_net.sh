#!/usr/bin/env bash
# 一键重建磐安县路网 + 需求（可复现）—— 保留全部道路，不删除任何类型。
#
# 前置：panan/network/panan_osm_bbox.osm.xml（osmWebWizard 或 fetch_and_build.sh 产出）
# 用法：bash panan/scripts/build_net.sh
#
# 如需去除某类道路（如辅助道路），建议用 netedit 手动删，或用 netconvert：
#   netconvert -s panan.net.xml --remove-edges.by-type highway.residential,highway.living_street --remove-edges.isolated -o panan_clean.net.xml
set -euo pipefail

SUMO_TOOLS="${SUMO_TOOLS:-/usr/share/sumo/tools}"
export PYTHONPATH="$SUMO_TOOLS"
PY="${PY:-.venv/bin/python}"

OSM="panan/network/panan_osm_bbox.osm.xml"
NET="panan/network/panan.net.xml"
TYPO="$(dirname "$(dirname "$SUMO_TOOLS")")/data/typemap/osmNetconvert.typ.xml"
[ -f "$TYPO" ] || TYPO="/usr/share/sumo/data/typemap/osmNetconvert.typ.xml"

[ -f "$OSM" ] || { echo "缺少 $OSM（请先用 osmWebWizard 或 fetch_and_build.sh 获取）"; exit 1; }

echo "==> [1/3] 先构建一次以分析路口拓扑"
netconvert --osm-files "$OSM" --type-files "$TYPO" \
  --geometry.remove --roundabouts.guess --junctions.join \
  --output.street-names --keep-edges.by-vclass passenger \
  -o "$NET" >/dev/null 2>&1

echo "==> [2/3] 选取主要干净交叉口（驶入边≥3 且所有进口道≥30m，按重要性取前 25）"
TLS_SET="$("$PY" - "$NET" <<'PYEOF'
import sys, sumolib
net = sumolib.net.readNet(sys.argv[1])
cands = []
for n in net.getNodes():
    inc = n.getIncoming()
    if len(inc) < 3:
        continue
    if min(e.getLength() for e in inc) < 30:
        continue  # 排除 OSM 复杂路口簇（0.2m 连接段）
    lanes = sum(e.getLaneNumber() for e in inc)
    cands.append((lanes + 3 * len(inc), n.getID()))
cands.sort(reverse=True)
print(",".join(i for _, i in cands[:25]))
PYEOF
)"
echo "    选中 $(echo "$TLS_SET" | tr ',' '\n' | wc -l) 个信号灯路口"

echo "==> [3/3] 用 --tls.set 重建路网 + 生成需求"
netconvert --osm-files "$OSM" --type-files "$TYPO" \
  --geometry.remove --roundabouts.guess --junctions.join \
  --tls.set "$TLS_SET" \
  --output.street-names --keep-edges.by-vclass passenger \
  -o "$NET" >/dev/null 2>&1

python3 "$SUMO_TOOLS/randomTrips.py" -n "$NET" \
  -o panan/demand/panan_trips.xml -e 3600 --period 1.5 --fringe-factor 5 --min-distance 300 --seed 42 >/dev/null 2>&1
duarouter -n "$NET" -r panan/demand/panan_trips.xml \
  -o panan/demand/panan_routes.rou.xml --seed 42 --ignore-errors >/dev/null 2>&1

echo "==> 完成：$NET"
echo "    路口数：$(grep -c '<junction ' "$NET")"
echo "    信号灯路口：$(grep -c 'type=\"traffic_light\"' "$NET")"
echo "    车辆数：$(grep -c '<vehicle ' panan/demand/panan_routes.rou.xml)"
