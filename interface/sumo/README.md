# interface/sumo —— SUMO 仿真台

用 SUMO 仿真一个 4 向路口，把决策层当「软相位请求」驱动起来。

## 环境

- Ubuntu 24.04
- SUMO 1.28.0（`sudo apt install sumo sumo-tools sumo-doc` 通常装到 `/usr/share/sumo`）
- 项目 Python 环境（`uv sync` 已建好 `.venv`）

## 运行

```bash
export SUMO_HOME=/usr/share/sumo      # 若 apt 装；源码装则指向你的 sumo 目录

cd interface/sumo
chmod +x build.sh
./build.sh                            # 生成 intersection.net.xml

python run_simulation.py              # 无界面（sumo）
python run_simulation.py --gui        # 图形界面（sumo-gui）
```

`run_simulation.py` 会用 `traci` 把 SUMO 的 `tools` 目录加入 `sys.path`，因此必须已 `export SUMO_HOME`。

## 它做了什么（对应软请求三层）

1. **感知适配（感知层占位）**：读每根入口车道的车辆（`traci.lane.getLastStepVehicleIDs`）→ 映射到规范车道 ID → 组 `VehicleObservation`。
2. **控制器（软请求执行）**：把 `DecisionLayer.tick()` 返回的 `PhaseDecision` 当成相位指示，按配置的黄灯/全红秒数执行「黄灯 → 全红 → 新相位绿灯」。
3. **灯色仍由仿真器里的信号灯执行**：脚本只 `setRedYellowGreenState`，不越过信号机模型的安全语义。

> 当前用 `configs/simulation.yaml`（模式 B：主路 NS + 绿波）作为决策配置；下游占用暂未接（`downstream_occupancy=None`），绿波窗口由调用方未提供（`green_wave_active` 默认 False）。

## 文件

| 文件 | 作用 |
|---|---|
| `network.nod.xml` / `.edg.xml` / `.con.xml` | 4 向路口：每向 2 入口车道（直行 + 左转） |
| `build.sh` | `netconvert` 生成 `intersection.net.xml` |
| `trafficlight.add.xml` | 信号灯初始程序（运行时被脚本接管） |
| `demand.rou.xml` | 8 条路线（4 直行 + 4 左转）的车流 |
| `intersection.sumocfg` | 汇总配置 |
| `run_simulation.py` | TraCI 驱动 + 控制器 + 感知适配 |

## 规范车道 ID ↔ SUMO 车道

| 规范 ID | SUMO 边·车道 |
|---|---|
| `north_through` | `N2C_0` |
| `north_left` | `N2C_1` |
| `east_through` | `E2C_0` |
| `east_left` | `E2C_1` |
| `south_through` | `S2C_0` |
| `south_left` | `S2C_1` |
| `west_through` | `W2C_0` |
| `west_left` | `W2C_1` |

右转未建模（与决策层一致）。

## 验证清单（本机未装 SUMO，脚本未经真机执行）

1. `./build.sh` 无报错生成 `intersection.net.xml`；
2. `sumo -c intersection.sumocfg --no-step-log --duration-log.statistics` 能跑完；
3. `python run_simulation.py` 无 ImportError（`SUMO_HOME` 正确、`traci` 可导入）。
