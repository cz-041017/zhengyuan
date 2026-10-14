# 决策层可调参数参考（调参手册）

> 全部初值均为**待标定值**，须经仿真 + 真机标定后方可部署；**任何改动不得破坏优先级相对次序 P0 > P1 > P2 > P3**。
> 参数集中在 [`configs/*.yaml`](../configs/) 与 [`config_loader.py`](../src/traffic_control/config_loader.py) 的校验里。

## 1. 评分与优先级（P0–P3）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `optimization.max_wait_target_s` | 120 | P0 单方向最长等待（s），硬指标 | 国标/甲方要求，一般不动 |
| `optimization.wait_urgency_start_s` | 80 | S_wait 软预警起点（s） | 越接近 120 越「激进」，太小会过早压过绿波 |
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

## 2. 相位与配时

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `signal.layout.mode` | `minor_only` | 相位分组：全支路 4 组 / 主路绿波 4 组 | 按路口类型选 |
| `signal.layout.main_road_direction` | null | 主路朝向（`NS`/`EW`） | `main_road` 时必填 |
| `signal.phases[].min_green_s` | — | 最小绿（s，硬下限） | 需满足行人过街/清空，来自审核 |
| `signal.phases[].max_green_s` | — | 最大绿（s，硬上限） | 过长会让 120s 依赖抢占（见时序分析） |
| `signal.phases[].yellow_s` | — | 黄灯（s） | 来自 GB 25280/审核配置，不臆造 |
| `signal.phases[].all_red_s` | — | 全红（s） | 同上 |

> 提示：跑 [`timing_analysis.py`](../src/traffic_control/timing_analysis.py) 看「无抢占最坏等待」，若远超 120s 说明 max 绿配得太长，120s 全压在了抢占上。

## 3. 绿波（P2）

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `green_wave.enabled` | false | 是否启用绿波 | 有主路协调时开启 |
| `green_wave.main_through_phase` | null | 主路直行相位 ID | 通常 `P_MT` |
| `green_wave.band_start_s` / `band_end_s` | 0 / 45 | 绿波窗口（相位周期内起止） | 按上游路口协调相位对齐 |
| `green_wave.period_s` | 120 | 绿波周期（s） | 与信号周期一致 |

## 4. 感知 / 数据链路

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `runtime.observation_timeout_s` | 3.0 | 方向级数据新鲜度超时（s） | 须 > 感知上报周期；超时即降级 |
| `runtime.confirm_timeout_s` | 10.0 | 切换执行确认超时（s） | 须 > 黄灯+全红+余量 |
| `queue_estimation.queue_speed_threshold_mps` | 0.5 | 排队速度阈值（m/s） | 按现场怠速/蠕行速度标定 |
| `queue_estimation.vehicle_length_m` | 5.0 | 平均车长（m） | 用于排队长度估算 |
| `queue_estimation.stopped_gap_m` | 2.0 | 停车间距（m） | 同上 |
| `queue_estimation.stale_after_s` | 3.0 | 轨迹过期清理（s） | 略大于上报周期 |

## 5. 倒数

| 参数 | 默认 | 含义 | 调参建议 |
|---|---|---|---|
| `countdown.display_duration_s` | 15 | 15s 切换倒数（s） | 与 GA/T 508 显示设备一致 |

## 6. 优先级顺序（勿动）

```yaml
optimization.priorities:
  - wait_time_guarantee            # P0
  - downstream_spillback_prevention # P1
  - main_road_green_wave            # P2
  - demand_score                    # P3
```

> 顺序即语义优先级，改动会被 `config_loader` 拒绝。
