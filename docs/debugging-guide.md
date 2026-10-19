# 调试与复现手册

> 面向：本项目的开发者 / 联调同事。
> 目标：**从零复现环境**、跑通决策层、**复现各类算法行为**、排查常见故障。
> 相关文档：算法设计 [`decision-algorithm-design.md`](decision-algorithm-design.md)　·　可调参数 [`parameter-reference.md`](parameter-reference.md)　·　感知层接口 [`perception-interface.md`](perception-interface.md)

---

## 1. 环境复现（从零开始）

### 1.1 前置要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows / Linux / macOS 均可 |
| Python | **3.11**（由 uv 自动安装，无需手动装） |
| git | 任意近期版本 |
| uv | 必需（依赖与 Python 版本由它管理） |

安装 uv：

```powershell
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1.2 克隆并复现依赖

```powershell
git clone https://github.com/cz-041017/zhengyuan.git
cd zhengyuan
uv sync
```

`uv sync` 会：读取 [`.python-version`](../.python-version)（3.11）→ 必要时下载 Python → 在项目内创建 `.venv` → 严格按 [`uv.lock`](../uv.lock) 安装锁定版本依赖。

### 1.3 验证复现成功

```powershell
uv run pytest -q
# 期望输出：120 passed

uv run python --version
# 期望：Python 3.11.x

uv run smart-traffic-control
# 期望：Smart Traffic Control: decision engine ready.
```

三项都通过，说明环境与代码完全复现。

---

## 2. 跑一次决策（最小示例）

推荐用门面 `DecisionLayer`：内部串起 排队估计 → 新鲜度/降级 → 保守等待 → 溢流滞回 → 状态机 → 执行反馈。

```python
from traffic_control.decision_layer import DecisionLayer
from traffic_control.models import ControllerMode, ControllerState, VehicleObservation

layer = DecisionLayer.from_yaml("configs/simulation.yaml")

# 全部可调参数一次取全
p = layer.parameters
print(p.max_wait_target_s, p.observation_timeout_s, p.score.green_wave_score)

state = ControllerState(mode=ControllerMode.NORMAL, current_phase_id="P_MT", phase_started_at=0.0)
observations = [
    VehicleObservation(track_id=1, lane_id="west_through", observed_at=0.0, speed_mps=0.0),
]

result = layer.tick(
    now=120.0,
    state=state,
    observations=observations,
    downstream_occupancy={"east_through": 0.9},   # None 表示未知，按拥堵处理
)

print("下一步相位：", result.decision.selected_phase_id)
print("原因：", result.decision.reason)
print("各相位得分：", result.decision.scores)
print("应用的约束：", result.decision.constraints_applied)
print("降级模式：", result.degradation)
print("执行反馈：", result.feedback)
```

只想验证单个部件时，走底层 API：

```python
from traffic_control.config_loader import load_config
from traffic_control.runtime import build_runtime
from traffic_control.state_machine import PhaseDemand

runtime = build_runtime(load_config("configs/simulation.yaml"))
decision = runtime.state_machine.tick(
    state,
    12.0,
    {
        "P_MT": PhaseDemand(green_wave_active=True),
        "P_W": PhaseDemand(vehicle_count=6, oldest_wait_s=118.0),
    },
)
```

`decision.reason` 是**排查问题的第一入口**，常见取值：

| `reason` | 含义 |
|---|---|
| `minimum_green_not_met` | 当前相位未走完最小绿，不能切换 |
| `p0_wait_preempt` | **P0 硬抢占**：某方向已到等待截止，直接调度 |
| `max_green_reached` | 达到最大绿，强制切换 |
| `debouncing` / `countdown_started` / `countdown_in_progress` / `countdown_complete` | 分数驱动切换的 15s 倒数各阶段 |
| `countdown_cancelled` | 倒数期间候选分数回落，取消切换 |
| `score_not_exceeded` | 无候选项分数超过当前（含滞回） |
| `no_feasible_candidate` / `no_feasible_successor` | 所有候选被下游拥堵排除 |
| `p0_p1_conflict` | **P0 与 P1 冲突**：截止方向下游又拥堵 → `feasible=False`，需上报仲裁 |
| `controller_not_normal` / `unknown_current_phase` | 状态非法（故障/清空/未知相位） |

---

## 3. 复现各类算法行为

最直接的方式是**跑对应的单元测试**（每个行为都有独立用例）：

```powershell
# 跑单个用例（-v 显示用例名）
uv run pytest tests/test_state_machine.py::test_p0_preempt_switches_immediately -v
```

| 想复现的行为 | 命令 | 对应模块 |
|---|---|---|
| P0 硬抢占（等待到截止） | `uv run pytest tests/test_state_machine.py -v -k preempt` | `state_machine.py` |
| 15s 倒数完整生命周期 | `uv run pytest tests/test_state_machine.py -v -k countdown` | `state_machine.py` |
| 倒数中途取消 | `uv run pytest tests/test_state_machine.py -v -k cancelled` | `state_machine.py` |
| P1 下游拥堵排除候选 | `uv run pytest tests/test_state_machine.py -v -k blocked` | `state_machine.py` |
| P0–P1 冲突判不可行 | `uv run pytest tests/test_state_machine.py -v -k p0_p1` | `state_machine.py` |
| 最小绿/最大绿边界 | `uv run pytest tests/test_state_machine.py -v` | `state_machine.py` |
| 评分公式与优先级次序 | `uv run pytest tests/test_scoring.py -v` | `scoring.py` |
| 溢流滞回（含未知数据按拥堵） | `uv run pytest tests/test_scoring.py -v -k spillback` | `scoring.py` |
| 相位分组（模式 A / B） | `uv run pytest tests/test_phasing.py -v` | `phasing.py` |
| 冲突矩阵对称性校验 | `uv run pytest tests/test_phasing.py -v -k conflict` | `phasing.py` |
| 三种降级模式 | `uv run pytest tests/test_degradation.py -v -k degradation` | `degradation.py` |
| 保守等待（数据缺失） | `uv run pytest tests/test_degradation.py -v -k conservative` | `degradation.py` |
| 执行反馈失败三态 | `uv run pytest tests/test_feedback.py -v` | `feedback.py` |
| 120s 可行性/最坏等待 | `uv run pytest tests/test_timing_analysis.py -v` | `timing_analysis.py` |
| 感知层输入校验 | `uv run pytest tests/test_perception.py -v` | `perception.py` |
| 端到端装配 + 决策 | `uv run pytest tests/test_runtime.py -v` | `runtime.py` |
| **决策层门面（推荐入口）** | `uv run pytest tests/test_decision_layer.py -v` | `decision_layer.py` |
| 反馈超时（同一目标重复提议） | `uv run pytest tests/test_feedback.py -v -k reproposed` | `feedback.py` |
| 重复相位被拒 | `uv run pytest tests/test_config_loader.py -v -k duplicate` | `config_loader.py` |
| 优先级护栏（调反被拒） | `uv run pytest tests/test_scoring.py -v -k outranking` | `scoring.py` |
| 不可行配时告警 | `uv run pytest tests/test_runtime.py -v -k infeasible` | `runtime.py` |

---

## 4. 调参与验证

1. **改参数**：编辑 [`configs/`](../configs/) 下的 YAML（参数含义与建议见[参数手册](parameter-reference.md)）；
2. **改完立即验证**：`uv run pytest -q`。配置校验会拦住非法值，例如：
   - 优先级顺序被调反（`green_wave_score` 不大于需求权重和等）→ `ConfigError`
   - 重复的 `phase_id` → `ConfigError`
   - `o_low ≥ o_high`、`min_green > max_green`
   - 结构不可行的配时（min 绿 + 清空 ≥ 120s）→ 装配时 `UserWarning`
3. **检查 120s 是否结构可行**：

```python
from traffic_control.config_loader import load_config
from traffic_control.runtime import build_runtime
from traffic_control.timing_analysis import analyze_timing

runtime = build_runtime(load_config("configs/simulation.yaml"))
a = analyze_timing(runtime.phases, runtime.score_config.max_wait_target_s)
print("120s 可行：", a.feasible_120s)
print("最大切换开销(s)：", a.max_time_to_green_s)
print("无抢占最坏等待(s)：", a.worst_wait_s)
print("依赖抢占的相位：", a.preempt_required)
```

> 若 `preempt_required` 覆盖全部相位，说明最大绿配得过长，120s 全靠抢占兜底——应缩短最大绿，使 120s 成为**结构性保证**。

---

## 5. 常见问题排查（Troubleshooting）

### 5.1 命令一执行就崩溃，退出码 `0xC0000142`

- **现象**：任何命令都启动失败（DLL 初始化失败），连 `git --version` 都跑不了。
- **原因**：**运行环境处于受限沙箱模式**（不是代码问题，也不是 git/uv 装坏了）。
- **处理**：把当前会话 / 终端切到**完全权限（full access）**模式即可恢复正常。若在 CI 或容器里遇到，检查是否有安全策略限制进程创建。

### 5.2 `git` 或 `uv` 提示「无法将...识别为 cmdlet/命令」

- **原因**：新起的 PowerShell 进程没有合并「用户级 PATH」，而 uv 装在 `%USERPROFILE%\.local\bin`。
- **处理**（当前会话临时修复）：

```powershell
$env:PATH = [Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User')
```

- **永久修复**：重开一个终端（新终端会读到已写入的用户 PATH）。

### 5.3 无法运行 `.ps1` 脚本（"禁止运行脚本"）

- **现象**：`无法加载文件 ... 因为在此系统上禁止运行脚本`。
- **处理**：临时绕过或调整当前用户策略：

```powershell
# 方案一：单次绕过
powershell -NoProfile -ExecutionPolicy Bypass -File .\some-script.ps1

# 方案二：调整当前用户策略（推荐 RemoteSigned）
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 5.4 `uv sync` 报依赖与 `uv.lock` 不一致

```powershell
uv sync            # 按锁文件对齐
uv lock --upgrade  # 仅在确认要升级依赖时使用（会改动 uv.lock，需 review）
```

### 5.5 测试失败怎么定位

```powershell
uv run pytest -q                       # 先看总体
uv run pytest -x -vv                   # 遇到第一个失败即停，显示详细
uv run pytest tests/test_state_machine.py::用例名 -vv   # 精确定位
```

多数失败来自**配置被改坏**（`ConfigError` 会在信息里指出具体字段），先核对 `configs/*.yaml`。

### 5.6 Git 提示 `LF will be replaced by CRLF`

Windows 下 `core.autocrlf=true` 的正常提示，**不影响内容**，可忽略。

---

## 6. 提交与发布

```powershell
git add -A
git commit -m "docs: ..."        # 或 feat/fix
git push origin main
```

- **首次推送需要 GitHub 凭据**（Git Credential Manager 会引导登录）；
- 提交前建议先 `uv run pytest -q` 确认全绿；
- `.venv`、`.pytest_cache`、`__pycache__` 已被 [`.gitignore`](../.gitignore) 排除，不会误入库。

---

## 7. 尚未实现（调试时请注意）

- **仿真台**尚未实现：目前没有「按时间推进的控制器执行模型 + 车流到达生成 + 主循环 + 指标统计」。因此 `configs/simulation.yaml` 里的 `simulation:` 段（`duration_s` / `step_s` / `random_seed`）**当前尚未被任何代码消费**。
  - 现阶段验证算法行为，请走**单元测试**（第 3 节）或**手动 tick**（第 2 节）。
- **控制接口层**（GB/T 20999）与 **PLC / 仿真器接入**尚未实现，`PhaseDecision` 暂不落到真实设备。
