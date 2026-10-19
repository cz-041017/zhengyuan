#!/usr/bin/env bash
# Build the road network with netconvert (SUMO 1.28.0).
set -euo pipefail
cd "$(dirname "$0")"

netconvert \
  --node-files=network.nod.xml \
  --edge-files=network.edg.xml \
  --connection-files=network.con.xml \
  --tllogic-files=trafficlight.add.xml \
  --output-file=intersection.net.xml

echo "Built intersection.net.xml"
