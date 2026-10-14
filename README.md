# zhengyuan —— 智能交通信号控制系统 · 逻辑决策层

> 仓库地址：<https://github.com/cz-041017/zhengyuan>
> 项目名（pyproject）：`smart-traffic-control`　版本：`0.1.0`
> 语言 / 版本：Python ≥ 3.11（uv 管理依赖）
> 状态：**逻辑决策层已完整实现**（评分 / 相位分组 / 状态机 / 降级 / 反馈 / 时序分析），100 个单元测试通过
>
> 文档索引：算法设计 [`docs/decision-algorithm-design.md`](docs/decision-algorithm-design.md)　·　感知层接口 [`docs/perception-interface.md`](docs/perception-interface.md)　·　可调参数 [`docs/parameter-reference.md`](docs/parameter-reference.md)　·　调试与复现 [`docs/debugging-guide.md`](docs/debugging-guide.md)

---

## 一、项目是什么

本项目是**智能交通信号控制系统（Intelligent Traffic Signal Control, ITSC）的逻辑决策层（Logic Decision Layer）**。

它接收感知层（YOLO11 视觉识别 + 边缘计算）的车辆检测结果，经过**数据校验 → 状态估计 → 安全约束与优先级仲裁 → 相位选择与配时控制**，输出**「建议的下一相位」`PhaseDecision`**，由控制接口层转发给执行层（PLC / 信号机 / 仿真器）。

**一句话**：把「路口各方向排队了多少车、等了多久、下游堵不堵」变成「下一个该放哪个相位、为什么」。

> **分层边界**：决策层只输出**建议**，**不直接生成灯色指令**；真实灯色由信号机执行，并须回传可信反馈后才更新决策层内部状态。

---

## 二、已实现的功能

### 2.1 相位建模与分组

- **相位分组**（[`phasing.py`](src/traffic_control/phasing.py)）：按路口类型自动推导相位组
  - **模式 A（全支路）**：每向「直行 + 左转」合并 → 4 组 `P_N / P_E / P_S / P_W`；
  - **模式 B（有主路）**：主路双向直行 = 绿波组 `P_MT`，主路双向左转 = `P_ML`，两条支路各一组。
- **规范流向 ID**：8 个（`north_through` / `north_left` / … / `west_left`）。
- **冲突矩阵**：从审核配置加载，校验**对称性 / 无自冲突 / 无未知相位**，绝不从运动重叠推断。

### 2.2 决策评分（P0–P3，语义优先级）

| 优先级 | 语义 | 实现方式 |
|---|---|---|
| **P0** | 单方向等待 ≤ 120s | **硬抢占**：`W ≥ D − (剩余最小绿 + 黄灯 + 全红)` 时无条件调度，绕过评分与倒数 |
| **P1** | 下游溢流保护 | **硬排除**下游拥堵方向 + 滞回解除（`o_high` / `o_low`）；未知数据按拥堵处理 |
| **P2** | 主路绿波优先 | 主路直行在绿波窗口内 `+2.0`（可被 P0 覆盖） |
| **P3** | 需求评分 | `0.6·车数归一 + 0.4·等待归一`，∈[0,1] |

> 关键设计：**P0/P1 是硬约束，不是「把分数调大」**——否则一个过大的绿波权重就能淹没等待时限，造成红灯超时。

### 2.3 相位状态机与配时

- **最小绿**：硬下限，未满足**禁止切换**（含 P0 抢占也须等完）；
- **最大绿**：硬上限，到达即**强制切换**；
- **区间内**：候选分数 `> 当前 + 滞回` 且持续去抖 → 触发 **15s 倒数**；
- **15s 倒数**：仅用于**分数驱动的可选切换**，倒数期间每 tick 复核、可取消；**P0 抢占与最大绿跳过倒数直接切换**（否则会突破 120s 或最大绿）。

### 2.4 安全与降级

- **安全守卫**（[`safety.py`](src/traffic_control/safety.py)）：最小绿、冲突、模式、未知相位检查；
- **P0–P1 冲突上报**：截止方向又下游拥堵 → 判 `p0_p1_conflict`（`feasible=False`）上报仲裁，**不伪造高分强行放行**；
- **数据新鲜度与故障降级**（[`degradation.py`](src/traffic_control/degradation.py)）：
  - `NORMAL` 全新鲜 → 自适应；
  - `PARTIAL` 部分过期 → 保守等待（自上次观测起算），仍经 P0 被服务；
  - `FAILSAFE` 无新鲜数据 → 固定配时 / 黄闪，**绝不把「无数据」当作「没车」**；
- **执行反馈（E 状态）**（[`feedback.py`](src/traffic_control/feedback.py)）：切换后须在确认超时内收到目标相位，**超时 / 控制器故障 / 相位错配 → 判执行失败**进故障流程。

### 2.5 验证与工程化

- **最坏情况时序分析**（[`timing_analysis.py`](src/traffic_control/timing_analysis.py)）：验证 120s 目标的**结构性可行性**（切换开销 vs 截止）+ 无抢占最坏等待 + preempt 负载标记；
- **配置驱动**：全部参数集中在 YAML，加载时强校验（含部署模式：必须有审核相位与配时）；
- **端到端装配**（[`runtime.py`](src/traffic_control/runtime.py)）：`build_runtime(config)` 一条调用产出 `Scorer` + `SpillbackMonitor` + `DegradationController` + `FeedbackMonitor` + `PhaseStateMachine`；
- **感知层接口边界**（[`perception.py`](src/traffic_control/perception.py)）：类型化输入 + 非法车道 ID 快速失败；
- **100 个单元测试**覆盖上述全部行为。

---

## 三、模块一览

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

---

## 四、系统总体架构（四层）

本仓库属于其中的**逻辑决策层**：

```
┌─────────────────────────────────────────────────────────────┐
│  感知层 Perception                                            │
│  · YOLO11 目标识别（停止线前车辆数、车道归属、时间戳、速度）      │
│  · 接口见 docs/perception-interface.md                        │
└───────────────────────────┬─────────────────────────────────┘
                            │ VehicleObservation / DownstreamReport
┌───────────────────────────▼─────────────────────────────────┐
│  逻辑决策层 Logic Decision  ◀──── 本项目                       │
│  · 排队估计 QueueEstimator      · 评分 Scorer（P0–P3）          │
│  · 相位分组/冲突矩阵 Phasing     · 状态机 PhaseStateMachine      │
│  · 降级 DegradationController   · 反馈 FeedbackMonitor          │
│  · 输出 PhaseDecision（仅建议，非灯色指令）                      │
└───────────────────────────┬─────────────────────────────────┘
                            │ 相位建议 PhaseDecision
┌───────────────────────────▼─────────────────────────────────┐
│  控制接口层 Control Interface（尚未实现）                       │
│  · 与信号控制机 / 上位机通信（GB/T 20999 等）                    │
└───────────────────────────┬─────────────────────────────────┘
                            │ 控制指令 / 执行反馈
┌───────────────────────────▼─────────────────────────────────┐
│  执行层 Execution（尚未实现）                                   │
│  · PLC / 交通信号仿真器                                        │
└─────────────────────────────────────────────────────────────┘
```

---

## 五、目录结构

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
│   └── runtime.py                   # 配置 → 运行时装配
├── tests/                           # 10 个测试文件 / 100 个用例
├── .gitignore
├── .python-version                  # 3.11
├── pyproject.toml                   # 依赖：pyyaml；dev：pytest
├── uv.lock                          # 依赖锁定
└── README.md
```

---

## 六、快速开始（复现）

前置：已安装 [uv](https://docs.astral.sh/uv/)（uv 会自动装 Python 3.11 与依赖）。

```bash
git clone https://github.com/cz-041017/zhengyuan.git
cd zhengyuan
uv sync                                       # 按 uv.lock 创建 .venv 并安装依赖
uv run pytest                                 # 期望：100 passed
uv run smart-traffic-control                  # 入口自检：打印 decision engine ready
```

跑一次决策（伪代码，完整示例见[调试手册](docs/debugging-guide.md)）：

```python
from traffic_control.config_loader import load_config
from traffic_control.runtime import build_runtime
from traffic_control.models import ControllerMode, ControllerState
from traffic_control.state_machine import PhaseDemand

runtime = build_runtime(load_config("configs/simulation.yaml"))
state = ControllerState(mode=ControllerMode.NORMAL, current_phase_id="P_MT", phase_started_at=0.0)
demands = {
    "P_MT": PhaseDemand(green_wave_active=True),
    "P_W":  PhaseDemand(vehicle_count=6, oldest_wait_s=118.0),   # 接近 120s
}
print(runtime.state_machine.tick(state, 12.0, demands).selected_phase_id)   # -> P_W（P0 抢占）
```

> 环境准备、复现各类行为、常见故障排查，见 **[docs/debugging-guide.md](docs/debugging-guide.md)**。

---

## 七、核心设计原则

1. **score 无灯色权限**：评分只在**已通过安全约束筛选的可行相位集合 `F(t)`** 内排序，不能单独决定改灯。
2. **先安全后优化**：冲突互锁、切换时序、信号机允许执行、下游条件满足是前置硬约束；评分只在 `F(t)` 内进行。
3. **配时参数必须可追溯**：最小绿/最大绿/黄灯/全红/15s 倒数必须来自**经审核的路口配置**，不在运行时临时生成，也不把未经核验的配时称为国标规定值。
4. **执行反馈是状态权威**：无反馈不更新相位；执行失败进故障流程。
5. **数据缺失 ≠ 空 / 畅通**：观测超时或下游无返回按「未知」处理并降级，绝不当作「没车」或「畅通」。

---

## 八、涉及标准（接口与验收依据）

正式开发前须取得适用版本全文，并由交通信号控制专业人员确认具体条款、冲突相位表、黄灯与全红时间、故障行为与信号机接口。

| 标准 | 内容与关注点 | 状态备注 |
|---|---|---|
| **GB 25280-2016** | 信号控制机的安全、控制功能与设备要求（强制性国标） | 强制标准 |
| **GB/T 20999-2017** | 信号控制机与上位机之间的数据通信协议 | 现行 |
| **GA/T 508-2014** | 道路交通信号倒计时显示器相关要求 | 现行 |
| **T/BJITS 01-2025** | 边缘计算单元与信号控制机之间的通信要求 | 待核验全文 |
| **GA/T 1049.2-2024** | 公安交通集成指挥平台与交通信号控制系统的通信协议 | 已实施 |
| **T/CITSA 36-2023** | 路侧边缘计算单元与交通信号控制机的数据通信规范 | 参考接口 |

> 本项目不会把未经全文核验的标准条款或自拟配时参数说成「国家标准规定值」。

---

## 九、当前状态与路线图

### 已实现

- [x] 核心数据模型与枚举（`models.py`）
- [x] YAML 配置加载与强校验，含部署模式约束（`config_loader.py`）
- [x] 逐车道排队需求估计（`queue_estimator.py`）
- [x] 仿真级安全守卫（`safety.py`）
- [x] 相位分组与冲突矩阵（`phasing.py`）
- [x] P0–P3 评分引擎与下游溢流滞回（`scoring.py`）
- [x] 相位状态机：最小/最大绿、P0 硬抢占、15s 倒数（`state_machine.py`）
- [x] 数据新鲜度与故障降级（`degradation.py`）
- [x] 执行反馈与故障回退（`feedback.py`）
- [x] 最坏情况时序分析（`timing_analysis.py`）
- [x] 感知层接口边界与文档（`perception.py`）
- [x] 配置 → 运行时端到端装配（`runtime.py`）
- [x] 100 个单元测试 + CLI 入口

### 尚未实现（下一阶段）

- [ ] **仿真台**：控制器执行模型（按时间推进相位/清空）+ 车流到达模型 + 主循环与指标统计
- [ ] 控制接口层对接（GB/T 20999 等通信协议）
- [ ] PLC / 交通信号仿真器执行层接入

---

## 十、重要安全声明

> 本项目是**研究 / 原型阶段的决策层软件**，**不是经认证的信号控制机**，不能替代当地/司法辖区的具体要求。
>
> - 代码中所有 score 初值、权重、配时参数均为**待标定值**，**未经仿真标定、最坏情况验证与真机联调前，不得部署到真实路口**；
> - 真正的冲突判断、相位转换时序、允许的控制方式，必须来自**已核验的路口配置与信号机规则**；
> - 涉及交通安全的改动须由交通信号控制专业人员评审。
