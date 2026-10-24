# SUMO 仿真平台（仿真台）

把本项目（`src/traffic_control`）逻辑决策层接入 SUMO 的桥接与实验脚本。

## 依赖

- SUMO ≥ 1.28（`sumo` / `netgenerate` / `duarouter` / `randomTrips.py` / `libsumo`）
- Python 3.11（`.venv`，含 `pyyaml`；决策层通过 editable 安装）
- SUMO Python 绑定需加入 `PYTHONPATH=/usr/share/sumo/tools`

## 一键复现

```bash
cd /home/ubuntu/zhengyuan
export PYTHONPATH=/usr/share/sumo/tools

# 1. 生成 9×9 路网
netgenerate --grid --grid.number 9 --grid.length 150 --grid.attach-length 120 \
  --default-junction-type traffic_light --default.lanenumber 1 --no-turnarounds \
  -o sumo/network/grid.net.xml

# 2. 生成需求（6001 车次）
python3 /usr/share/sumo/tools/randomTrips.py -n sumo/network/grid.net.xml \
  -o sumo/demand/trips.xml -e 3600 --period 0.6 --fringe-factor 5 \
  --min-distance 600 --seed 42
duarouter -n sumo/network/grid.net.xml -r sumo/demand/trips.xml \
  -o sumo/demand/routes.rou.xml --seed 42

# 3. 单次实验
.venv/bin/python sumo/scripts/run_simulation.py \
  --sumocfg sumo/config/grid.sumocfg --config sumo/config/adaptive_base.yaml \
  --controller adaptive --end-time 3600 --metrics sumo/results/metrics/x.csv

# 4. 批量实验（固定配时对比 + 参数扫描）
.venv/bin/python sumo/scripts/make_experiments.py
```

## 可视化仿真（sumo-gui）

**实时窗口模式**（需要图形界面 / 显示器）：

```bash
export PYTHONPATH=/usr/share/sumo/tools
.venv/bin/python sumo/scripts/run_simulation.py \
  --sumocfg sumo/config/grid.sumocfg \
  --config sumo/config/adaptive_base.yaml \
  --controller adaptive \
  --gui \
  --end-time 600 \
  --delay-ms 100 \
  --metrics sumo/results/metrics/gui.csv
```

- `--gui`：用 `sumo-gui` 打开可视化窗口，由决策层实时控制 81 个路口的信号灯。
- `--delay-ms`：每仿真步之间的真实时间（默认 100ms ≈ 10 帧/秒；调小更快、调大更慢）。
- `--end-time`：观看时长（仿真秒）。想多看就调大；窗口会在结束后自动关闭。
- 对比固定配时：把 `--controller adaptive` 换成 `--controller fixed --fixed-green-s 20`。

**离屏生成演示动画（GIF，无需显示器）**：

```bash
export PYTHONPATH=/usr/share/sumo/tools
.venv/bin/python sumo/scripts/capture_demo.py \
  --end-time 180 --every 3 --out-dir sumo/results/demo \
  --gif sumo/results/demo.gif --fps 10
```

> 说明：`sumo-gui -c grid.sumocfg` 直接运行只会用 SUMO 自带的固定配时，**不会**走本项目的决策层；
> 要看到「决策层实时控灯」必须通过上面的 `--gui` 脚本（桥接层通过 TraCI 接管信号灯）。

## 文件

| 文件 | 说明 |
|---|---|
| `scripts/sumo_bridge.py` | 桥接层：NetInfo 拓扑映射、JunctionController、Simulation 主循环与指标 |
| `scripts/run_simulation.py` | 单次实验 CLI |
| `scripts/make_experiments.py` | 批量实验（扫描 + 固定配时对比），输出 `results/experiments.json` |
| `config/grid.sumocfg` | SUMO 运行配置 |
| `config/adaptive_base.yaml` | 决策层配置（模式 A 四相位，显式配时便于调参） |
| `results/metrics/*.csv` | 各实验逐秒指标 |

## 关键设计

- 每路口独立装配一套决策层运行时；`PhaseDecision` → SUMO 红黄绿状态；
  黄灯(3s)/全红(1s)清空由 `SignalPhase` 配时驱动，`FeedbackMonitor` 回传执行确认。
- 右转折入直行需求（决策层仅建模 8 个流向）。
- P1 溢流：movement 下游边 `occupancy` 喂给 `SpillbackMonitor`（`o_high=0.8` 滞回）。
