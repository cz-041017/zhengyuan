#!/usr/bin/env bash
# 从 OpenStreetMap 下载「金华市磐安县」真实路网并转换为 SUMO 网络。
#
# ⚠️ 重要：若命令行访问 Overpass 被拦截（overpass-api.de 返回 406、镜像 500、
#    OSM API 超时），请改用 **osmWebWizard.py**（它用浏览器发请求，通常能成功）：
#
#       python3 $SUMO_HOME/tools/osmWebWizard.py
#       # 在打开的网页地图上框选磐安县区域 → Generate scenario
#       # 产物目录（如 /home/ubuntu/2026-.../）里有 osm_bbox.osm.xml.gz / osm.net.xml.gz
#       # 把该目录的 osm_bbox.osm.xml.gz 拷到 panan/network/ 后，直接用下面的
#       # netconvert 步骤重建即可（并把 --tls.set 换成你需要的信号灯路口）。
#
# 用法（在能访问 Overpass/OSM 的机器上，仓库根目录执行）：
#     bash panan/scripts/fetch_and_build.sh
#
# 依赖：sumo / netconvert / duarouter / randomTrips.py / curl
#       Python 绑定需在 PYTHONPATH（默认 /usr/share/sumo/tools）
#
# 说明：本脚本用 Overpass API 抓取以 (120.430890, 29.036192) 为中心约 3km×3km
#       的机动车道路，转换为带信号灯的路网，并生成 1 小时随机 OD 需求。

set -euo pipefail

SUMO_TOOLS="${SUMO_TOOLS:-/usr/share/sumo/tools}"
export PYTHONPATH="$SUMO_TOOLS"

CENTER_LON=120.430890
CENTER_LAT=29.036192
# bbox: 西,南,东,北（约 3.1km × 3.1km）
WEST=$(python3 -c "print($CENTER_LON - 0.016)")
SOUTH=$(python3 -c "print($CENTER_LAT - 0.014)")
EAST=$(python3 -c "print($CENTER_LON + 0.016)")
NORTH=$(python3 -c "print($CENTER_LAT + 0.014)")

NET_DIR="panan/network"
DEMAND_DIR="panan/demand"
mkdir -p "$NET_DIR" "$DEMAND_DIR"

echo "==> 下载 OSM 数据 bbox=$WEST,$SOUTH,$EAST,$NORTH"
if ! curl -fsS -G "https://overpass-api.de/api/interpreter" \
    --data-urlencode "data=[out:xml][timeout:180];(way[\"highway\"]( $SOUTH,$WEST,$NORTH,$EAST ););out body;>;out skel qt;" \
    -o "$NET_DIR/panan.osm.xml"; then
  echo "!! overpass-api.de 失败，尝试镜像 overpass.private.coffee"
  curl -fsS -G "https://overpass.private.coffee/api/interpreter" \
    --data-urlencode "data=[out:xml][timeout:180];(way[\"highway\"]( $SOUTH,$WEST,$NORTH,$EAST ););out body;>;out skel qt;" \
    -o "$NET_DIR/panan.osm.xml"
fi
echo "    OSM 文件大小: $(du -h "$NET_DIR/panan.osm.xml" | cut -f1)"

echo "==> 转换为 SUMO 路网（netconvert）"
netconvert \
  --osm-files "$NET_DIR/panan.osm.xml" \
  -o "$NET_DIR/panan.net.xml" \
  --type-files "$SUMO_TOOLS/../data/typemap/osmNetconvert.typ.xml" \
  --geometry.remove \
  --roundabouts.guess \
  --junctions.join \
  --tls.guess-signals \
  --tls.discard-simple \
  --tls.join \
  --output.street-names \
  --remove-edges.isolated \
  --keep-edges.by-vclass passenger

echo "==> 生成随机 OD 需求（1 小时）"
python3 "$SUMO_TOOLS/randomTrips.py" \
  -n "$NET_DIR/panan.net.xml" \
  -o "$DEMAND_DIR/trips.xml" \
  -e 3600 --period 1.5 --fringe-factor 5 --min-distance 400 --seed 42

echo "==> 路由分配（duarouter）"
duarouter -n "$NET_DIR/panan.net.xml" -r "$DEMAND_DIR/trips.xml" \
  -o "$DEMAND_DIR/routes.rou.xml" --seed 42 --ignore-errors

echo "==> 完成。生成文件："
ls -la "$NET_DIR/panan.net.xml" "$DEMAND_DIR/routes.rou.xml"
echo
echo "下一步（生成 .sumocfg 并运行仿真）："
echo "  .venv/bin/python panan/scripts/make_sumocfg.py"
