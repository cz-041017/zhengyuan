# 调试与复现手册（含一键仿真教程）

> 面向：本项目的开发者 / 联调同事 / 演示人员。
> 目标：**从零复现环境 → 跑通决策层 → 复现各类算法行为 → 一键开启仿真演示 → 排查常见故障**。
> 相关文档：算法设计 [`decision-algorithm-design.md`](decision-algorithm-design.md)　·　可调参数 [`parameter-reference.md`](parameter-reference.md)　·　感知层接口 [`perception-interface.md`](perception-interface.md)

---

## 1. 环境复现（从零开始，按步骤做）

### 1.1 前置要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows / Linux / macOS 均可 | 仿真台（第 5 节）建议在 Linux 或 Windows 桌面 |
| Python | **3.11**（由 uv 自动安装，无需手动装） | 逻辑层依赖 |
| git | 任意近期版本 | 拉代码 |
| uv | 必需 | 依赖与 Python 版本由它管理 |
| SUMO | **仅在跑仿真台时需要**（第 5 节） | 逻辑层单元测试不需要 SUMO |

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

### 1.3 验证复现成功（三步）

```powershell
uv run pytest -q
# 期望输出：122 passed

uv run python --version
# 期望：Python 3.11.x

uv run smart-traffic-control
# 期望：Smart Traffic Control: decision engine ready.
```

三项都通过，说明环境与代码完全复现。

---

## 2. 跑一次决策（最小示例）

> **推荐用门面 `DecisionLayer`**（内部串起 排队估计 → 数据新鲜度/降级 → 保守等待 → 溢流滞回 → 状态机 → 执行反馈）：
>
> ```python
> from traffic_control.decision_layer import DecisionLayer
> from traffic_control.models import ControllerMode, ControllerState, VehicleObservation
>
> layer = DecisionLayer.from_yaml("configs/simulation.yaml")
> p = layer.parameters          # 全部可调参数一次取全（TunableParameters）
> result = layer.tick(
>     now=120.0,
>     state=ControllerState(mode=ControllerMode.NORMAL, current_phase_id="P_MT", phase_started_at=0.0),
>     observations=[VehicleObservation(track_id=1, lane_id="west_through", observed_at=0.0, speed_mps=0.0)],
>     downstream_occupancy={"east_through": 0.9},   # None 表示未知，按拥堵处理
> )
> print(result.decision.selected_phase_id, result.degradation, result.feedback)
> ```
>
> 下面是最小手动 tick 版本（适合只关注状态机时）：

```python
from traffic_control.config_loader import load_config
from traffic_control.runtime import build_runtime
from traffic_control.models import ControllerMode, ControllerState
from traffic_control.state_machine import PhaseDemand

# 1) 加载配置并装配决策层
runtime = build_runtime(load_config("configs/simulation.yaml"))

# 2) 当前控制器状态（真实部署时来自信号机反馈）
state = ControllerState(
    mode=ControllerMode.NORMAL,
    current_phase_id="P_MT",
    phase_started_at=0.0,
)

# 3) 各相位的聚合需求（真实部署时由感知数据聚合而来）
demands = {
    "P_MT": PhaseDemand(green_wave_active=True),
    "P_ML": PhaseDemand(vehicle_count=3, oldest_wait_s=20.0),
    "P_E":  PhaseDemand(vehicle_count=5, oldest_wait_s=40.0),
    "P_W":  PhaseDemand(vehicle_count=6, oldest_wait_s=118.0),
}

# 4) 决策
decision = runtime.state_machine.tick(state, 12.0, demands)
print("下一步相位：", decision.selected_phase_id)
print("原因：", decision.reason)
print("各相位得分：", decision.scores)
print("应用的约束：", decision.constraints_applied)
print("可行：", decision.feasible)
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
| `invalid_phase_start_time` | 相位开始时间晚于当前时间（状态时间戳非法） |

---

## 3. 复现各类算法行为（按测试用例逐条复现）

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

---

## 4. 调参与验证（按步骤）

1. **改参数**：编辑 [`configs/`](../configs/) 下的 YAML（参数含义与建议见[参数手册](parameter-reference.md)）；
2. **改完立即验证**：`uv run pytest -q`（配置校验会拦住非法值，例如优先级顺序、`o_low ≥ o_high`、`min_green > max_green`）；
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

## 5. 一键开启仿真（可视化演示）

> 这一节是**给甲方演示 / 现场展示**用的完整教程，按步骤做即可。

### 5.1 前置条件（只需装一次）

| 项 | 要求 | 检查命令 |
|---|---|---|
| SUMO | 已安装（含 `sumo-gui`、`netconvert`、`duarouter`） | `sumo --version` |
| SUMO Python 绑定 | 工具目录（默认 `/usr/share/sumo/tools`） | `ls /usr/share/sumo/tools/sumolib` |
| 项目依赖 | 已 `uv sync`（或仓库自带 `.venv`） | `uv run pytest -q`（122 passed） |
| 路网 | `panan/network/panan.net.xml`（磐安县手绘路网） | 已随仓库提供 |

> 如果 SUMO 装在其他路径，用环境变量指定：`export SUMO_TOOLS=/你的/sumo/tools`。
>
> **平台说明**：一键脚本（`run_demo.sh` / `run_eval.sh`）是 **bash 脚本**，依赖 Linux 路径
> （`.venv/bin/python`、`/usr/share/sumo/tools`、`pkill`）。Windows 下请用 **WSL** 或 **Git Bash**，
> 并确认 SUMO 的 Linux 版（或 WSL 内的 SUMO）可用；纯 Windows 原生 PowerShell 下这些脚本不能直接运行。

### 5.2 一键运行（最常用）

```bash
cd zhengyuan
bash panan/scripts/run_demo.sh
```

**它会自动完成：**

1. 检查需求文件（`panan/demand/panan_routes.rou.xml`），
   不存在或**路网比需求新**（你刚用 netedit 改过路网）→ 自动重新生成需求；
2. 打开 `sumo-gui`，用**逻辑决策层实时控制 43 个信号灯路口**；
3. 长时间运行（默认 86400s 仿真时间），**随时可关闭窗口停止**；
   > 注意：需求文件默认只生成到 **3600s**（`randomTrips -e 3600`），所以 3600s 之后路网会逐渐没车。
   > 若要长跑持续有车，把 `run_demo.sh` / `run_eval.sh` 里的 `-e 3600`（以及 `gen_fengqing_flow.py --end`）调大。
4. 窗口里可**鼠标滚轮放大 / 拖动**，逐个路口查看红绿灯切换和排队。

### 5.3 一键脚本的各种用法

| 命令 | 效果 |
|---|---|
| `bash panan/scripts/run_demo.sh` | 实时窗口，**自适应决策层**控灯（默认） |
| `bash panan/scripts/run_demo.sh fixed` | 实时窗口，**固定配时**对照（30s 轮换） |
| `bash panan/scripts/run_demo.sh gif` | 生成 GIF 动画，输出 `panan/results/demo.gif`（**仍需 sumo-gui 能启动**：无显示器时请用 `xvfb-run` 或确保 `DISPLAY` 可用） |
| `bash panan/scripts/run_demo.sh gif fixed` | 固定配时的 GIF |
| `bash panan/scripts/run_eval.sh adaptive` | **指标评估**（无界面），4 项指标追加到 `panan/results/metrics.csv` |
| `bash panan/scripts/run_eval.sh fixed` | 固定配时指标评估 |
| `bash panan/scripts/run_eval.sh adaptive 3600 regen` | 强制重新生成需求再评估 |

### 5.4 演示参数怎么调

| 想调整的 | 改哪里 | 说明 |
|---|---|---|
| **仿真跑多快**（时间流速） | `run_demo.sh` 的 `DELAY` | 默认 250（约 4 倍速）；500=2 倍速；1000=1 倍速（真实时间） |
| **跑多久** | `run_demo.sh` 的 `END` | 实时窗口默认 86400（手动关窗停止） |
| **整体车流密度** | `run_demo.sh` 里 `randomTrips --period` | 1.2 密 / 1.8 中等（默认）/ 2.5 稀疏 |
| **奉庆线车流** | `run_demo.sh` 里 `--count`（常规）/ `--to-e10`（往 E10 方向） | 脚本传参 300 / 250（`gen_fengqing_flow.py` 自身默认是 500 / 400） |
| **车辆起步快慢** | `panan/config/vtypes.add.xml` | `accel`（加速度）、`sigma`（反应延迟）、`tau`（跟车时距） |
| **相位检查** | `PYTHONPATH=/usr/share/sumo/tools .venv/bin/python panan/scripts/inspect_phases.py` | 打印每个路口的相位分组，标出被合并的路口（需 sumolib，故要设 PYTHONPATH） |

### 5.5 指标评估（给甲方看数据）

```bash
bash panan/scripts/run_eval.sh adaptive
```

每跑一次，自动把一行结果追加到 `panan/results/metrics.csv`：

| 列 | 含义 |
|---|---|
| `avg_delay_s` | **平均车均延误**（每辆车平均多花了多少秒） |
| `avg_queue_veh` | **平均排队长度**（信号灯进口道平均停着几辆车） |
| `capacity_veh_per_h` | **通行能力**（实测每小时通过多少辆） |
| `conflicts` | **冲突/碰撞累计值**（每仿真步累加 `getCollidingVehiclesNumber()`，即"涉碰撞车辆数"之和，不是去重后的碰撞次数；0 表示全程无碰撞） |

### 5.6 常见演示问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 打开窗口后车辆不动 | 需求文件缺失 / 路网被改过 | 删掉 `panan/demand/panan_routes.rou.xml` 重跑，或加 `regen` |
| 车辆卡在某个路口前不动 | 改过路网（拆分/合并边）但需求没更新 | 同上（需求与路网边长不匹配） |
| GIF 生成失败 | 无图形环境 | 用 `run_demo.sh gif`（离屏），不要用实时窗口 |
| 窗口关不掉 / 卡住 | 无显示器时 sumo-gui 阻塞 | 用 `gif` 模式，或 `pkill -9 -x sumo-gui` |

---

## 6. 常见问题排查（Troubleshooting）

### 6.1 命令一执行就崩溃，退出码 `0xC0000142`

- **现象**：任何命令都启动失败（DLL 初始化失败），连 `git --version` 都跑不了。
- **原因**：**运行环境处于受限沙箱模式**（不是代码问题，也不是 git/uv 装坏了）。
- **处理**：把当前会话 / 终端切到**完全权限（full access）**模式即可恢复正常。若在 CI 或容器里遇到，检查是否有安全策略限制进程创建。

### 6.2 `git` 或 `uv` 提示「无法将...识别为 cmdlet/命令」

- **原因**：新起的 PowerShell 进程没有合并「用户级 PATH」，而 uv 装在 `%USERPROFILE%\.local\bin`。
- **处理**（当前会话临时修复）：

```powershell
$env:PATH = [Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User')
```

- **永久修复**：重开一个终端（新终端会读到已写入的用户 PATH）。

### 6.3 无法运行 `.ps1` 脚本（"禁止运行脚本"）

- **现象**：`无法加载文件 ... 因为在此系统上禁止运行脚本`。
- **处理**：临时绕过或调整当前用户策略：

```powershell
# 方案一：单次绕过
powershell -NoProfile -ExecutionPolicy Bypass -File .\some-script.ps1

# 方案二：调整当前用户策略（推荐 RemoteSigned）
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 6.4 `uv sync` 报依赖与 `uv.lock` 不一致

```powershell
uv sync            # 按锁文件对齐
uv lock --upgrade  # 仅在确认要升级依赖时使用（会改动 uv.lock，需 review）
```

### 6.5 测试失败怎么定位

```powershell
uv run pytest -q                       # 先看总体
uv run pytest -x -vv                   # 遇到第一个失败即停，显示详细
uv run pytest tests/test_state_machine.py::用例名 -vv   # 精确定位
```

多数失败来自**配置被改坏**（`ConfigError` 会在信息里指出具体字段），先核对 `configs/*.yaml`。

### 6.6 Git 提示 `LF will be replaced by CRLF`

Windows 下 `core.autocrlf=true` 的正常提示，**不影响内容**，可忽略。

---

## 7. 提交与发布

```powershell
git add -A
git commit -m "docs: ..."        # 或 feat/fix
git push origin main
```

- **首次推送需要 GitHub 凭据**（Git Credential Manager 会引导登录）；
- 提交前建议先 `uv run pytest -q` 确认全绿；
- `.venv`、`.pytest_cache`、`__pycache__` 已被 [`.gitignore`](../.gitignore) 排除，不会误入库。

---

## 8. 尚未实现（调试时请注意）

- **控制接口层**（GB/T 20999）与 **PLC / 仿真器接入**尚未实现，`PhaseDecision` 暂不落到真实设备。
- `configs/simulation.yaml` 里的 `simulation:` 段（`duration_s` / `step_s` / `random_seed`）是**给未来仿真台预留**的字段；
  当前磐安县仿真台由 `panan/scripts/panan_bridge.py` 驱动，参数见第 5.4 节与[参数手册](parameter-reference.md)。
