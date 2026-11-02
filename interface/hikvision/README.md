# Hikvision XHJ-CW-GA-HK3 控制接口层

RK3588 边缘机 ↔ 海康 XHJ-CW-GA-HK3 智能交通信号机的控制接口（**软请求**）。

## 通信方式

- **协议**：GB/T 20999-2017《交通信号控制机与上位机间的数据通信协议》
  （数据链路层引用 GB/T 17547-1998 HDLC 高级数据链路控制规程）。
- **物理层**（二选一）：
  - RS-232-C：GND / TX / RX，1 起始位 + 8 数据位 + 1 结束位；校验位/波特率
    待确认（`SerialTransport` 默认无校验 `parity="none"`，按海康官方文档调整）。
  - 以太网口：IEEE 802.2/3，TCP 连接。
- **主从**：RK3588 为主（上位机），信号机为从。
- **帧结构**：`0x7E FLAG + 地址(1B) + 控制(1B) + 命令/数据 + CRC-16/CCITT + 0x7E FLAG`，
  帧内字节转义 `0x7D ^ 0x20`（见 `protocol.py`）。

## 软请求边界（重要）

本接口**只发相位指示**（「请切换到相位 N」），**绝不直接下发灯色或配时**。
最小绿 / 最大绿 / 黄灯 / 全红 / 安全联锁全部由信号机内部裁决。信号机可拒绝或
推迟相位切换；指挥中心可随时摘除远程控制权（本地优先）。

## 模块

| 文件 | 职责 |
|---|---|
| `protocol.py` | 帧编解码、CRC-16、字节转义 |
| `adapter.py` | 相位指示 / 状态查询 / 接管摘除命令 + 状态解析 |
| `transports.py` | `SerialTransport` / `TcpTransport` / `NullTransport` |
| `heartbeat.py` | 心跳轮询 + 断线检测 |
| `takeover.py` | 接管 / 摘除状态 |
| `controller_link.py` | 顶层串联：决策层 → 映射 → 协议 → 传输 |
| `phase_mapping.yaml` | 相位映射（占位） |
| `timing_alignment.md` | 配时对齐说明 |

## 最小用法

```python
from interface.hikvision.controller_link import ControllerLink
from interface.hikvision.protocol import ProtocolConfig
from interface.hikvision.transports import SerialTransport
import yaml

mapping = yaml.safe_load(open("interface/hikvision/phase_mapping.yaml"))

link = ControllerLink(
    transport=SerialTransport(port="/dev/ttyS0", baudrate=9600),
    protocol=ProtocolConfig(address=mapping["controller_address"]),
    phase_mapping=mapping["phases"],
    special_phases=mapping["special"],
)
link.open()
link.request_takeover()

# 决策层每个 tick 之后：
link.request_phase(
    decision.selected_phase_id,
    reason=decision.reason,
    all_red_required="all_red_required" in decision.constraints_applied,
)

status = link.poll()   # 心跳 + 状态查询

link.request_detach()
link.close()
```

## RK3588 部署

- 系统：ARM64 Linux（Ubuntu/Debian），Python 3.11 + uv。
- 串口：`/dev/ttySx` 或 USB 转串口；需把运行用户加入 `dialout` 组。
- 依赖：串口需 `pyserial`；TCP 无需额外依赖。

## 待确认清单（TODO）

- [ ] 海康《XHJ-CW-GA-HK3 通信协议》字节级细节：起始符字节数、命令字、
      CRC 多项式/初值、地址域宽度、波特率。
- [ ] 相位映射表 `phase_mapping.yaml`（决策层 phase_id ↔ 信号机相位编号）。
- [ ] 特殊态编号（全红 / 黄闪 / 熄灯）。
- [ ] 配时对齐（见 `timing_alignment.md`）。
- [ ] 状态响应帧结构（当前相位 / 灯色 / 故障码）。
- [ ] TCP 帧缓冲切帧（生产用，见 `transports.TcpTransport` 的 TODO）。
