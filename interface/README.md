# interface/ —— 接口协议层

> **⚠️ 降级说明**：`sumo/` 下的 4 向路口桥接是早期演示，现已被
> [`panan/scripts/panan_bridge.py`](../panan/scripts/panan_bridge.py)（磐安县真实路网、
> 任意拓扑、复用同一套逻辑决策层）取代为**正式维护的 SUMO 桥接**。
> `run_simulation.py` 仅作最小示例参考，新增仿真一律以 `panan/` 为准。

决策层（`src/traffic_control/`）与外部世界（信号机、仿真器、指挥中心）之间的接缝。
决策层内核只输出 `PhaseDecision`（建议的下一相位，**软请求**）；本目录负责把它落到具体设备/仿真。

## 目录

```
interface/
├── hikvision/                 # 海康 XHJ-CW-GA-HK3 信号机控制接口层（软请求）
│   ├── protocol.py            # GB/T 20999-2017 帧编解码（CRC-16 + HDLC 转义）
│   ├── adapter.py             # 相位指示 / 状态查询 / 接管摘除命令 + 状态解析
│   ├── transports.py          # SerialTransport / TcpTransport / NullTransport
│   ├── heartbeat.py           # 心跳轮询 + 断线检测
│   ├── takeover.py            # 接管 / 摘除状态
│   ├── controller_link.py     # 顶层串联：决策层 → 映射 → 协议 → 传输
│   ├── phase_mapping.yaml     # 相位编号映射（占位，待填）：phase_id ↔ 海康相位编号
│   ├── timing_alignment.md    # 配时对齐：我方 min/max 绿/黄灯/全红 ↔ 信号机实际编程值
│   └── README.md              # 通信方式 + 接线 + RK3588 部署 + 待确认清单
│
└── sumo/                      # SUMO 仿真台（早期演示，已降级）
    ├── build.sh               # 用 netconvert 生成路网
    ├── run_simulation.py      # TraCI 驱动：感知适配 + 控制器 + 软相位请求
    ├── network.*.xml          # 4 向路口（节点/边/连接）
    ├── demand.rou.xml         # 车流（直行 + 左转）
    ├── trafficlight.add.xml   # 信号灯（运行时由脚本控制）
    └── intersection.sumocfg
```

## 软请求的边界（务必记住）

- 本目录**只发相位指示，绝不发灯色、绝不发配时值**（灯色与配时是信号机/审核配置的职责）。
- 决策层内核对信号机是「推荐者」；信号机是「安全裁决者」；指挥中心是「最高接管者」。
- 真实接入的第一步永远是**只读**（影子运行），拿到对比报告后再开软请求下发。
