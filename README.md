# 正源智能交通信号控制系统 · 逻辑决策层

> 仓库地址：<https://github.com/cz-041017/zhengyuan>
> 项目名（pyproject）：`smart-traffic-control`　版本：`0.1.0`
> 语言 / 版本：Python ≥ 3.11（用 uv 管理依赖）
> 状态：**逻辑决策层已完整实现**（评分 / 相位分组 / 状态机 / 降级 / 反馈 / 时序分析），143 个单元测试全部通过
>
> 文档索引：算法设计 [`docs/decision-algorithm-design.md`](docs/decision-algorithm-design.md)　·　感知层接口 [`docs/perception-interface.md`](docs/perception-interface.md)　·　可调参数 [`docs/parameter-reference.md`](docs/parameter-reference.md)　·　调试与复现 [`docs/debugging-guide.md`](docs/debugging-guide.md)

---

## 一、用一句话说清楚这个项目

**给路口的红绿灯装一个"会看、会想、会做决定"的大脑。**

传统红绿灯是按固定时间表轮换的（比如"南北绿 30 秒，然后东西绿 30 秒"），不管实际有没有车。
本项目做的是**"按需放行"**：它盯着每个方向排了多少车、等了多久、前面路口堵不堵，
然后**算出"下一轮该先放哪个方向、放多久"**，让等待最久的车、排得最多的方向优先走。

**它能回答的典型问题：**

- 现在哪个方向车最多？→ 先放它。
- 有没有哪个方向已经等了很久（比如超过 2 分钟）？→ 必须优先照顾它，不能再拖。
- 前面那个路口是不是已经堵死了？→ 如果堵了，就先别往那边放车，免得堵上加堵。

> 一句话总结：**把"路口各方向排了多少车、等了多久、下游堵不堵"变成"下一个该放哪个相位、为什么"。**

---

## 二、这个"大脑"在整套系统里处在什么位置

一个完整的智能路口系统通常分为四层，**本项目只做其中的"逻辑决策层"（第二层）**：

```
┌───────────────────────────────────────────────────────────┐
│ ① 感知层（Perception）—— 眼睛                              │
│   用摄像头 + AI（YOLO11）看：每个方向有几辆车、等了多久、      │
│   下游路口堵不堵。把这些"看到的东西"交给决策层。              │
└──────────────────────────┬────────────────────────────────┘
                           │ 车辆数 / 等待时间 / 下游占用率
┌──────────────────────────▼────────────────────────────────┐
│ ② 逻辑决策层（Logic Decision）—— 大脑   ◀──── 本项目         │
│   拿到"眼睛"看到的信息，经过"校验 → 估算 → 安全把关 → 打分   │
│   → 选相位"，输出"建议下一相位"。它只提建议，不直接按灯。     │
└──────────────────────────┬────────────────────────────────┘
                           │ 相位建议 PhaseDecision
┌──────────────────────────▼────────────────────────────────┐
│ ③ 控制接口层（Control Interface）—— 传令兵（尚未实现）      │
│   把"大脑"的建议翻译成信号机能听懂的通信指令（GB/T 20999 等）。│
└──────────────────────────┬────────────────────────────────┘
                           │ 控制指令 / 执行反馈
┌──────────────────────────▼────────────────────────────────┐
│ ④ 执行层（Execution）—— 手（尚未实现）                      │
│   信号机 / PLC / 仿真软件，真正把红灯变绿灯。                │
└───────────────────────────────────────────────────────────┘
```

**为什么要分这么细？** 因为"看"（摄像头）和"按灯"（信号机）是硬件厂商的事，
而"怎么决定放行"是算法的事。把决策单独做成一层，算法可以独立升级、独立测试，
出问题时也容易定位是"没看清"还是"想错了"。

> **重要边界**：决策层只输出**建议**（"我建议下一个放南北方向"），
> **不直接生成灯色指令**。真正亮灯由信号机执行，并且要**回传"已执行"的确认**，
> 决策层才更新自己的内部状态。这样不会出现"算法以为灯变了、其实没变"的危险情况。

---

## 三、先认识几个词（术语白话对照）

| 术语 | 白话解释 |
|---|---|
| **相位（phase）** | 同一时刻允许一起走的一组车流。比如"北向直行+左转"就是一个相位。 |
| **相位切换** | 从"放行 A 组"换成"放行 B 组"（比如从南北放行换到东西放行）。 |
| **抢占（preempt）** | 越过正常排队和打分，直接把绿灯优先给"等得太久"的方向。 |
| **滞回（hysteresis）** | 给换灯门槛留一点余量，避免分数轻微抖动就来回切灯。 |
| **去抖（debounce）** | 连续多次确认后才真正动作，避免一次误判就换灯。 |
| **溢流（spillback）** | 下游路口堵满了，车倒灌回上游路口，越堵越死。 |
| **绿波（green wave）** | 让主路连续几个路口尽量一路绿灯（协调偏好）。 |
| **最小绿 / 最大绿** | 一个相位至少亮多久（保护起步/行人）/ 最多亮多久（防止霸占）。 |

## 四、这个"大脑"由哪几部分组成（各部分的作用）

逻辑决策层不是一个黑盒，而是由多个各司其职的小模块拼起来的。用大白话解释：

### 3.1 数据模型与配置（`models.py` / `config_loader.py`）
- **数据模型**：定义系统里所有"名词"——什么是一辆车、一个车道、一个相位、一条决策。
  相当于先约定好"大家说的'相位'到底指什么"，避免各说各话。
- **配置加载与校验**：**大部分**可调数字（评分权重、等待上限、阈值、降级开关）写在 YAML 文件里；
  但**仿真台的部分配时仍是代码里的占位值**（如 `runtime.py` / `panan_bridge.py` 中的
  `min_green=10 / max_green=60 / yellow=3 / all_red=1`），`configs/*.yaml` 的 `signal.phases` 默认为空。
  加载时会**严格检查**（比如"最小绿不能比最大绿还长"），配错了直接报错，不会带病运行。

### 3.2 排队估计（`queue_estimator.py`）
- **作用**：把"摄像头每帧看到的一堆车辆"整理成"每个车道现在排了多少车、等了多久"。
- **为什么需要**：摄像头可能重复识别、丢帧、同一辆车换车道，需要"去重 + 计时"。

### 3.3 相位分组与冲突矩阵（`phasing.py`）
- **作用**：把"方向"（北、东、南、西）合并成"相位"（同一时间允许一起走的车流组合）。
  默认模式 A 下每个方向单独一组（北直+北左、东、南、西），不同方向互斥；
  只有模式 B 才把"南北直行"合并成绿波组 `P_MT`（同时放行）。
- **冲突矩阵**：一张"谁和谁不能同时放行"的表。它来自**经过审核的路口配置**，
  不是算法自己猜的——因为"能不能一起走"关系到撞车，必须由人确认。

### 3.4 决策评分（`scoring.py`）—— 最核心的"打分器"
给每个候选相位打分，分数高的优先。打分考虑四个**优先级**（P0 最高，P3 最低）：

| 优先级 | 白话解释 | 打分方式 |
|---|---|---|
| **P0 等待保障** | "有车等太久了，必须尽快放" | 从 80s 起分数快速爬升；到截止**抢在评分之前调度**（仍受最小绿约束） |
| **P1 防溢流** | "前面路口堵了，别往那放" | 下游堵的方向**直接排除**，分数扣到 -1000 |
| **P2 绿波优先** | "主路要一路绿灯，优先照顾" | 主路直行 +2 分（**参数已定义并校验，运行时接线尚未完成，见第十节**） |
| **P3 需求评分** | "谁车多、谁等得久，谁先走" | 车数 60% + 等待 40% 加权 |

> **关键设计**：P0 和 P1 走**预定义约束路径**，不是"把分数调大一点"。
> 为什么？因为如果只靠加分，一个特别大的权重就可能盖过 P0。
>
> **诚实说明（重要）**：130s 在当前实现里是**尽力保证（best-effort）**，不是绝对硬保证——
> ① 最小绿是比抢占更优先的硬下限，若某方向在最小绿走完前就到截止，实际等待会略超 130s；
> ② 当"到截止"与"下游拥堵"冲突（P0/P1 冲突）时，实现返回 `feasible=False` 且**不切换**，
> 由上层上报 / 人工仲裁，而不是伪造一个相位强行放行。文档与实现均按此语义表述。

### 3.5 相位状态机（`state_machine.py`）—— 决定"什么时候换灯"
- **最小绿**：一个相位至少亮这么久，没到时间不许切换（防止"刚绿就红"）。
- **最大绿**：一个相位最多亮这么久，到时间强制切换（防止某个方向一直霸占绿灯）。
- **15 秒倒数**：在"可换可不换"的区间里，如果另一个方向分数明显更高，就启动 15 秒倒数，
  倒数期间还会反复复核（万一有更紧急的情况就取消）。**这只是"可选的换灯"用的**；
  P0 强制放行和最大绿到点则**跳过倒数直接换**，否则会超时。

### 3.6 数据新鲜度与故障降级（`degradation.py`）—— 出问题时的"安全垫"
- 摄像头数据可能延迟、中断。**数据过期时，绝不能当成"没车"**（那会很危险）。
- 三种状态：
  - `NORMAL`：数据都新鲜 → 正常自适应；
  - `PARTIAL`：部分数据过期 → 保守处理（按最后一次看到的时间继续计时）；
  - `FAILSAFE`：完全没数据 → 退回固定配时 / 黄闪，保证路口不会乱。

### 3.7 执行反馈（`feedback.py`）—— 确认"交代的事真的做了"
- 决策层说"下一个放南北"，信号机要回一句"收到，已放南北"。
- 如果等太久没回、或者回的是别的相位 → 判定执行失败，进入故障处理。

### 3.8 时序分析（`timing_analysis.py`）—— 提前算"最坏情况会不会超时"
- 在部署前就算一遍：如果所有方向都在排队，最坏要等多久？
- 用来验证"130 秒等待上限"**在结构上是否真的做得到**，而不是靠运气。

### 3.9 感知层接口（`perception.py`）—— 和"眼睛"对接的插座
- 定义感知层该按什么格式传数据（车辆 ID、车道、时间戳、速度）。
- 格式不对（比如车道 ID 写错）会立刻报错，避免错误数据悄悄流进来。

### 3.10 端到端装配（`runtime.py`）—— 把核心零件拼成整机
- 一行 `build_runtime(配置)` 就能装配**核心决策链**：`Scorer` + `SpillbackMonitor` +
  `DegradationController` + `FeedbackMonitor` + `PhaseStateMachine`（+ 相位/评分/状态机配置）。
- 注意：`SafetyGuard`、`QueueEstimator`、`perception` **不在** `build_runtime` 的返回值里，
  它们由仿真台/上层自行构造与调用。

### 3.11 行人保底（`state_machine.py` + `pedestrian.*`）—— 没车时也给行人留一次过街
- 行人绿灯**绑定直行相位**：普通路口按“北→东、东→南、南→西、西→北”的顺时针相邻人行横道；
  绿波主路按“南北直行放行东西两侧、南北左转 4 向全红、东向放北向、西向放南向”。
- 某方向连续红灯达到 `pedestrian.max_red_s`（默认 150s）→ 给一次保底绿灯（默认 12s），
  只放行“该方向直行 + 对应人行横道”，左转照常。
- 优先级：**车辆 P0（130s）> 行人保底（150s）> 普通评分**；且保底不会把任何方向推过 130s。
- 行人黄灯：车辆绿灯最后 `pedestrian.yellow_s`（默认 3s）由接口层驱动，
  逻辑层通过 `LayerResult.active_crosswalks` / `pedestrian_yellow_remaining_s` 输出可用信息。

### 3.12 数据看门狗（`degradation.py` + `telemetry.py`）—— 感知层“失联”就交权
- 1× 超时（默认 3s）无数据 → `PARTIAL`（继续自适应，但等待按保守值算）。
- 2× 超时（默认 6s）无数据 → `FAILSAFE`：逻辑层**主动交权**（`selected_phase_id=None,
  reason="failsafe_hold"`），由信号机切固定配时/黄闪。
- 告警通过 `TelemetrySink` 抽象接口上报（默认只写本地日志，**不接网络**；
  后期由数据接口层转发上云）。

### 3.13 指挥中心接管（`DecisionLayer.takeover/release`）—— 人接管时逻辑层闭嘴
- 单路口接管：`layer.takeover(now)` 后该路口**完全静默**（`reason="manual_override"`），
  其他路口不受影响。
- 解除接管：`layer.release(now)` → **内存清零**（不重启进程）+ 走国标启动序列
  （5s 黄闪 + 10s 红灯），序列结束后恢复正常决策。
- 接管/解除指令协议由交警部门制定；逻辑层只认 `TAKEOVER(junction_id)` / `RELEASE(junction_id)` 两个信号。

---

## 五、模块一览（速查表）

| 模块 | 职责 | 主要类型 / 函数 |
|---|---|---|
| [`models.py`](src/traffic_control/models.py) | 核心数据模型与枚举 | `SignalColor`、`ControllerMode`、`VehicleObservation`、`LaneState`、`SignalPhase`、`ControllerState`、`PhaseDecision` |
| [`config_loader.py`](src/traffic_control/config_loader.py) | YAML 配置加载与强校验 | `load_config`、`validate_config`、`ConfigError` |
| [`queue_estimator.py`](src/traffic_control/queue_estimator.py) | 逐车道排队需求估计 | `QueueEstimator` |
| [`safety.py`](src/traffic_control/safety.py) | 仿真级安全守卫 | `SafetyGuard`、`SafetyCheck` |
| [`phasing.py`](src/traffic_control/phasing.py) | 相位分组与冲突矩阵 | `PhaseGroup`、`phase_groups`、`ConflictMatrix` |
| [`scoring.py`](src/traffic_control/scoring.py) | P0–P3 评分与溢流滞回 | `ScoreConfig`、`Scorer`、`SpillbackMonitor`、`aggregate_demand` |
| [`state_machine.py`](src/traffic_control/state_machine.py) | 相位状态机（最小/最大绿、P0 抢占、15s 倒数） | `PhaseStateMachine`、`MachineConfig`、`PhaseDemand`、`MachineStage` |
| [`degradation.py`](src/traffic_control/degradation.py) | 数据新鲜度与故障降级 | `DataFreshness`、`DegradationController`、`DegradationMode`、`conservative_oldest_wait` |
| [`feedback.py`](src/traffic_control/feedback.py) | 执行反馈与故障回退（E 状态） | `FeedbackMonitor`、`FeedbackStatus` |
| [`timing_analysis.py`](src/traffic_control/timing_analysis.py) | 最坏情况时序分析 | `analyze_timing`、`TimingAnalysis` |
| [`perception.py`](src/traffic_control/perception.py) | 感知层输入边界 | `RawDetection`、`DownstreamReport`、`PerceptionFrame`、`normalize_detections`、`downstream_occupancy` |
| [`runtime.py`](src/traffic_control/runtime.py) | 配置 → 运行时端到端装配 | `build_runtime`、`RuntimeObjects`、`build_phases` 等 |
| [`decision_layer.py`](src/traffic_control/decision_layer.py) | **决策层门面**：一步串起排队→降级→溢流→状态机→反馈，并汇总全部可调参数 | `DecisionLayer`、`TunableParameters`、`LayerResult` |
| [`telemetry.py`](src/traffic_control/telemetry.py) | 告警/日志输出**抽象接口**（只定义边界，不接网络，后期由数据接口层转发上云） | `TelemetrySink`、`NullTelemetrySink`、`AlarmEvent` |
| [`startup.py`](src/traffic_control/startup.py) | 国标启动序列：5s 黄闪 + 10s 全红 | `StartupSequence`、`StartupStage` |

---

## 六、目录结构

```
zhengyuan/
├── configs/
│   ├── default.yaml                 # 模式 A（全支路）示例配置
│   └── simulation.yaml              # 模式 B（主路 NS + 绿波）示例配置
├── docs/
│   ├── decision-algorithm-design.md # 完整算法设计与安全基线（权威）
│   ├── perception-interface.md      # 感知层（YOLO11）接入接口文档
│   ├── parameter-reference.md       # 可调参数手册（调参）
│   └── debugging-guide.md           # 调试与复现手册
├── panan/                            # 磐安县真实路网仿真台（演示用）
│   ├── network/                     # 路网文件（panan.net.xml）
│   ├── config/                      # 仿真配置（panan.sumocfg / vtypes.add.xml）
│   ├── demand/                      # 需求（车辆行程）文件
│   ├── scripts/                     # 仿真脚本（桥接、可视化、需求生成等）
│   └── results/                     # 仿真结果（metrics.csv / demo.gif）
├── src/traffic_control/
│   ├── __init__.py                  # CLI 占位入口 main()
│   ├── models.py                    # 数据模型与枚举
│   ├── config_loader.py             # 配置加载与校验
│   ├── queue_estimator.py           # 逐车道排队估计
│   ├── safety.py                    # 安全守卫
│   ├── phasing.py                   # 相位分组与冲突矩阵
│   ├── scoring.py                   # P0–P3 评分
│   ├── state_machine.py             # 相位状态机
│   ├── degradation.py               # 数据新鲜度与故障降级
│   ├── feedback.py                  # 执行反馈（E 状态）
│   ├── timing_analysis.py           # 最坏情况时序分析
│   ├── perception.py                # 感知层接口边界
│   ├── runtime.py                   # 配置 → 运行时装配
│   ├── decision_layer.py            # 决策层门面（一键调用 + 参数汇总）
│   ├── telemetry.py                 # 告警/日志输出接口（不接网络）
│   └── startup.py                   # 国标 5s 黄闪 + 10s 红灯启动序列
├── tests/                           # 15 个测试文件 / 154 个用例
├── .gitignore
├── .python-version                  # 3.11
├── pyproject.toml                   # 依赖：pyyaml；dev：pytest
├── uv.lock                          # 依赖锁定
├── AGENTS.md                        # 协作代理 / 同事的最小工作规程
└── README.md
```

---

## 七、快速开始（5 分钟跑起来）

前置：已安装 [uv](https://docs.astral.sh/uv/)（uv 会自动装 Python 3.11 和依赖）。

```bash
git clone https://github.com/cz-041017/zhengyuan.git
cd zhengyuan
uv sync                                       # 按 uv.lock 创建虚拟环境并安装依赖
uv run pytest                                 # 期望：154 passed（全部测试通过）
uv run smart-traffic-control                  # 入口自检：打印 decision engine ready
```

**用代码跑一次决策**（看它怎么"做决定"）：

```python
from traffic_control.config_loader import load_config
from traffic_control.runtime import build_runtime
from traffic_control.models import ControllerMode, ControllerState
from traffic_control.state_machine import PhaseDemand

# 1) 加载配置并装配决策层
runtime = build_runtime(load_config("configs/simulation.yaml"))

# 2) 模拟当前状态：现在是主路直行相位 P_MT
state = ControllerState(mode=ControllerMode.NORMAL, current_phase_id="P_MT", phase_started_at=0.0)

# 3) 各方向的需求：西向已经等了 118 秒（接近 130s 上限！）
demands = {
    "P_MT": PhaseDemand(green_wave_active=True),
    "P_W":  PhaseDemand(vehicle_count=6, oldest_wait_s=118.0),
}

# 4) 让决策层"想一下"
decision = runtime.state_machine.tick(state, 12.0, demands)
print(decision.selected_phase_id)   # -> P_W（因为 P0 等待保障，触发截止抢占；仍受最小绿约束）
```

> 想跑真实路网的可视化仿真（磐安县 43 个信号灯路口）？见 **[docs/debugging-guide.md](docs/debugging-guide.md)** 的"一键开启仿真"章节。

---

## 八、核心设计原则（为什么这么设计）

1. **分数不能直接改灯**：评分只在"已经通过安全筛选的候选相位"里排序，不能单凭分数把灯改掉。
2. **先安全、后优化**：冲突互锁、切换时序、信号机能否执行、下游是否堵——这些是"硬门槛"，
   通过了才轮到比分数。
3. **配时参数必须可追溯**：绿灯多久、黄灯多久，必须来自**经过审核的路口配置**，
   不能运行时临时拍脑袋生成，更不能把没核验过的数字说成"国标"。
4. **执行反馈说了算**：没收到信号机回执，就不更新状态；执行失败就进故障流程。
5. **数据缺失 ≠ 没车**：观测超时或下游没回数据，按"未知"处理并降级，
   **绝不能当成"没车"或"畅通"**——这是最容易出事的地方。

---

## 九、涉及标准（接口与验收依据）

正式开发前须取得适用版本全文，并由交通信号控制专业人员确认具体条款、冲突相位表、黄灯与全红时间、故障行为与信号机接口。

| 标准 | 内容与关注点 | 状态备注 |
|---|---|---|
| **GB 25280-2016** | 信号控制机的安全、控制功能与设备要求（强制性国标） | 强制标准 |
| **GB/T 20999-2017** | 信号控制机与上位机之间的数据通信协议 | 现行 |
| **GA/T 508-2014** | 道路交通信号倒计时显示器相关要求 | 现行 |
| **T/BJITS 01-2025** | 边缘计算单元与信号控制机之间的通信要求 | 待核验全文 |
| **GA/T 1049.2-2024** | 公安交通集成指挥平台与交通信号控制系统的通信协议 | 已实施 |
| **T/CITSA 36-2023** | 路侧边缘计算单元与交通信号控制机的数据通信规范 | 参考接口 |

> 本项目不会把未经全文核验的标准条款或自拟配时参数说成"国家标准规定值"。

---

## 十、当前状态与路线图

### 已实现

- [x] 核心数据模型与枚举（`models.py`）
- [x] YAML 配置加载与强校验，含部署模式约束（`config_loader.py`）
- [x] 逐车道排队需求估计（`queue_estimator.py`）
- [x] 仿真级安全守卫（`safety.py`）
- [x] 相位分组与冲突矩阵（`phasing.py`）
- [x] P0–P3 评分引擎与下游溢流滞回（`scoring.py`）
- [x] 相位状态机：最小/最大绿、P0 截止抢占（最小绿优先）、15s 倒数（`state_machine.py`）
- [x] 数据新鲜度与故障降级（`degradation.py`）
- [x] 执行反馈与故障回退（`feedback.py`）
- [x] 最坏情况时序分析（`timing_analysis.py`）
- [x] 感知层接口边界与文档（`perception.py`）
- [x] 配置 → 运行时端到端装配（`runtime.py`）
- [x] 143 个单元测试 + CLI 入口
- [x] 磐安县真实路网仿真台（`panan/`，可视化演示 + 指标统计）

### 部分实现 / 未接线（重要，避免误读）

- [ ] **P2 绿波**：参数与配置校验已实现（`green_wave.*`），但**运行时窗口计算尚未接线**，
  仿真台硬编码 `green_wave_active=False`；因此 P2 当前**不生效**。
- [x] **降级 / 执行反馈（决策层已接线）**：`DecisionLayer` 门面（`decision_layer.py`）已把
  排队估计 → 数据新鲜度/降级 → 保守等待 → 溢流滞回 → 状态机 → 执行反馈串成一条链，
  `layer.tick()` 返回 `result.degradation` / `result.feedback`。
- [ ] **仿真台的降级/反馈接线**：`panan_bridge.py` 仍直接调 `state_machine.tick()`，
  **未走 `DecisionLayer` 门面**，因此仿真中降级/反馈不生效（属仿真台接线缺口，非决策层缺口）。
- [ ] **`safety.*` 三个开关**：`config_loader.py` 只校验其布尔类型，**全库无消费者**，当前不产生实际约束力。
- [ ] **信号配时从配置读取**：仿真台相位配时仍用代码占位值（`runtime.py` / `panan_bridge.py`），
  未读取 `configs/*.yaml` 的 `signal.phases`（默认为空）。

### 尚未实现（下一阶段）

- [ ] 控制接口层对接（GB/T 20999 等通信协议）
- [ ] PLC / 交通信号仿真器执行层接入
- [ ] 真机标定与最坏情况验证

---

## 十一、重要安全声明

> 本项目是**研究 / 原型阶段的决策层软件**，**不是经认证的信号控制机**，不能替代当地/司法辖区的具体要求。
>
> - 代码中所有 score 初值、权重、配时参数均为**待标定值**，
>   **未经仿真标定、最坏情况验证与真机联调前，不得部署到真实路口**；
> - 真正的冲突判断、相位转换时序、允许的控制方式，必须来自**已核验的路口配置与信号机规则**；
> - 涉及交通安全的改动须由交通信号控制专业人员评审。
