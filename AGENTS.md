# AGENTS.md

智能交通信号控制系统·**逻辑决策层**（Python 3.11 / uv）。安全关键：改动可能影响真实路口的信号配时。

## 每次改动的工作循环

1. **读权威设计** → `docs/decision-algorithm-design.md`（算法定义、安全红线、模块状态）。
2. **跑基线** → `uv run pytest -q`，确认全绿。
3. **小步改** → 每个行为改动配一个对应测试。
4. **收尾** → 再跑 `uv run pytest -q`。

完成判据：`uv run pytest -q` 全绿，且新增行为有测试覆盖；不满足就不算完成。

## 不可破坏的不变量

- **score 无灯色权限**：评分只在可行相位集合内排序，灯色由信号机执行。
- **硬约束优先**：最小绿、最大绿、P0 等待截止、P1 下游拥堵、清空时序由代码强制，分数不得抵消。
- **配时可追溯**：min/max 绿、黄灯、全红来自审核配置，运行时不臆造。
- **数据缺失按未知处理**：观测超时或下游无返回，不等于「没车」或「畅通」。
- **优先级 P0 > P1 > P2 > P3**：由 `ScoreConfig` 的不等式守卫在加载期强制。

## 使用决策层

```python
from traffic_control.decision_layer import DecisionLayer

layer = DecisionLayer.from_yaml("configs/simulation.yaml")
result = layer.tick(now, state, observations, downstream_occupancy)

result.decision.selected_phase_id   # 建议的下一相位
result.decision.reason              # 排查问题的第一入口
result.degradation, result.feedback # 调用方据此决定降级 / 故障
```

全部可调参数一次取全：`layer.parameters`。

## 文档指针（按分支取用）

- 改评分／状态机／安全约束 → `docs/decision-algorithm-design.md`
- 接感知层／改车道 ID／改输入格式 → `docs/perception-interface.md`
- 调参数 → `docs/parameter-reference.md`
- 复现环境／复现某个行为／排障 → `docs/debugging-guide.md`
