# interface/ —— 接口协议层

决策层（`src/traffic_control/`）与外部世界（信号机、仿真器、指挥中心）之间的接缝。
决策层内核只输出 `PhaseDecision`（建议的下一相位，**软请求**）；本目录负责把它落到具体设备/仿真。

## 目录

```
interface/
├── sumo/                      # SUMO 仿真台（已实现，Ubuntu 24.04 + SUMO 1.28.0）
│   ├── build.sh               # 用 netconvert 生成路网
│   ├── run_simulation.py      # TraCI 驱动：感知适配 + 控制器 + 软相位请求
│   ├── network.*.xml          # 4 向路口（节点/边/连接）
│   ├── demand.rou.xml         # 车流（直行 + 左转）
│   ├── trafficlight.add.xml   # 信号灯（运行时由脚本控制）
│   └── intersection.sumocfg
│
└── （待交警配合后实现，暂不写）
    ├── phase_mapping.yaml     # 相位编号映射：phase_id ↔ 海康相位编号
    ├── timing_alignment.md    # 配时对齐：我方 min/max 绿/黄灯/全红 ↔ 信号机实际编程值
    ├── protocol_adapter.py    # 通讯协议适配器：PhaseDecision → GB/T 20999 / 海康 SDK 报文
    ├── heartbeat.py           # 心跳与回退：断线检测 + 干净摘除
    └── takeover.py            # 回退/接管开关：指挥中心一键切断，信号机回本地方案
```

## 软请求的边界（务必记住）

- 本目录**只发相位指示，绝不发灯色、绝不发配时值**（灯色与配时是信号机/审核配置的职责）。
- 决策层内核对信号机是「推荐者」；信号机是「安全裁决者」；指挥中心是「最高接管者」。
- 真实接入的第一步永远是**只读**（影子运行），拿到对比报告后再开软请求下发。
