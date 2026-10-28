# 决策层可调参数参考（调参手册）

> 代码里一次取全：`DecisionLayer.from_yaml(path).parameters` 返回 `TunableParameters`
> （含 `max_wait_target_s`、`observation_timeout_s`、`score.*` 等分组字段）。

> 全部初值均为**待标定值**，须经仿真 + 真机标定后方可部署；**任何改动不得破坏优先级相对次序 P0 > P1 > P2 > P3**。
> 参数集中在 [`configs/*.yaml`](../configs/) 与 [`config_loader.py`](../src/traffic_control/config_loader.py) 的校验里。
> 仿真台（磐安县路网演示）的参数在第 9 节。

---

## 1. 项目元信息

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `project.name` | `smart-traffic-control` | 项目名（仅标识） | 一般不动 |
| `project.config_version` | 1 | 配置格式版本 | 格式变更时递增 |

---

## 2. 运行模式与数据链路（`runtime`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `runtime.mode` | `simulation` | 运行模式：`simulation` / `deployment` | **部署必须用 `deployment`**；该模式强制要求审核相位与配时，仿真可留空 |
| `runtime.observation_timeout_s` | 3.0 | 方向级数据新鲜度超时（s） | 须 > 感知上报周期；超时即降级 |
| `runtime.confirm_timeout_s` | 10.0 | 切换执行确认超时（s） | 须 > 黄灯+全红+余量 |
| `simulation.duration_s` / `step_s` / `random_seed` | — | 仿真台预留字段（当前未被任何代码消费） | 改这些**不影响**现有磐安县仿真台（它用自己的 CLI 参数） |

## 3. 排队估计（`queue_estimation`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `queue_estimation.queue_speed_threshold_mps` | 0.5 | 排队速度阈值（m/s） | 按现场怠速/蠕行速度标定 |
| `queue_estimation.vehicle_length_m` | 5.0 | 平均车长（m） | 用于排队长度估算 |
| `queue_estimation.stopped_gap_m` | 2.0 | 停车间距（m） | 同上 |
| `queue_estimation.stale_after_s` | 3.0 | 轨迹过期清理（s） | 略大于上报周期 |

## 4. 评分与优先级（`optimization`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `optimization.max_wait_target_s` | 130 | P0 单方向最长等待（s），硬指标 | 国标/甲方要求，一般不动 |
| `optimization.wait_urgency_start_s` | 80 | S_wait 软预警起点（s） | 越接近 130 越「激进」，太小会过早压过绿波 |
| `optimization.wait_urgency_max_score` | 5.0 | S_wait 峰值（必须 > 绿波 2.0） | 保持 > `green_wave_score`，保证 P0 压过 P2 |
| `optimization.green_wave_score` | 2.0 | P2 绿波加分 | 必须 < `wait_urgency_max_score`，避免绿波淹没 P0 |
| `optimization.demand.vehicle_weight` | 0.6 | P3 车辆数权重 | 与 wait_weight 之和宜 =1 |
| `optimization.demand.wait_weight` | 0.4 | P3 等待权重 | 同上 |
| `optimization.demand.n_max` | 20 | P3 归一化容量（辆） | 按单相位最大可排队长度定，用于把计数压到 [0,1] |
| `optimization.spillback.o_high` | 0.8 | P1 拥堵进入阈值 | 与感知层占用率口径一致 |
| `optimization.spillback.o_low` | 0.5 | P1 拥堵解除阈值（滞回） | 必须 < o_high，防止抖动 |
| `optimization.spillback.penalty` | -1000 | P1 兜底压制分 | 足够大即可，一般不动 |
| `optimization.score_hysteresis` | 0.1 | 切换滞回 | 防分数抖动，太小会频繁倒数 |
| `optimization.switch_debounce_s` | 3.0 | 切换去抖（s） | 越大越稳但越迟钝 |
| `optimization.priorities` | P0→P1→P2→P3 | 语义优先级顺序（勿动） | 顺序即优先级，改动会被 `config_loader` 拒绝 |

## 5. 相位、配时与冲突表（`signal`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `signal.layout.mode` | `minor_only` | 相位分组：全支路 4 组 / 主路绿波 4 组 | 按路口类型选 |
| `signal.layout.main_road_direction` | null | 主路朝向（`NS`/`EW`） | `main_road` 时必填 |
| `signal.conflict_matrix` | 无默认（必填；`default.yaml` 示例为 4 项互斥） | **经审核的相位冲突表**（谁和谁不能同时放行） | 必须与布局相位一致、对称、无自冲突；**不得由算法推断**。留空 `{}` 时 `build_runtime` 会抛错 |
| `signal.phases[].phase_id` | — | 相位 ID | 须与冲突表键一致 |
| `signal.phases[].movement_ids` | — | 该相位包含的规范流向 | 只能是 8 个规范 ID |
| `signal.phases[].min_green_s` | — | 最小绿（s，硬下限） | 需满足行人过街/清空，来自审核 |
| `signal.phases[].max_green_s` | — | 最大绿（s，硬上限） | 过长会让 130s 依赖抢占（见时序分析） |
| `signal.phases[].yellow_s` | — | 黄灯（s） | 来自 GB 25280/审核配置，不臆造 |
| `signal.phases[].all_red_s` | — | 全红（s） | 同上 |

> **硬约束（改错会被 `config_loader` 拒绝或破坏优先级次序）**：
> `spillback.penalty < 0`；`wait_urgency_start_s ∈ [0, max_wait_target_s)`；
> `demand.vehicle_weight + wait_weight > 0`；`green_wave.band_start_s < band_end_s`；
> `green_wave.enabled=true` 时 `main_through_phase` 必填。
>
> 提示：`deployment` 模式要求 `signal.phases` **非空且完整**；`simulation` 模式可留空（构建器用占位配时）。
> 跑 [`timing_analysis.py`](../src/traffic_control/timing_analysis.py) 看「无抢占最坏等待」，若远超 130s 说明 max 绿配得太长。

## 6. 绿波（`green_wave`，P2）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `green_wave.enabled` | false | 是否启用绿波 | 有主路协调时开启 |
| `green_wave.main_through_phase` | null | 主路直行相位 ID | 通常 `P_MT` |
| `green_wave.band_start_s` / `band_end_s` | 0 / 45 | 绿波窗口（相位周期内起止） | 按上游路口协调相位对齐 |
| `green_wave.period_s` | 120 | 绿波周期（s） | 与信号周期一致 |

> **实现状态（重要）**：这 5 个绿波参数**只被 `config_loader.py` 校验，运行时从未读取**
> （`runtime.build_runtime` 不消费、仿真台硬编码 `green_wave_active=False`）。
> 因此当前改这些参数**不会让绿波生效**，P2 绿波尚未接入。

## 7. 倒数（`countdown`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `countdown.display_duration_s` | 15 | 15s 切换倒数（s） | 参考 GA/T 508 倒计时显示要求；**具体数值需核验标准全文**，不冒充国标规定值 |

## 7.5 行人保底（`pedestrian`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `pedestrian.max_red_s` | 150 | 某方向连续红灯上限（s），到点给一次保底绿灯 | 必须 **> `optimization.max_wait_target_s`**（加载期强制），建议 150 |
| `pedestrian.min_green_s` | 12 | 行人绿灯**可接受下限**（s）；实际值由下方物理参数推导，低于它时才用它兜底 | 一般不用改，改物理参数即可 |
| `pedestrian.crosswalk_length_m` | 15 | 人行横道长度（m） | **按实际路口标定**；越长行人绿灯越长 |
| `pedestrian.walking_speed_mps` | 1.2 | 行人步行速度（m/s） | 老人/儿童多取 0.8~1.0 |
| `pedestrian.clearance_s` | 3 | 行人清空时间（s） | 横道越长清空越久，通常 3~6s |
| `pedestrian.yellow_s` | 3 | 行人黄灯（车辆绿灯最后 N 秒，禁止新行人） | 由接口层驱动；逻辑层经 `LayerResult.pedestrian_yellow_remaining_s` 输出 |

> **当前实现（项目决定）**：逻辑层与仿真台的**默认最小绿 = 12s**。
> `crosswalk_length_m / walking_speed_mps / clearance_s` 保留为**标定参考字段**，
> 当前不参与自动推导（仿真对比显示 12s 在延误与排队之间较平衡）。
> 若以后要按实际横道长度自动推导，可在 `runtime.build_machine_config` 里恢复
> `max(min_green_s, 长度/速度 + 清空)`。
| `runtime.data_gap_s` | 6 | 数据中断判定（s），超过则重建行人红灯基线 | 应与 `2×observation_timeout_s` 一致；tick 周期更慢时需调大 |

**实现边界（重要）**：
- 本段**可选**（缺省用默认值 150/12/3），旧配置（如 `panan/config/panan.yaml`）无需改。
- 行人绿灯**绑定直行相位**，映射为：
  - 普通路口（`minor_only`）：北→东、东→南、南→西、西→北（顺时针相邻人行横道）；
  - 绿波（`main_road`）：南北直行→东西两侧、南北左转→4 向全红、东向→北向、西向→南向。
    两套规则是**项目定义**，不统一；`EW` 绿波为 `P_N→W`、`P_S→E`。
- 保底**不会**把任何方向推过 `max_wait_target_s`：若保底会让某方向超时，则否决该次保底。
- **行人黄灯的分工（约定）**：
  - 逻辑层 `LayerResult.pedestrian_yellow_remaining_s` 只给**最小绿窗口**的锚
    （相位最小绿的最后 `pedestrian.yellow_s` 秒），因为逻辑层不知道实际绿灯何时结束；
  - **接口层**拿到该字段后，必须按**实际正在执行的剩余绿灯时间**重算行人黄灯，
    窗口长度用 `pedestrian.yellow_s`。绿灯延长到 `max_green` 或被 P0 提前抢占时，
    都应以接口层的实际剩余时间为准。
- **仿真台注意**：`panan_bridge.py` 的相位 `min_green_s` 硬编码为 10，小于默认
  `pedestrian.min_green_s=12`，因此**仿真台里行人保底会被跳过**（候选 min_green < 12 被拒绝）。
  若要在仿真中观察行人保底，需把仿真台相位最小绿调到 ≥12。

## 8. 安全开关（`safety`）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `safety.require_config_validation` | true | 加载配置时强制校验 | **生产必须 true**，勿关 |
| `safety.reject_unknown_phases` | true | 拒绝未知相位 ID | **生产必须 true** |
| `safety.reject_stale_observations` | true | 拒绝过期观测（进降级） | **生产必须 true** |

> **实现状态（重要）**：当前 `config_loader.py` 只校验这三个字段是布尔值，**全库没有消费者**
> （搜索不到读取它们的代码），即当前**不产生实际约束力**。它们是为后续接入预留的开关；
> 不要把它们当作"已生效的安全保护"。真正的安全约束由 `safety.py` / `state_machine.py` 实现。

## 9. 仿真台参数（磐安县路网演示，`panan/`）

> 仿真台**不修改逻辑层算法**，只是把决策层接到 SUMO 路网上跑。参数分三类：脚本开关、车辆行为、需求生成。
>
> **重要**：仿真台**不读取** `configs/*.yaml` 的 `signal.phases`、`green_wave.*`、`runtime.mode`、`safety.*`；
> 相位配时用 `panan_bridge.py` 的占位值（`min_green=10 / max_green=60 / yellow=3 / all_red=1`），
> 绿波硬编码关闭。因此改这些 YAML 字段**不会影响仿真台行为**。

### 9.1 一键脚本开关（`panan/scripts/run_demo.sh` / `run_eval.sh`）

| 参数 | 位置 | 默认 | 含义 |
|---|---|---|---|
| 控制器 | `run_demo.sh` 第 1 个参数 | `adaptive` | `adaptive`=决策层控灯 / `fixed`=固定配时对照 / `gif`=离屏动画 |
| `END` | `run_demo.sh` | 86400 | 实时窗口仿真时长（秒），默认长跑、手动关窗停止 |
| `DELAY` | `run_demo.sh` | 250 | **时间流速**：每仿真秒的真实毫秒数（1000=1倍速真实时间，500=2倍速，250≈4倍速） |
| `CTRL` | `run_eval.sh` 第 1 个参数 | adaptive | 评估用控制器 |
| `END` | `run_eval.sh` 第 2 个参数 | 3600 | 评估仿真时长 |
| `regen` | `run_eval.sh` 第 3 个参数 | — | 强制重新生成需求 |

### 9.2 桥接层命令行（`panan/scripts/panan_bridge.py`）

| 参数 | 默认 | 含义 |
|---|---|---|
| `--sumocfg` | 必填 | SUMO 配置文件（路网 + 需求） |
| `--config` | 必填 | 决策层配置（如 `configs/default.yaml`） |
| `--controller` | `adaptive` | `adaptive` / `fixed` |
| `--fixed-green-s` | 30.0 | 固定配时每相位绿灯时长（仅 fixed 控制器） |
| `--end-time` | 3600.0 | 仿真时长（秒） |
| `--step-length` | 1.0 | 仿真步长（秒） |
| `--metrics` | 无 | 逐秒指标 CSV 输出路径 |
| `--report-json` | 无 | 汇总报告 JSON 输出路径 |
| `--csv` | 无 | **4 项汇总指标追加**到该 CSV（每跑一次一行） |
| `--seed` | 42 | 需求种子（仅记录到 CSV 元信息） |
| `--gui` | 关 | 是否开 sumo-gui 实时窗口 |
| `--delay-ms` | 100.0 | GUI 时间流速（毫秒/仿真秒） |

### 9.3 可视化脚本 `demo.py`（被 `run_demo.sh` 调用）

| 参数 | 默认 | 含义 |
|---|---|---|
| `--end-time` | 86400 | 仿真时长（秒） |
| `--delay-ms` | 100 | GUI 时间流速（毫秒/仿真秒） |
| `--controller` | adaptive | `adaptive` / `fixed` |
| `--gif` | 无 | 指定后离屏生成 GIF（无显示器用） |
| `--every` | 3.0 | GIF 每帧间隔（仿真秒） |
| `--fps` | 10 | GIF 帧率 |
| `--view-zoom` | 400 | 视野缩放 |
| `--view-center` | auto | 视野中心（`auto` 或 `x,y`） |

> `run_demo.sh gif` 实际传参：`--every 2 --fps 6`，且 `END=180`。

### 9.4 车辆行为（`panan/config/vtypes.add.xml`）

| 参数 | 默认 | 含义 | 效果 |
|---|---|---|---|
| `accel` | 1.2 | 加速度（m/s²） | 越小起步越缓 |
| `decel` | 3.5 | 减速度（m/s²） | 刹车能力 |
| `sigma` | 1.0 | 驾驶员反应不完美度 [0,1] | **越大起步反应越慢**（拟真起步延时） |
| `tau` | 1.5 | 车头时距（s） | 越大后车跟得越远 |
| `maxSpeed` | 14.0 | 最大速度（m/s，≈50km/h） | 城区限速 |
| `length` / `minGap` | 5.0 / 2.5 | 车长 / 最小间距（m） | 影响排队密度 |
| `speedFactor` | normc(1.0,0.1,0.8,1.2) | 速度因子分布（正态，受上下界截断） | 让每辆车期望车速略有差异，更拟真 |

### 9.5 需求生成（车流）

| 参数 | 位置 | 默认 | 含义 |
|---|---|---|---|
| `--period` | `run_demo.sh`/`run_eval.sh` 的 randomTrips | 1.8 | 随机车流密度（秒/辆，越小越密；约 2000 辆/时） |
| `--fringe-factor` | 同上 | 5 | 路网边缘起终点权重 |
| `--min-distance` | 同上 | 300 | 最短行程（m），过滤太短的出行 |
| `--count` | `gen_fengqing_flow.py`（脚本默认 500；两个 runner 传 300） | 300（runner 传参） | 常规奉庆线车流量 |
| `--to-e10` | `gen_fengqing_flow.py`（脚本默认 400；两个 runner 传 250） | 250（runner 传参） | 奉庆线→E10 方向专项车流量 |
| `--begin` / `--end` | `gen_fengqing_flow.py` | 0 / 3600 | 车流出发时间范围 |
| `--seed` | 生成脚本 | 42 | 随机种子（可复现） |

> **注意**：改过路网（netedit 保存）后，`run_demo.sh`/`run_eval.sh` 会自动检测并重新生成需求（`net.xml -nt routes.xml`）；
> 也可手动 `bash panan/scripts/run_eval.sh adaptive 3600 regen` 强制重建。

### 9.6 相位检查

| 命令 | 作用 |
|---|---|
| `uv run python panan/scripts/inspect_phases.py` | 打印每个信号灯路口的「驶入边数 → 相位数」，标出被合并的路口 |

---

## 10. 优先级顺序（勿动）

```yaml
optimization.priorities:
  - wait_time_guarantee             # P0
  - downstream_spillback_prevention # P1
  - main_road_green_wave            # P2
  - demand_score                    # P3
```

> 顺序即语义优先级，改动会被 `config_loader` 拒绝。

## 7. 优先级护栏（代码强制）

`ScoreConfig` 构造时校验下列不等式，违反即拒绝加载：

```text
green_wave_score > demand.vehicle_weight + demand.wait_weight   # P2 > P3
wait_urgency_max_score > green_wave_score                        # P0 紧迫 > P2
|spillback.penalty| > wait_urgency_max_score                     # P1 压制 > 其余
```

默认值 `1.0 < 2.0 < 5.0 < 1000` 已满足。`green_wave_score = 0` 表示关闭绿波层，此时第一条不校验（其余两条仍生效）。

## 8. 参数汇总入口

代码侧用 `TunableParameters` 一次取全，不必记住各参数散落在哪：

```python
from traffic_control.decision_layer import DecisionLayer

layer = DecisionLayer.from_yaml("configs/simulation.yaml")
p = layer.parameters
p.max_wait_target_s, p.observation_timeout_s, p.confirm_timeout_s
p.queue_speed_threshold_mps, p.vehicle_length_m, p.stopped_gap_m, p.stale_after_s
p.score.green_wave_score, p.score.spillback_penalty, p.score.n_max
p.machine.countdown_duration_s, p.machine.score_hysteresis
```

相位配时（min/max 绿、黄灯、全红）属审核数据，仍在 `signal.phases`，不在 `TunableParameters` 内。
