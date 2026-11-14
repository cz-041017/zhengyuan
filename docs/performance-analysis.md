# 性能瓶颈与优化报告

> 范围：逻辑决策层（`src/traffic_control/`）+ 仿真台（`panan_bridge.py`）
> 日期：基于 24h 拟真仿真与 cProfile 实测

## 1. 实测基线

| 项目 | 数值 | 来源 |
|---|---|---|
| 单路口单次 tick 时延 | p50=0.044ms / p95=0.047ms / p99=0.05ms | bench（8 车道满观测，N=10~300） |
| 24h 仿真 wall（41 路口） | 自适应 **120s**，固定配时 **25s** | panan_bridge 实测 |
| 逻辑层 tick 的 wall 贡献 | ≈ 120 − 25 = **95s** | 上两行之差 |

> 关键数字：逻辑层 tick 贡献了仿真 wall 的约 **80%**（95s / 120s）。固定配时不含逻辑层，只有 TraCI 车辆统计 + TLS 设置，仅 25s。

## 2. cProfile 热点（1200s 自适应，1271 万次调用）

| 排名 | 函数 | 次数 | 累计耗时 | 占比 |
|---|---|---|---|---|
| 1 | `_step_adaptive`（决策入口） | 1200 | 3.305s | 96.8% |
| 2 | `PhaseStateMachine.tick` | 45,456 | 1.498s | 43.4% |
| 3 | `_phase_score` | 315,974 | 0.895s | 25.9% |
| 4 | `Scorer.phase_score` | 315,974 | 0.546s | 15.8% |
| 5 | `builtins.max`（候选择优） | 983,593 | 0.372s | 10.8% |
| 6 | `demand_score` | 315,974 | 0.274s | 7.9% |
| 7 | `aggregate_demand` | 174,000 | 0.168s | 4.9% |

（TraCI/libsumo 的 C++ 执行不在此 profile 内，另见 §4。）

## 3. 瓶颈定位

### 瓶颈 A：相位评分（最大热点，占 Python 时间 ~50%）

- 每个路口每步对 **~7 个候选相位** 做完整评分：`tick → _phase_score → phase_score → demand_score/aggregate_demand`。
- `315,974 次 _phase_score` = 41 路口 × 1100 步 × 7 候选。评分内部又逐车道聚合（`aggregate_demand` 174,000 次）与反复 `max`（98 万次）。
- 这是**纯计算瓶颈**，与 I/O 无关。

### 瓶颈 B：TraCI 通信（未计入 profile，但决定 wall）

- 每步对每辆车做多次 TraCI 调用：`getRoadID / getSpeed / getWaitingTime / getTimeLoss / getLanePosition / getRoute`（高峰 90 车 × 6 ≈ 540 次/步）。
- 每步对 41 个路口 `getRedYellowGreenState`（绿灯利用率统计新增）。
- 都是逐次调用，未用 subscription 批量接口。

### 瓶颈 C：绿灯利用率统计（本次新增的开销）

- `green_junctions` 每步 41 次 `getRedYellowGreenState`，86400 步 × 41 = **354 万次** TLS 查询。
- 该状态其实可从控制器内部状态推导，无需每次查 TraCI。

## 4. 优化建议（按收益排序）

| 优先级 | 措施 | 预期收益 | 风险 |
|---|---|---|---|
| P0 | 评分剪枝：`min_green` 未到、或硬约束已排除的相位不评分；缓存跨步不变的观测聚合结果 | tick 时延 ↓30~50% | 低（评分只做排序，剪枝不改语义） |
| P0 | TraCI 批量：用 `vehicle.subscribe` + `getSubscriptionResults` 一次拉全部车辆状态；TLS 状态用控制器内部 phase_id 推导，去掉每步 `getRedYellowGreenState` | wall ↓20~40% | 低（只改采集方式） |
| P1 | 决策频率自适应：仅当有车通过/排队变化才 tick 该路口（车少时段跳过） | 夜间 wall 大降 | 中（需保证 FAILSAFE/P0 兜底不跳过） |
| P2 | 评分内层用 `max`/`sum` 的 C 级聚合替代逐元素循环；`_best_candidate` 用单次 `max(key=...)` 替代多次比较 | tick 时延 ↓10% | 低 |
| P2 | 若需扩展路口数，将评分向量化（numpy）或按路口分片并行 | 线性扩展 | 中（引入依赖） |

## 5. 部署评估（RK3588）

- 部署是**单路口**（非 41），2Hz tick（`tick_period_s=0.5`）。
- 单路口 tick 0.044ms → 每 0.5s 一次，CPU 占用 ≈ 0.044ms / 500ms = **<0.01%**，性能余量极大。
- 真正的部署风险不在逻辑层计算，而在**感知输入延迟**与**串口/HDLC 通信**（`interface/hikvision`），属 I/O 瓶颈，与本报告的计算瓶颈无交集。
- 结论：逻辑层在板子上**无需优化**即可满足 2Hz 实时性；本报告的优化主要服务于**仿真/调优循环**（把 24h 仿真从 120s 压到 60s 量级，加快迭代）。

## 6. 建议落地顺序

1. 先做 **P0 评分剪枝 + TraCI 批量**（两处改动互不冲突，可一起做，验证 24h 仿真 wall 是否降到 ~60s）。
2. 确认指标不变（5 项评价指标 + P0>130=0 + 冲突=0）后再上 **P1 决策频率自适应**。
3. 数据中台保留每次优化前后的 wall/指标，作为"性能回归"记录。
