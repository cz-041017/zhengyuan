# 磐安县交通信号灯仿真（真实坐标 120.430890, 29.036192）

把本项目逻辑决策层接入**真实/任意拓扑路网**的信号灯控制，配套可视化演示与自动报告。

## 目录

```
panan/
├── network/                # 路网（占位网格 + 真实 OSM 下载产物）
├── demand/                 # trips + 路由
├── config/
│   ├── panan.yaml          # 决策层参数
│   └── panan.sumocfg       # SUMO 运行配置
├── scripts/
│   ├── fetch_and_build.sh  # 一键下载真实磐安县 OSM 路网并转 SUMO（需联网）
│   ├── make_sumocfg.py     # 为真实路网生成 .sumocfg
│   ├── panan_bridge.py     # 通用桥接层（接口层）+ 仿真入口
│   ├── demo.py             # 可视化演示入口（实时窗口 / GIF）
│   └── make_report.py      # 生成仿真报告
└── results/
    ├── 仿真报告.md          # 最终报告
    ├── adaptive_report.json / fixed_report.json
    └── demo.gif             # 演示动画
```

## 快速开始（一键可视化，便于展示）

```bash
cd /home/ubuntu/zhengyuan
export PYTHONPATH=/usr/share/sumo/tools          # 换成你机器上 SUMO 工具目录

# 一键实时窗口：自适应决策层控灯（默认 1200s）
bash panan/scripts/run_demo.sh

# 一键固定配时对照
bash panan/scripts/run_demo.sh fixed

# 一键离屏生成 GIF 动画（无图形界面也能用）
bash panan/scripts/run_demo.sh gif
```

底层等价命令（更多参数用 demo.py）：

```bash
.venv/bin/python panan/scripts/demo.py --end-time 600 --delay-ms 100   # 实时窗口
.venv/bin/python panan/scripts/demo.py --gif panan/results/demo.gif --end-time 180  # GIF
```

## 独立运行仿真验证（改完逻辑层后，一条命令重新评估）

每次运行会自动把 **4 项指标追加一行** 到 `panan/results/metrics.csv`：

```bash
# 自适应决策层（默认 3600s）
bash panan/scripts/run_eval.sh adaptive

# 固定配时对照
bash panan/scripts/run_eval.sh fixed

# 自定义时长 / 改了路网后重新生成需求
bash panan/scripts/run_eval.sh adaptive 1800
bash panan/scripts/run_eval.sh adaptive 3600 regen
```

CSV 列：`timestamp, controller, config, net, seed, duration_s, arrived, avg_delay_s(平均车均延误), avg_queue_veh(平均排队车辆数), capacity_veh_per_h(通行能力), conflicts(冲突点数量)`。

> 底层命令（等价）：
> ```bash
> .venv/bin/python panan/scripts/panan_bridge.py \
>   --sumocfg panan/config/panan.sumocfg --config configs/default.yaml \
>   --controller adaptive --end-time 3600 --csv panan/results/metrics.csv
> ```

## 相位分组与指标口径

- **相位分组**：按 `configs/default.yaml` 模式A（`minor_only`）——`P_N/P_E/P_S/P_W`，每向「直行+左转」合并为一个相位，冲突矩阵为「任意两相位互斥」。三岔路口自动降为 3 相位（去掉缺失方向）。
- **平均车均延误** = SUMO `timeLoss`（实际行程 − 自由流行程），按到达车辆平均。
- **平均排队长度** = 信号灯进口道上「停车（速度<0.1m/s）」车辆数的时间平均。
- **通行能力** = 实测每小时到达（驶离路网）车辆数 veh/h。
- **冲突点数量** = 仿真期间实际碰撞车辆数（`getCollidingVehiclesNumber` 累计）；为 0 表示相位分组无同时放行冲突流向。

## 路网：真实磐安县 OSM（已就位，保留全部道路）

当前 `panan/network/panan.net.xml` 是**真实磐安县 OSM 路网**（bbox 120.417~120.482°E / 29.017~29.072°N，
约 6.4km×6.2km，**302 路口 / 769 边**，含全部道路类型），其中 **25 个主要交叉口设信号灯**并由决策层控制。

> 命令行 Overpass 在本机被拦截（overpass-api.de 406、镜像 500、OSM API 超时），
> 真实数据是改用 **`osmWebWizard.py`**（浏览器请求路径）下载后，由 `netconvert` 重建的。

> ✂️ 如需删减道路（如去除辅助道路），推荐用 **netedit 手动删**（可视化、可控）：
> `netedit panan/network/panan.net.xml` → 选中边 → Delete → Ctrl+S。

### 重新获取/更新真实路网

```bash
# 方式 A（推荐）：osmWebWizard（浏览器能通就行）
python3 $SUMO_HOME/tools/osmWebWizard.py
#   在网页地图上框选磐安县 → Generate scenario
#   产物目录（如 /home/ubuntu/<时间戳>/）含 osm_bbox.osm.xml.gz / osm.net.xml.gz

# 方式 B：命令行（本机 Overpass 未被拦截时）
bash panan/scripts/fetch_and_build.sh
```

拿到 `osm_bbox.osm.xml` 后，直接一条命令重建（`build_net.sh` 内部自动完成下面全部步骤）：

```bash
bash panan/scripts/build_net.sh   # 去辅助道路 + 选干净交叉口(间距≥200m) + 生成需求
```

`build_net.sh` 做的事：
1. `netconvert --remove-edges.by-type highway.residential,highway.living_street,highway.unclassified --remove-edges.isolated` —— **去除辅助道路**；
2. 用 `sumolib` 选「驶入边 ≥3 且所有进口道 ≥30m 且**路口间距 ≥200m**」的干净路口（按重要性取前 25）设信号灯；
3. `randomTrips --period 2.5` + `duarouter` 生成需求。

> ⚠️ **两个必须避开的坑**：
> 1. **OSM 复杂路口簇**（相距 0.2m）：独立控灯会连接段死锁（实测 300s）。
> 2. **决策层无路口间协调**：若给相距 <200m 的连续路口都设信号灯，上游绿灯会塞满下游短路段 → 溢流死锁（实测 949s）。
>    所以 `build_net.sh` 里加了「信号灯间距 ≥200m」约束。

## 关键设计

- **通用路口**：`panan_bridge.py` 按每个信号灯路口的实际来向（朝向角聚类 N/E/S/W）**动态生成相位**
  （三岔 3 相位、四岔 4 相位），复用逻辑层 `Scorer/PhaseStateMachine/QueueEstimator/...`，
  **不修改逻辑层代码**。
- **只控信号灯路口**：OSM/net 中 `traffic_light` 节点才交给决策层，其余保持 SUMO 默认（优先/环岛）。
- **指标**：理论最大承载车流（车道数×1800 veh/h）、实际通过车流、平均通过路口时间（停止线 60m 检测区）、
  平均等待时间。

## 演示定制（给甲方演示）

- **奉庆线车流注入**：`gen_fengqing_flow.py` 生成 500 辆常规奉庆线流量（终点随机）。
- **奉庆线 → E10 专项车流**：再生成 400 辆从奉庆线穿过路口、往 **E10/月山路方向**（`-E10.29`）的车辆，
  两类车流都在 0-3600s 均匀分布、互相错开。
- **整体车流**：`randomTrips --period 1.2`（约 3000 车次/小时），比之前 1.5 更密。
- **起步延时**：`config/vtypes.add.xml` 覆盖默认车辆参数（`accel=1.2 / sigma=1.0 / tau=1.5`），
  绿灯后车辆逐辆缓慢起步。
- **时间流速**：`run_demo.sh` 默认 `--delay-ms 250`（约 4 倍速），比原来 100ms 慢，更适合演示。
- **需求排序**：`merge_trips.py` 合并多个 trip 文件并按 `depart` 排序（否则乱序车辆会被 SUMO 丢弃）。

## 一键运行仿真验证

```bash
cd /home/ubuntu/zhengyuan
bash panan/scripts/run_demo.sh          # 实时窗口演示（delay 250ms，更接近现实）
```

