# 感知层（YOLO11）→ 决策层 接口文档

> 面向对象：感知层开发同事。
> 目标：把 YOLO11 的目标识别 / 跟踪结果，以决策层能直接消费的格式传入，完成「停止线前等待车辆计数 → 逻辑决策」。
> 决策层项目：`smart-traffic-control`（逻辑决策层），仓库见 [`README.md`](../README.md)。

---

## 1. 决策层需要什么（总览）

决策层每个周期（tick）只消费两种输入：

| 输入 | 用途 | 对应代码类型 |
|---|---|---|
| **车辆检测流** | 逐车道排队计数、等待时间（P0/P3） | `VehicleObservation` |
| **下游占用** | 下游溢流保护（P1） | `DownstreamReport` |

决策层的输出是「建议的下一相位」`PhaseDecision`（**不是**灯色指令），由控制接口层转发给信号机。

> 完整决策算法见 [`decision-algorithm-design.md`](decision-algorithm-design.md)。

---

## 2. 数据契约 A：车辆检测

> **数据流**：感知层传入 `RawDetection`（原始检测）→ 经 `normalize_detections()` 校验并转换为
> 决策层内部使用的 `VehicleObservation`。两者字段**基本对应**，但 `RawDetection.confidence`
> 仅保留在感知侧，`VehicleObservation` 不含该字段（normalize 时丢弃）。
> `normalize_detections` 会校验 `lane_id` 必须是下方 8 个规范 ID 之一，否则直接报错（不静默丢弃）。

每辆车每次被识别，发一条检测记录。字段如下：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `track_id` | 整数 ≥0 | ✅ | 跨帧稳定的跟踪 ID，**同一辆车必须始终用同一个 ID** |
| `lane_id` | 字符串 | ✅ | 车辆当前所在车道的规范 ID（见 §2.1） |
| `observed_at` | 浮点（秒） | ✅ | 观测时刻，见 §2.2 时间戳约定 |
| `speed_mps` | 浮点 ≥0 或 null | ⬜ | 车辆速度（m/s）。**强烈建议提供**，用于排队判定 |
| `confidence` | 浮点 [0,1] 或 null | ⬜ | 置信度（预留，决策层当前忽略） |

### 2.1 车道 ID 约定（务必一致）

决策层只识别以下 **8 个规范车道/流向 ID**（小写、下划线）：

| 方向 | 直行 | 左转 |
|---|---|---|
| 北 N | `north_through` | `north_left` |
| 东 E | `east_through` | `east_left` |
| 南 S | `south_through` | `south_left` |
| 西 W | `west_through` | `west_left` |

**你必须提供「相机像素区域 → 规范 lane_id」的映射**（每个路口现场标定一次）。例如：

```yaml
# 站点级映射示例（你们维护），把每个摄像头里的车道框映射到规范 ID
camera_1:
  lane_roi_0: north_through
  lane_roi_1: north_left
camera_2:
  lane_roi_0: east_through
  ...
```

> 规则：车辆一旦跨越「停止线前等待区」边界，就用该车道的规范 ID 标记；
> 换道时 `track_id` 不变、`lane_id` 更新为当前所在车道。

### 2.2 时间戳约定

- `observed_at` 用**同一时钟**（建议自研边缘机统一 NTP），单位秒。
- 决策层用 `observed_at` 计算「最老等待时间」（P0 的 130s 计时），时钟漂移会直接破坏等待计时，请保证同步。
- **必须 `observed_at ≤ now`**：`QueueEstimator` 会丢弃"时间戳在未来"的观测（视为非法），
  请确保边缘机时钟不超前。

### 2.3 `track_id` 稳定性（关键）

- 等待时间 = `now − 该方向最早出现的 track 的 observed_at`。
- **丢跟踪 / ID 跳变会重置等待计时**，可能导致某个方向永远不被判定为「等得久」。
- 要求：停止线前等待区内，同一辆车 ID 稳定；车辆驶离后 ID 可复用。

### 2.4 `speed_mps` 说明

- 决策层用 `speed_mps ≤ 0.5 m/s` 判定「排队车辆」（阈值可调，见参数参考）。
- 无法测速时填 `null`：该车仍计入「车辆数」；但**排队长度会被记为 `0.0`（而非"未知"）**——
  这会低估排队压力，建议尽量提供测速。

### 2.5 频率与新鲜度（务必遵守）

- 建议上报频率 **≥ 2 Hz**（生产默认 2 Hz = 0.5s 一帧；2–5 Hz 更佳）。
- **即使当前帧 0 辆车，也要发一帧空列表**（心跳）。决策层把「超过 `3 s` 无数据」视为**数据丢失**，会进入降级模式（固定配时/黄闪），**而不是当作「没车」**。

---

## 3. 数据契约 B：下游占用 `DownstreamReport`（P1 溢流保护）

每个方向的下游路口，需要上报「下游路段是否已堵」：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `lane_id` | 字符串 | ✅ | 规范车道 ID，表示「喂给这条下游路段的来车方向」 |
| `occupancy` | 浮点 [0,1] 或 null | ✅ | 下游路段归一化占用率；`null` = 未知 |
| `observed_at` | 浮点（秒） | ✅ | 观测时刻 |

- 占用率定义由你们定义（如「下游停止线后 50m 内排队车辆数 / 该路段容量」），只需**归一化到 [0,1]** 且**口径一致**。
- 决策层滞回阈值：`occupancy ≥ 0.8` 判「下游拥堵」，降到 `≤ 0.5` 才解除（阈值可调）。
- **红线**：拿不到下游数据时上报 `null`，**不要上报 0**——决策层会把「未知」按「拥堵」处理，把 0 误报为畅通可能造成上游继续放行、下游溢出。
- **校验**：`DownstreamReport.lane_id` 现在**会做规范 ID 校验**（§2.1 的 8 个 ID，拼错直接抛 `ValueError`）；
  `observed_at` 当前仍**不被消费**（决策层只看"有没有返回条目"，不按时间戳判新鲜度）。
- **缺条目 ≠ null**：若某个下游方向**整条缺失**（没发报告），`SpillbackMonitor` 不会清零，
  而是**保持上一次状态**。这可能把"断流"误判为"畅通"，因此**务必每帧都发全部下游方向**。

---

## 4. 每帧打包 `PerceptionFrame`

一个上报周期把两类数据打包成一帧。Python 端结构：

```python
@dataclass(frozen=True)
class PerceptionFrame:
    observed_at: float                          # 帧时间戳（秒）
    detections: tuple[VehicleObservation, ...]  # 车辆检测（已 normalize）
    downstream: tuple[DownstreamReport, ...] = ()  # 下游占用（可空）
```

对应 JSON 表示：

```json
{
  "observed_at": 1730000000.0,
  "detections": [
    {"track_id": 101, "lane_id": "north_through", "observed_at": 1730000000.0, "speed_mps": 0.2, "confidence": 0.93},
    {"track_id": 102, "lane_id": "east_left",     "observed_at": 1730000000.1, "speed_mps": null}
  ],
  "downstream": [
    {"lane_id": "east_through",  "occupancy": 0.85, "observed_at": 1730000000.0},
    {"lane_id": "west_through",  "occupancy": null, "observed_at": 1730000000.0}
  ]
}
```

---

## 5. 集成方式

当前决策层提供**进程内 Python 边界** [`perception.py`](../src/traffic_control/perception.py)：

```python
from traffic_control.perception import (
    RawDetection, DownstreamReport, normalize_detections, downstream_occupancy,
)

observations = normalize_detections([
    RawDetection(track_id=101, lane_id="north_through", observed_at=now, speed_mps=0.2),
])
occupancy = downstream_occupancy([
    DownstreamReport(lane_id="east_through", occupancy=0.85, observed_at=now),
])
# downstream_occupancy 返回 dict[lane_id -> float|None]，None 表示"未知"（决策层按拥堵处理）
# 之后：QueueEstimator.update(observations, now)  →  聚合为 PhaseDemand → PhaseStateMachine.tick()
```

- 若你们是**独立进程/跨机**：把上面 JSON 通过 MQTT / HTTP / 共享内存发送即可，字段名**完全一致**。
- 将来上信号机对端协议时，再落到 GB/T 20999 等标准（属控制接口层，另行设计）。

---

## 6. 约束与已知边界（请知悉）

1. **右转暂未建模**：当前相位只含「直行 + 左转」。若路口有**受信号控制的右转**，需要扩展相位模型，请先告知，不要擅自把右转并入其它车道 ID。
2. **`confidence` 暂不使用**：预留字段，未来可能用于数据质量过滤，现阶段可填可不填。
3. **规范 ID 是 8 个**：多出或改名会直接报错（`unknown lane_id`），确保映射表与此一致。

---

## 7. 联调自检清单

- [ ] 车道映射表已建立，8 个规范 ID 全部能覆盖；
- [ ] 同一辆车跨帧 `track_id` 稳定；
- [ ] 时钟同步，`observed_at` 一致；
- [ ] 空场景也按时发心跳（≥2 Hz）；
- [ ] 下游占用按 [0,1] 归一化，拿不到时发 `null` 而非 0；
- [ ] 能测速时提供 `speed_mps`。

---

## 8. 生产接入（main.py 消费的 JSON 文件）

板子生产入口 [`main.py`](../main.py) 每个控制周期（`tick_period_s`，默认 0.5s = 2 Hz）读取一个
**JSON 文件快照**，路径由 [`configs/deployment.yaml`](../configs/deployment.yaml) 的
`perception.input_path` 指定（默认 `/var/run/traffic-control/perception.json`）。

### 8.1 文件格式

与 §4 的 `PerceptionFrame` JSON **完全一致**：

```json
{
  "observed_at": 1730000000.0,
  "detections": [
    {"track_id": 101, "lane_id": "north_through", "observed_at": 1730000000.0, "speed_mps": 0.2, "confidence": 0.93}
  ],
  "downstream": [
    {"lane_id": "east_through", "occupancy": 0.85, "observed_at": 1730000000.0}
  ]
}
```

字段含义同 §2/§3：`detections` 里 `track_id`/`lane_id` 必填，`speed_mps`/`confidence` 可空；
`downstream` 每个方向一条，`occupancy` 拿不到填 `null`。

### 8.2 写入方式（务必遵守）

1. **原子写**：先写同目录临时文件，再 `os.replace(tmp, 目标)`（POSIX 原子替换）。
   否则 `main.py` 可能读到写了一半的 JSON，被当作损坏帧丢弃。
2. **频率 ≥2 Hz**：与 `tick_period_s`（默认 0.5s）对齐；即使空场景也要按时写心跳帧
   （`detections: []`、`downstream: []`）。
3. **时钟**：`observed_at` 用 NTP 同步的 epoch 秒，与边缘机主时钟一致（见 §2.2）。
4. **断流语义（关键）**：`main.py` 自身不判新鲜度，只把「读不到 / 损坏」当成空输入传给决策层。
   决策层在 `observation_timeout_s`（默认 3s）无数据后进入降级（PARTIAL），
   `2×`（6s）后进入 **FAILSAFE** —— 生产入口会请求**全红**并告警。
   **所以：感知进程崩溃 ≠ 路口放空，而是触发全红保护，交给信号机本地兜底。**
