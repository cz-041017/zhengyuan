# 运行日志数据契约（Logging Data Contract）

> 状态：已定案（用户 6 点答复确认）。实现顺序：本文档 → 本地落盘 → 云端上报 → RL 数据预留。

## 1. 目的与消费方

决策系统落地后需要两类输出，供两个不同消费方：

| 消费方 | 需要的数据 | 频率 | 体积 |
|---|---|---|---|
| 云端后台（可视化 / 问题定位） | **事件**：相位切换、降级切换、心跳故障、告警 | 低（变化时） | 小 |
| 未来大模型强化学习（RL）升级 | **全量轨迹**：每个 tick 的 state/action | 高（每 tick） | 大 |

核心结论：**一份数据源，两条输出**。本地每 tick 全量落盘（事实源 + RL 训练集），云端只在「决策变化 / 离散事件」时上传（可视化，省带宽）。

## 2. 架构总览

```
                        ┌─────────────────────────────────────┐
感知层 ──► 决策层 tick ──► 结构化日志管线 LoggingPipeline          │
                        │   ├─ 每 tick 全量快照 → 本地 JSONL 落盘 │
                        │   ├─ 环形缓冲 BlackBox（崩溃取证）      │
                        │   └─ 事件触发 → CloudSink（MQTT）上传   │
                        └─────────────────────────────────────┘
```

- **本地**：`/var/log/traffic-control/`（默认），逐 tick JSONL，按天 / 大小轮转，按保留天数清理。
- **云端**：MQTT，决策变化 + 降级切换 + 心跳故障 + 告警触发上传。

## 3. Tick 快照 Schema（v1）

每 tick 一条 JSON 行（JSONL），`schema_version: 1`。字段如下：

```json
{
  "schema_version": 1,
  "tick_id": 12345,
  "junction_id": "panan_1",
  "ts_wall": 1731081600.0,
  "ts_mono": 1234.5,
  "boot_id": "boot-uuid",
  "config_hash": "sha256:ab12cd34",
  "mode": "normal",
  "state": {
    "controller": {
      "current_phase_id": "P_N",
      "phase_started_at": 100.0,
      "mode": "normal",
      "signal_colors": {"north_through": "green", "east_through": "red"}
    },
    "degradation": "normal",
    "demands": {
      "P_N": {"vehicle_count": 3, "oldest_wait_s": 12.0, "downstream_blocked": false, "green_wave_active": false}
    },
    "lane_states": {
      "north_through": {"lane_id": "north_through", "vehicle_count": 2, "queue_length_m": 14.0, "oldest_wait_s": 8.0, "downstream_blocked": false, "last_updated_at": 1230.0}
    },
    "downstream_blocked": {},
    "startup": null,
    "active_crosswalks": [],
    "pedestrian_yellow_remaining_s": 0.0
  },
  "action": {
    "selected_phase_id": "P_E",
    "reason": "countdown_complete",
    "scores": {"P_N": 1.2, "P_E": 1.5},
    "constraints_applied": ["hysteresis", "debounce", "countdown"],
    "feasible": true,
    "feedback": "pending"
  },
  "reward": null
}
```

**字段来源**（与决策层 dataclass 一一对应）：

- `state.controller` ← `ControllerState`（`mode`/`current_phase_id`/`phase_started_at`/`signal_colors`）
- `state.degradation` ← `LayerResult.degradation`（`normal`/`partial`/`failsafe`）
- `state.demands` ← `LayerResult.demands`（每相位 `PhaseDemand`：`vehicle_count`/`oldest_wait_s`/`downstream_blocked`/`green_wave_active`）
- `state.lane_states` ← `LayerResult.lane_states`（每车道 `LaneState`：`lane_id`/`vehicle_count`/`queue_length_m`/`oldest_wait_s`/`downstream_blocked`/`last_updated_at`）
- `action` ← `LayerResult.decision`（`PhaseDecision`）+ `feedback`

**明确不存**（部署决策）：原始 `VehicleObservation`（逐车 track_id/speed）。64 GB 存储预算有限，只存聚合后的 `demands` + `lane_states`。

**`reward`**：预留字段，恒为 `null`。RL 升级时由后台 / 离线从日志反推（延误、排队、通行量），离线学习时再回填。

**`config_hash`**：决策配置的稳定哈希（对完整配置字典做规范化 JSON 序列化后取 SHA-256 前 16 位十六进制）。注意哈希的是完整配置而非仅 `TunableParameters`——因为相位集合与其配时同样是动作空间的一部分，仅参数相同但相位不同的两份配置不应碰撞。离线训练时用于区分「哪套配置产生的数据」。

## 4. 事件类型与上传触发

云端只在以下情况上传事件（事件 payload 均含 `tick_id` + `ts_wall`）：

| event_type | 触发条件 | 关键字段 |
|---|---|---|
| `decision_change` | `selected_phase_id` 变化（相位切换） | `from_phase` / `to_phase` / `reason` |
| `degradation_change` | 降级模式变化（`normal`↔`partial`↔`failsafe`） | `from_mode` / `to_mode` |
| `mode_change` | 控制器模式变化（`normal`/`clearance`/`all_red`/`fault`/`manual_override`） | `from_mode` / `to_mode` |
| `feedback_failure` | 执行反馈失败（`feedback == failure`，上升沿） | `current_phase_id` / `requested_phase_id` |
| `alarm` | 决策层告警（复用 `AlarmEvent`） | `alarm_type` / `level` / `detail` |
| `heartbeat_fault` | 信号机心跳超时（由主机循环 `publish_event` 上报） | `detail` |
| （自定义） | 主机循环 `publish_event(event_type, payload)` 任意事件 | 任意 |

> 说明：`feedback_failure` 是日志层补上的缺口——决策层当前在 `LayerResult.feedback` 返回 `failure` 时不发告警（`AlarmType.FEEDBACK_TIMEOUT` 未接线），日志管线在此上升沿处补发事件。

## 5. MQTT 上传协议

- **Topic**：
  - 事件：`traffic-control/{junction_id}/event/{event_type}`（QoS 1，retain=false）
  - 告警：`traffic-control/{junction_id}/alarm`（QoS 1，retain=false）
- **Payload**：JSON，每消息一个事件。
- **鉴权**：用户名 / 密码（config 注入），TLS 可选（端口 8883）。
- **幂等**：事件含 `tick_id`，后台按 `(junction_id, event_type, tick_id)` 去重。
- **重试**：有界内存出站队列（默认 10000 条）+ 断线指数退避重连（1–30s）；断线时事件入队、重连后按序补发。事件同时写入本地 JSONL（§6），因此队列满只会丢「云端投递」、绝不丢「审计记录」。

## 6. 本地落盘策略

- **位置**：`/var/log/traffic-control/`（config 可配）。
- **文件**：活动文件 `tick.jsonl`；轮转后 `tick_{YYYYMMDD_HHMMSS}.jsonl`。
- **轮转**：活动文件大小 ≥ `max_bytes_per_file`（默认 50 MB）或跨天时轮转。
- **保留**：按 `retention_days`（默认 30 天）清理轮转文件。
- **溢出兜底**：`max_total_bytes`（可选）——总字节超限时从最旧轮转文件开始删除。
- **黑匣子**：`BlackBox` 环形缓冲保留最近 120 个 tick（2 Hz 下约 60 秒），异常退出时 dump 到 `.crash` 文件。
- **可靠性**：日志层是**尽力而为**的——任何写盘 / 上传错误只记 warning，绝不抛进控制回路（安全关键）。

## 7. 存储预算与保留天数

- 单条快照约 **500 字节**（紧凑 JSON，8 车道 + 4 相位聚合，无原始观测）。
- 采样率 2 Hz（`tick_period_s=0.5`）→ **172,800 条/天 ≈ 86 MB/天 ≈ 2.6 GB/月**。
- RK3588 eMMC 常见 32–64 GB，系统 + 模型 + 代码约 10–20 GB，日志可用约 10–30 GB。
- **默认保留 30 天 ≈ 2.6 GB**，安全余量充足。可通过 `retention_days` 调大；`max_total_bytes`（建议设为日志分区 80%）作为溢出兜底。

## 8. RL 数据预留

快照已按 `(state, action, reward)` 骨架设计，本地 JSONL 逐 tick 全量轨迹即训练集。本期**只记录、不开发导出**（用户确认）；导出工具（本地 JSONL → 标准 (s,a,r) 序列）留待 RL 升级时实现。本节只定义「预留」的边界与语义。

### 8.1 (state, action, reward) 映射

| RL 项 | 快照字段 | 说明 |
|---|---|---|
| `state` | `state.*` | 决策层本 tick 的聚合输入：`controller`（当前相位/开始时刻）、`degradation`、`demands`、`lane_states`、`downstream_blocked`、`startup`、`active_crosswalks` |
| `action` | `action.selected_phase_id` | 决策层提议的下一相位；`reason` / `scores` / `constraints_applied` / `feasible` 是策略可解释性的旁证 |
| `reward` | `reward` | 恒 `null`，离线回填（见 8.2） |
| 策略版本 | `config_hash` | 区分「哪套配置产生这条轨迹」 |
| 轨迹身份 | `(junction_id, boot_id)` | 一次连续运行 = 一条轨迹；`tick_id` 单调递增给出时间序 |

### 8.2 离线 reward 的可计算性

`reward` 不落盘，但**计算 reward 所需的聚合指标已全部落盘**，离线可回填：

- `state.lane_states[*].oldest_wait_s` / `queue_length_m` → 排队延误、队列长度惩罚
- `state.demands[*].oldest_wait_s` / `vehicle_count` → 相位级等待 / 需求
- `state.downstream_blocked` → 溢流惩罚
- `state.controller.current_phase_id` / `phase_started_at` → 绿灯持续时间 / 切换频率
- `action.selected_phase_id` + `action.feasible` → 动作及是否可行

reward 函数本身（延误 / 停车次数 / 通行量 的加权）是 RL 升级时的设计决策，此处只保证「数据够算、不算丢」。

### 8.3 一个关键语义（未来 RL 开发必读）

`action.selected_phase_id` 是**决策层的提议**，不一定是信号机实际执行的结果。实际相位由 `state.controller.current_phase_id`（`SignalControllerDriver` 的乐观模型）逐 tick 追踪；在 `manual_override` / `failsafe` 时提议会被覆盖。离线训练时以 `state.controller` 的相位流为「真实动作」，`action` 为「策略提议」。

## 9. 兼容性

- `schema_version` 是唯一版本标识；字段新增走向后兼容（后台忽略未知字段），破坏性变更必须升版本号。
- 枚举一律序列化为小写字符串（`str, Enum` 的 `.value`）。
