# FocusFlow — UNO Q ↔ Windows BLE 通信协议

## 文档信息

| 项目 | 内容 |
|---|---|
| **协议名称** | FocusFlow BLE Communication Protocol |
| **协议版本** | v1.0 |
| **适用项目** | FocusFlow Lite — 智能专注度辅助系统 |
| **通信双方** | Windows 端（PyQt5 GUI）↔ Arduino UNO Q（Linux 侧） |
| **传输层** | BLE 5.0（ATT MTU 推荐协商到 ≥ 247 字节，应用 JSON ≤ 240 字节） |
| **应用层格式** | JSON over GATT（UTF-8） |
| **撰写日期** | 2026-07-21 |

---

## 目录

1. 概述
2. 系统角色与拓扑
3. GATT 服务定义
4. 消息通用格式
5. 消息类型详细定义
6. 状态定义
7. 休息控制流程
8. 错误处理与重连
9. 性能估算
10. 实现示例
11. 测试用例
12. 协议变更记录

---

## 1. 概述

本协议定义了 **Windows 端** 与 **Arduino UNO Q（Linux 侧）** 之间的 BLE 双向通信规范。Windows 端负责采集头部姿态和屏幕内容，并提供用户交互界面；UNO Q 端负责融合脑电信号、做决策推理、控制手环与 TFT 彩屏。两端通过本协议交换数据和控制指令。

**设计目标**：
- **简单**：使用 JSON 格式，调试方便
- **可靠**：消息序号、重连机制、错误反馈
- **可扩展**：消息类型 + Payload 模式，新增功能无需改协议
- **低开销**：典型消息 80-150 字节，5Hz 频率下带宽充足

**不在本协议范围内**：
- UNO Q → ESP32 手环的单向通信（使用另一套简化协议）
- 脑电头环与 UNO Q 的通信（脑电头环自带的私有协议）

---

## 2. 系统角色与拓扑

```
┌─────────────┐  BLE 双向  ┌────────────────┐  BLE 单向  ┌──────────┐
│  Windows 端 │◀─────────▶│  UNO Q Linux   │───────────▶│ ESP32 手环│
│  (GATT      │  JSON over │  (GATT Server) │   振动指令  │ (Server) │
│   Client)   │   GATT     │                │            │          │
└─────────────┘            └────────────────┘            └──────────┘
       │                           │
       │  MediaPipe                │  Python BLE (bluez/bleak)
       │  截图+API                 │  ONNX 推理
       │  PyQt5 GUI                │  通过 RPC
       │                           ▼
       │                  ┌──────────────┐
       └─────────────────▶│ UNO Q STM32  │
                          │ (TFT 彩屏)   │
                          └──────────────┘
```

**角色分配**：

| 角色 | 设备 | 任务 |
|---|---|---|
| GATT Server | UNO Q Linux | 提供 RX Characteristic（被 Windows 写入）和 TX Characteristic（Notify 给 Windows） |
| GATT Client | Windows 端 | 写入 RX、订阅 TX Notify |

---

## 3. GATT 服务定义

### 3.1 Service

| 字段 | 值 |
|---|---|
| **Service UUID** | `19B10000-E8F2-537E-4F6C-D104768A1214` |
| **说明** | 参考 Nordic UART Service（NUS）模式，业内最常用、最稳定的 BLE 串口透传模式 |

### 3.2 Characteristic

| 名称 | UUID | 属性 | 方向 | 长度上限 | 说明 |
|---|---|---|---|---|---|
| **RX** | `19B10001-E8F2-537E-4F6C-D104768A1214` | Write, WriteWithoutResponse | Windows → UNO Q | 244 B | Windows 写入上行数据（眼动、屏幕、休息指令） |
| **TX** | `19B10002-E8F2-537E-4F6C-D104768A1214` | Notify, Read | UNO Q → Windows | 244 B | UNO Q 推送下行数据（状态、倒计时、设备状态） |

### 3.3 协商 MTU

- 默认 ATT MTU：23 字节（不够用）
- 推荐协商到 **247 字节 ATT MTU**，对应单个 Characteristic Value 最大 244 字节
- 应用层 JSON 上限固定为 **240 字节**，预留少量空间给 BLE/驱动实现；不要将 JSON 上限放宽到 280 字节，因为单个 Notify 无法承载该长度
- UNO Q Linux 侧需开启 MTU 协商（bleak 默认会协商）
- Windows 端系统驱动自动协商

---

## 4. 消息通用格式

每条消息均为 **UTF-8 编码的 JSON 字符串**。

### 4.1 顶层结构

```json
{
  "type": "<message_type>",
  "seq":  <uint32>,
  "ts":   <uint32>,
  "data": { ... }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 消息类型标识符，见 §5 |
| `seq` | uint32 | 是 | 消息序号，发送方从 0 开始自增，到 2^32 后归零 |
| `ts` | uint32 | 是 | Unix 时间戳（秒） |
| `data` | object | 视类型 | 消息载荷，结构由 `type` 决定 |

### 4.2 通用规则

- **UTF-8 编码**：所有字符串字段使用 UTF-8
- **浮点数**：保留 2 位小数（避免过长字符串）
- **时间戳**：`ts` 字段统一使用 Unix 秒
- **空字段**：可省略，缺省为 null 或 0
- **未知字段**：接收方应忽略（向前兼容）
- **JSON 长度**：单条消息最大 240 字节（紧凑 UTF-8 JSON；单个 Characteristic Value 的实际上限为 244 字节）
- **`sync_response` 压缩**：`rest_countdown` 可以省略；嵌套 `device_status` 可以只携带部分字段。完整设备状态应通过独立的 `device_status` 下行消息发送。这样 `sync_response` 才能在单条 Notify 内传输

### 4.3 示例

```json
{"type":"eye_data","seq":1234,"ts":1700000000,"data":{"yaw":5.2,"pitch":-3.1,"is_focused":1,"state_duration":2.5,"confidence":0.95}}
```

---

## 5. 消息类型详细定义

### 5.1 消息类型一览

#### 上行（Windows → UNO Q）

| type | 名称 | 频率 | 优先级 |
|---|---|---|---|
| `eye_data` | 头部姿态数据 | 5 Hz | 高 |
| `screen_data` | 屏幕内容数据 | 0.5 Hz | 中 |
| `rest_command` | 休息控制指令 | 事件触发 | 高 |
| `heartbeat` | 心跳 | 1/10 s | 低 |
| `sync_request` | 同步请求 | 事件触发 | 中 |

#### 下行（UNO Q → Windows）

| type | 名称 | 频率 | 优先级 |
|---|---|---|---|
| `state_update` | 状态更新 | 事件触发 | 高 |
| `focus_score` | 专注度分数 | 1 Hz | 中 |
| `rest_countdown` | 休息倒计时 | 1/10 s（仅休息中） | 中 |
| `display_content` | TFT 彩屏当前内容 | 1 Hz | 低 |
| `device_status` | 设备连接状态 | 事件触发 | 中 |
| `vibration_feedback` | 振动反馈状态 | 事件触发 | 低 |
| `heartbeat` | 心跳响应 | 1/10 s | 低 |
| `sync_response` | 同步响应 | 事件触发 | 中 |
| `error` | 错误信息 | 事件触发 | 高 |

---

### 5.2 上行消息详细定义

#### 5.2.1 `eye_data` — 头部姿态数据

**触发**：Windows 端每 200 ms 检测一次头部姿态，每秒聚合 5 次发送

```json
{
  "type": "eye_data",
  "seq": 1001,
  "ts": 1700000000,
  "data": {
    "yaw": 5.2,
    "pitch": -3.1,
    "is_focused": 1,
    "state_duration": 2.5,
    "confidence": 0.95
  }
}
```

**字段约束**：
- `yaw`：float，[-180, 180]，头部水平偏角
- `pitch`：float，[-90, 90]，头部俯仰角
- `is_focused`：int，[0|1]，二分类结果
- `state_duration`：float，[0, +∞)，当前状态持续时间（秒）
- `confidence`：float，[0, 1]，MediaPipe 检测置信度

**典型大小**：~100 字节

---

#### 5.2.2 `screen_data` — 屏幕内容数据

**触发**：minimax vision API 返回结果后立即发送

```json
{
  "type": "screen_data",
  "seq": 1002,
  "ts": 1700000060,
  "data": {
    "state": "focused",
    "confidence": 0.92,
    "app": "VSCode",
    "category": "work"
  }
}
```

**字段约束**：
- `state`：enum
  - `focused` — 专注工作
  - `distracted` — 一般走神（白名单内但非学习）
  - `procrastinating` — 摸鱼（娱乐/社交/游戏）
  - `away` — 离开（黑屏、锁屏）
- `confidence`：float，[0, 1]
- `app`：string，识别到的应用名
- `category`：enum（work | study | entertainment | social | game | other）
- API 失败时可省略 `app` 和 `category`，但必须保留 `state` 与 `confidence`

**典型大小**：~120 字节

---

#### 5.2.3 `rest_command` — 休息控制指令

**触发**：用户在 GUI 点击休息按钮 / 番茄钟触发 / 强制休息触发

```json
{
  "type": "rest_command",
  "seq": 1003,
  "ts": 1700000120,
  "data": {
    "action": "start",
    "duration": 300,
    "reason": "manual"
  }
}
```

**action 详解**：

| 值 | 含义 | 必填字段 | UNO Q 行为 |
|---|---|---|---|
| `start` | 开始休息 | `duration`, `reason` | 切换到 resting 状态，启动倒计时 |
| `stop` | 结束休息 | （无） | 立即退出 resting 状态，恢复推理 |
| `extend` | 延长休息 | `duration` | 在当前休息上叠加 duration |
| `query` | 查询休息状态 | （无） | 立即发送一次 `rest_countdown` |

**reason 详解**：

| 值 | 含义 |
|---|---|
| `manual` | 用户手动触发 |
| `auto_pomodoro` | 番茄钟自动触发 |
| `auto_focus` | 检测到持续专注后自动触发 |
| `auto_long_session` | 连续学习 1 小时强制休息触发 |

**典型大小**：~110 字节

---

#### 5.2.4 `heartbeat` — 心跳

**触发**：Windows 端每 10 秒发送一次

```json
{
  "type": "heartbeat",
  "seq": 1004,
  "ts": 1700000180,
  "data": {
    "uptime": 3600
  }
}
```

UNO Q 收到后立即回 `heartbeat`（§5.3.7）。

---

#### 5.2.5 `sync_request` — 同步请求

**触发**：Windows 端启动时 / 重连后 / 用户点击"同步状态"按钮

```json
{
  "type": "sync_request",
  "seq": 1005,
  "ts": 1700000240,
  "data": {
    "fields": ["state", "device_status", "rest_countdown"]
  }
}
```

`fields` 数组可选，指定需要同步的字段；省略则返回全部。

**响应**：UNO Q 立即发送 `sync_response`（§5.3.8）。

---

### 5.3 下行消息详细定义

#### 5.3.1 `state_update` — 状态更新

**触发**：UNO Q 状态机发生状态切换时

```json
{
  "type": "state_update",
  "seq": 2001,
  "ts": 1700000005,
  "data": {
    "state": "procrastinating",
    "focus_score": 32,
    "prev_state": "focused",
    "duration_in_state": 8,
    "triggered_feedback": "vibrate_double"
  }
}
```

**triggered_feedback 枚举**：

| 值 | 含义 |
|---|---|
| `none` | 无反馈 |
| `vibrate_short` | 短振 1 次 |
| `vibrate_double` | 双振 2 次 |
| `vibrate_continuous` | 持续轻振 3 次 |
| `notification` | 笔记本弹窗 |
| `tft_alert` | TFT 彩屏告警界面 |

**典型大小**：~180 字节

---

#### 5.3.2 `focus_score` — 专注度分数

**触发**：每 1 秒定时发送

```json
{
  "type": "focus_score",
  "seq": 2002,
  "ts": 1700000010,
  "data": {
    "score": 82,
    "state": "focused"
  }
}
```

**注意**：状态未变化时也定期发送 score，便于 GUI 实时刷新曲线。

---

#### 5.3.3 `rest_countdown` — 休息倒计时

**触发**：处于 resting 状态时，每 10 秒发送一次；最后 30 秒每秒发送

```json
{
  "type": "rest_countdown",
  "seq": 2003,
  "ts": 1700000200,
  "data": {
    "remaining": 234,
    "total": 300,
    "state": "resting",
    "phase": "middle"
  }
}
```

**phase 详解**：

| 值 | 含义 | GUI 表现 |
|---|---|---|
| `start` | 刚开始（remaining > total × 0.8） | 显示"休息开始" |
| `middle` | 中间段 | 正常倒计时显示 |
| `ending` | 即将结束（remaining < 30s） | 显示"即将返回工作" + 颜色变化 |

---

#### 5.3.4 `display_content` — TFT 彩屏当前内容

**触发**：每 1 秒发送（与 UNO Q 更新 TFT 的频率一致）

```json
{
  "type": "display_content",
  "seq": 2004,
  "ts": 1700000030,
  "data": {
    "line1": "FocusFlow Lite v2.0",
    "line2": "状态: 高度专注",
    "line3": "专注度 82%",
    "line4": "学习中 18:42/25:00"
  }
}
```

**说明**：Windows GUI 可选地同步显示一个"TFT 镜像"区域，让用户能远程看到小屏内容。

---

#### 5.3.5 `device_status` — 设备连接状态

**触发**：任一外设连接/断开时 / 每 30 秒心跳

```json
{
  "type": "device_status",
  "seq": 2005,
  "ts": 1700000040,
  "data": {
    "eeg_connected": true,
    "eeg_battery": 85,
    "wristband_connected": true,
    "wristband_battery": 78,
    "tft_display": "running"
  }
}
```

**字段**：
- `eeg_connected`：bool，脑电头环是否连接
- `eeg_battery`：int，[0, 100]，百分比（设备不支持时为 -1）
- `wristband_connected`：bool，手环是否连接
- `wristband_battery`：int，[0, 100]，百分比
- `tft_display`：enum（running | error | updating | offline）

独立的 `device_status` 消息必须包含以上全部字段；但在 `sync_response.data.device_status` 中允许使用紧凑子集，以确保整个同步响应不超过 240 字节。

---

#### 5.3.6 `vibration_feedback` — 振动反馈状态

**触发**：每次手环振动指令发出后

```json
{
  "type": "vibration_feedback",
  "seq": 2006,
  "ts": 1700000050,
  "data": {
    "mode": "double",
    "trigger": "摸鱼",
    "success": true
  }
}
```

---

#### 5.3.7 `heartbeat` — 心跳响应

**触发**：收到 Windows 端 `heartbeat` 时立即回复

```json
{
  "type": "heartbeat",
  "seq": 2007,
  "ts": 1700000185,
  "data": {
    "uptime": 3605,
    "echo_seq": 1004
  }
}
```

**说明**：`echo_seq` 字段回显收到的 seq，便于排查乱序。

---

#### 5.3.8 `sync_response` — 同步响应

**触发**：收到 `sync_request` 时立即返回

```json
{
  "type": "sync_response",
  "seq": 2008,
  "ts": 1700000245,
  "data": {
    "state": "focused",
    "focus_score": 85,
    "prev_state": "focused",
    "device_status": {
      "eeg_connected": true,
      "wristband_connected": true,
      "tft_display": "running"
    }
  }
}
```
  
  **说明**：这是 208 字节左右的紧凑同步响应。`rest_countdown` 在同步响应中可省略；`device_status` 只保留连接状态和 TFT 状态即可。完整电量字段应随后通过独立的 `device_status` 消息发送。若包含 `rest_countdown` 或完整五字段 `device_status`，可能超过 240 字节，Linux 端应省略这些字段并继续发送后续状态消息。

---

#### 5.3.9 `error` — 错误信息

**触发**：协议解析失败 / 字段缺失 / 状态非法等

```json
{
  "type": "error",
  "seq": 2009,
  "ts": 1700000070,
  "data": {
    "code": "INVALID_MSG_TYPE",
    "message": "Unknown message type: 0x99",
    "fatal": false
  }
}
```

**错误码枚举**：

| code | 含义 |
|---|---|
| `INVALID_JSON` | JSON 解析失败 |
| `INVALID_MSG_TYPE` | 未知消息类型 |
| `MISSING_FIELD` | 必填字段缺失 |
| `OUT_OF_RANGE` | 字段值超出合法范围 |
| `STATE_CONFLICT` | 状态机不允许的操作（如非 resting 时 stop） |
| `DEVICE_BUSY` | 设备正在执行其他操作 |
| `INTERNAL_ERROR` | UNO Q 内部错误 |

---

## 6. 状态定义

### 6.1 状态枚举

```python
class State:
    FOCUSED         = "focused"           # 专注
    DISTRACTED      = "distracted"        # 走神
    PROCRASTINATING = "procrastinating"   # 摸鱼
    RESTING         = "resting"           # 休息
```

### 6.2 状态转移规则（参考，不在本协议范围内）

```
[focused] ──专注度<0.3──> [distracted]
[focused] ──屏幕=procrastinating──> [procrastinating]
[distracted] ──专注度>0.7──> [focused]
[procrastinating] ──屏幕=focused──> [focused]
[any] ──rest_command(start)──> [resting]
[resting] ──rest_command(stop) 或倒计时结束──> [previous]
```

详细状态机逻辑由 UNO Q 端实现，协议只负责传输。

---

## 7. 休息控制流程

### 7.1 时序图（用户手动开始休息）

```
Windows                                UNO Q
  │                                      │
  │ 1. 用户点击"开始休息"                │
  │ 2. 暂停摄像头 + 截图                 │
  │                                      │
  │──── rest_command (start, 300) ──────▶│
  │                                      │ 3. 切换状态: resting
  │                                      │ 4. 启动倒计时
  │                                      │ 5. TFT 显示"☕ 休息中"
  │                                      │
  │◀─── state_update (resting) ──────────│ 6. 推送给 Windows
  │                                      │
  │ 7. GUI 显示休息倒计时                 │
  │                                      │
  │        (每 10 秒)                    │
  │◀─── rest_countdown ──────────────────│
  │                                      │
  │        (倒计时结束)                  │
  │                                      │ 8. 切换状态: focused
  │                                      │ 9. 通知手环振动 × 3
  │◀─── state_update (focused) ──────────│ 10. 推送给 Windows
  │                                      │
  │ 11. GUI 重新启动摄像头 + 截图         │
```

### 7.2 时序图（用户中途结束休息）

```
Windows                                UNO Q
  │                                      │
  │ 1. 用户点击"结束休息"                 │
  │                                      │
  │──── rest_command (stop) ────────────▶│
  │                                      │ 2. 停止倒计时
  │                                      │ 3. 切换状态: focused
  │                                      │ 4. 通知手环振动 × 1
  │◀─── state_update (focused) ──────────│
  │                                      │
  │ 5. GUI 重新启动摄像头 + 截图         │
```

### 7.3 算力节省策略

| 阶段 | Windows 端 | UNO Q 端 |
|---|---|---|
| 正常 | 摄像头 5Hz + 截图 30s | 融合推理 1Hz |
| **休息中** | **关闭摄像头** + **暂停截图** + **暂停 eye_data/screen_data 发送** | **暂停融合推理** + 维持倒计时 |
| 休息结束 | 重新打开摄像头 + 截图 | 恢复推理 |

**关键**：休息期间 Windows 端**停止发送** `eye_data` 和 `screen_data`，UNO Q 端在收到 `rest_command(start)` 后也跳过对这两个消息的处理。`rest_command` 仍可随时发送以退出休息。

---

## 8. 错误处理与重连

### 8.1 错误处理原则

1. **静默丢弃**：CRC/JSON 解析失败的消息直接丢弃，不发 error（避免反向风暴）
2. **可恢复错误**：发送 `error` 消息，fatal=false
3. **不可恢复错误**：发送 `error` 消息，fatal=true，Windows 端弹窗并尝试重连

### 8.2 消息去重

- 接收方记录最近收到的 `seq`
- 收到 `seq` ≤ 之前记录的 → 丢弃
- `seq` 回绕（超过 2^32）：按时间戳 `ts` 二次判断

### 8.3 心跳超时

- 任意一方 30 秒未收到对方 `heartbeat` → 标记连接异常
- 异常方主动断开并尝试重连
- Windows 端：默认每 3 秒尝试重连，单次断线最多 5 次；重新连接成功后计数清零，长期运行的主程序可配置为无限重连
- UNO Q 端：保持 advertising，等待重连

### 8.4 重连流程

```
Windows                                UNO Q
  │                                      │
  │ 1. 检测到连接断开                    │
  │ 2. 停止所有数据发送                  │
  │ 3. 启动重连循环                      │
  │                                      │
  │──── 扫描 UNO-Q-FF01 ────────────────▶│ (一直 advertise)
  │                                      │
  │◀────── 连接成功 ──────────────────────│
  │                                      │
  │──── sync_request ───────────────────▶│ 4. 请求完整状态
  │                                      │
  │◀──── sync_response ──────────────────│ 5. 恢复显示
  │                                      │
  │ 6. 恢复正常数据流                    │
```

### 8.5 BLE 连接参数（推荐）

| 参数 | 值 | 说明 |
|---|---|---|
| Connection Interval | 30 ms ~ 50 ms | 平衡延迟与功耗 |
| Peripheral Latency | 0 | 减少响应延迟 |
| Supervision Timeout | 5000 ms | 5 秒超时断开 |
| ATT MTU | 247 | 推荐协商值；对应 Characteristic Value 最大 244 字节 |

---

## 9. 性能估算

### 9.1 带宽占用

| 消息 | 频率 | 大小 | 带宽 |
|---|---|---|---|
| `eye_data` | 5 Hz | 100 B | 500 B/s |
| `screen_data` | 0.5 Hz | 120 B | 60 B/s |
| `rest_command` | 事件触发（<0.1 Hz） | 110 B | < 11 B/s |
| `state_update` | 事件触发（<0.5 Hz） | 180 B | < 90 B/s |
| `focus_score` | 1 Hz | 50 B | 50 B/s |
| `rest_countdown` | 0.1 Hz（休息中） | 80 B | 8 B/s |
| `device_status` | 0.03 Hz | 130 B | 4 B/s |
| `heartbeat` | 0.1 Hz | 50 B | 5 B/s |
| `sync_response`（紧凑） | 启动/重连事件 | 约 208 B | 事件触发 |
| **合计（正常）** | | | **~720 B/s** |
| **合计（休息中）** | | | **~65 B/s** |

`sync_response` 的 208 B 是省略 `rest_countdown`、并在嵌套 `device_status` 中只保留 `eeg_connected`、`wristband_connected`、`tft_display` 时的典型值；完整设备状态通过后续 `device_status` 消息发送，不计入稳定带宽合计。

### 9.2 延迟

| 路径 | 目标 |
|---|---|
| 端到端（Windows 发送 → UNO Q 收到） | < 50 ms |
| 状态推送（UNO Q 触发 → Windows 收到） | < 200 ms |
| 休息指令生效 | < 500 ms |

### 9.3 容量评估

- BLE 5.0 理论速率：2 Mbps
- 实际使用：不到 0.1%
- 容量充足，但协议明确禁止应用层分包；每条消息必须在 240 字节 JSON 上限内。超出上限的同步字段应省略并由后续独立状态消息补齐。

---

## 10. 实现示例

### 10.1 UNO Q Linux 侧（Python + bleak）

```python
import asyncio
import json
import time

SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214"
RX_CHAR_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"  # Windows -> UNO Q
TX_CHAR_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"  # UNO Q -> Windows


class FocusFlowBLEServer:
    def __init__(self):
        self.seq = 0
        self.tx_char = None
        self.state_machine = None
        self.eeg_buffer = []
        self.last_eye_data = None
        self.last_screen_data = None
        self.in_rest = False
        self.rest_end_time = 0
        self.start_time = time.time()

    def get_next_seq(self):
        self.seq = (self.seq + 1) % (2 ** 32)
        return self.seq

    async def send(self, msg_type: str, data: dict):
        if self.tx_char is None:
            return
        msg = {
            "type": msg_type,
            "seq": self.get_next_seq(),
            "ts": int(time.time()),
            "data": data
        }
        payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if len(payload) > 240:
            print(f"[WARN] message too long: {len(payload)} bytes")
            return
        await self.tx_char.notify(payload)

    def handle_rx(self, sender, data: bytes):
        try:
            msg = json.loads(data.decode("utf-8"))
            msg_type = msg.get("type")
            payload = msg.get("data", {})

            if msg_type == "eye_data":
                self.last_eye_data = payload
                self._try_fuse_and_decide()

            elif msg_type == "screen_data":
                self.last_screen_data = payload
                self._try_fuse_and_decide()

            elif msg_type == "rest_command":
                self._handle_rest_command(payload)

            elif msg_type == "heartbeat":
                asyncio.create_task(self.send("heartbeat", {
                    "uptime": int(time.time() - self.start_time),
                    "echo_seq": msg.get("seq")
                }))

            elif msg_type == "sync_request":
                asyncio.create_task(self._send_sync_response(payload))

        except json.JSONDecodeError:
            print(f"[ERROR] invalid JSON: {data}")
        except Exception as e:
            print(f"[ERROR] handle_rx: {e}")

    def _try_fuse_and_decide(self):
        if self.in_rest:
            return
        if self.last_eye_data is None or self.last_screen_data is None:
            return
        new_state, focus_score, feedback = self.state_machine.decide(
            self.eeg_buffer[-1] if self.eeg_buffer else None,
            self.last_eye_data,
            self.last_screen_data
        )
        if new_state != self.state_machine.current_state:
            asyncio.create_task(self.send("state_update", {
                "state": new_state,
                "focus_score": focus_score,
                "prev_state": self.state_machine.current_state,
                "duration_in_state": 0,
                "triggered_feedback": feedback
            }))
            self.state_machine.current_state = new_state

    def _handle_rest_command(self, payload):
        action = payload.get("action")
        if action == "start":
            duration = payload.get("duration", 300)
            self.in_rest = True
            self.rest_end_time = time.time() + duration
            self.last_eye_data = None
            self.last_screen_data = None
            asyncio.create_task(self.send("state_update", {
                "state": "resting",
                "focus_score": 0,
                "prev_state": "focused",
                "duration_in_state": 0,
                "triggered_feedback": "vibrate_short"
            }))
        elif action == "stop":
            self.in_rest = False
            asyncio.create_task(self.send("state_update", {
                "state": "focused",
                "focus_score": 0,
                "prev_state": "resting",
                "duration_in_state": int(time.time() - self.rest_end_time),
                "triggered_feedback": "vibrate_short"
            }))

    async def _send_sync_response(self, payload):
        await self.send("sync_response", {
            "state": "focused",
            "focus_score": 85,
            "prev_state": "focused",
            "rest_countdown": None,
            "device_status": {
                "eeg_connected": True,
                "eeg_battery": 85,
                "wristband_connected": True,
                "wristband_battery": 78,
                "tft_display": "running"
            }
        })

    async def _focus_score_loop(self):
        while True:
            await asyncio.sleep(1.0)
            if not self.in_rest:
                score = self.state_machine.get_focus_score() if self.state_machine else 0
                await self.send("focus_score", {
                    "score": score,
                    "state": self.state_machine.current_state if self.state_machine else "focused"
                })

    async def _rest_countdown_loop(self):
        while True:
            await asyncio.sleep(10.0)
            if self.in_rest:
                remaining = max(0, int(self.rest_end_time - time.time()))
                if remaining == 0:
                    self.in_rest = False
                    await self.send("state_update", {
                        "state": "focused",
                        "focus_score": 0,
                        "prev_state": "resting",
                        "duration_in_state": 0,
                        "triggered_feedback": "vibrate_continuous"
                    })
                else:
                    total = 300
                    phase = "ending" if remaining < 30 else (
                        "start" if remaining > total * 0.8 else "middle"
                    )
                    await self.send("rest_countdown", {
                        "remaining": remaining,
                        "total": total,
                        "state": "resting",
                        "phase": phase
                    })
```

### 10.2 Windows 端（Python + bleak + PyQt5）

> **实现说明（2026-07-21 更新）**：本节代码展示协议调用轮廓。实际 Windows 实现位于 `windows_ble_client.py` 和 `windows_ble_qt.py`。PyQt5 主线程不得直接调用 `asyncio.run()`；应使用 `WindowsBLEClientThread`，由该线程内部的 asyncio 事件循环提交 BLE 操作。UNO Q Linux 端只需按照本协议的 GATT、JSON 字段、序号、心跳和重连约定实现，不需要依赖 Windows 端 Python 文件。

> 以下代码块仅用于说明消息字段和处理顺序，不应直接复制为生产 Qt 代码；特别是其中的 `asyncio.run(self.send(...))` 不能从 GUI 主线程调用。可运行的 Windows 端代码和主程序调用方式以 `ble/windows_ble_client.py`、`ble/windows_ble_qt.py` 及 `ble/README_Windows.md` 为准。

```python
import asyncio
import json
import time
from PyQt5.QtCore import QThread, pyqtSignal

SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214"
RX_CHAR_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
TX_CHAR_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"


class BLEClientThread(QThread):
    state_update_signal = pyqtSignal(dict)
    focus_score_signal = pyqtSignal(int, str)
    rest_countdown_signal = pyqtSignal(int, int, str)
    device_status_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, device_address="UNO-Q-FF01"):
        super().__init__()
        self.device_address = device_address
        self.seq = 0
        self.client = None
        self.connected = False

    def get_next_seq(self):
        self.seq = (self.seq + 1) % (2 ** 32)
        return self.seq

    async def send(self, msg_type: str, data: dict):
        if not self.connected:
            return
        msg = {
            "type": msg_type,
            "seq": self.get_next_seq(),
            "ts": int(time.time()),
            "data": data
        }
        payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        await self.client.write_gatt_char(RX_CHAR_UUID, payload)

    def handle_notification(self, sender, data: bytes):
        try:
            msg = json.loads(data.decode("utf-8"))
            msg_type = msg.get("type")
            payload = msg.get("data", {})

            if msg_type == "state_update":
                self.state_update_signal.emit(payload)
            elif msg_type == "focus_score":
                self.focus_score_signal.emit(payload["score"], payload["state"])
            elif msg_type == "rest_countdown":
                self.rest_countdown_signal.emit(
                    payload["remaining"],
                    payload["total"],
                    payload["phase"]
                )
            elif msg_type == "device_status":
                self.device_status_signal.emit(payload)
            elif msg_type == "error":
                self.error_signal.emit(payload.get("message", "unknown error"))
        except Exception as e:
            print(f"[ERROR] handle_notification: {e}")

    async def run_ble(self):
        from bleak import BleakClient
        while True:
            try:
                self.client = BleakClient(self.device_address, timeout=10.0)
                await self.client.connect()
                self.connected = True
                print("[BLE] Connected to UNO Q")

                await self.client.start_notify(TX_CHAR_UUID, self.handle_notification)
                await self.send("sync_request", {"fields": ["all"]})

                asyncio.create_task(self._heartbeat_loop())

                while self.connected:
                    await asyncio.sleep(1.0)

            except Exception as e:
                print(f"[BLE] Connection error: {e}")
                self.connected = False
                await asyncio.sleep(3.0)

    async def _heartbeat_loop(self):
        while self.connected:
            await self.send("heartbeat", {"uptime": int(time.time())})
            await asyncio.sleep(10.0)

    def run(self):
        asyncio.run(self.run_ble())

    def send_eye_data(self, yaw, pitch, is_focused, state_duration, confidence):
        asyncio.run(self.send("eye_data", {
            "yaw": round(yaw, 2),
            "pitch": round(pitch, 2),
            "is_focused": is_focused,
            "state_duration": round(state_duration, 2),
            "confidence": round(confidence, 2)
        }))

    def send_screen_data(self, state, confidence, app, category):
        asyncio.run(self.send("screen_data", {
            "state": state,
            "confidence": round(confidence, 2),
            "app": app,
            "category": category
        }))

    def send_rest_command(self, action, duration=300, reason="manual"):
        asyncio.run(self.send("rest_command", {
            "action": action,
            "duration": duration,
            "reason": reason
        }))
```

### 10.3 Windows GUI 中的集成示例

> 上述示例中的 `BLEClientThread` 是简化示意。生产集成请参阅 `ble/README_Windows.md`，使用 `WindowsBLEClientThread`；它会通过线程安全的协程提交避免阻塞 Qt GUI。

```python
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ble_thread = BLEClientThread()
        self.ble_thread.state_update_signal.connect(self.on_state_update)
        self.ble_thread.focus_score_signal.connect(self.on_focus_score)
        self.ble_thread.rest_countdown_signal.connect(self.on_rest_countdown)
        self.ble_thread.start()

    def on_start_rest_clicked(self):
        self.eye_tracker.pause()
        self.screen_monitor.pause()
        self.ble_thread.send_rest_command("start", duration=300, reason="manual")

    def on_state_update(self, data):
        state = data["state"]
        if state == "resting":
            self.show_rest_panel()
        else:
            self.show_normal_panel()
            if not self.eye_tracker.is_running():
                self.eye_tracker.resume()
                self.screen_monitor.resume()

    def on_rest_countdown(self, remaining, total, phase):
        self.rest_label.setText(f"休息剩余: {remaining}s / {total}s")
        if phase == "ending":
            self.rest_label.setStyleSheet("color: red; font-weight: bold;")
```

### 10.4 Windows 实际实现与 UNO Q Linux 端的对接约定

以下内容是 Windows 客户端代码的实际行为，Linux 端应以此作为服务端实现依据：

1. **设备发现**：Windows 默认按设备名 `UNO-Q-FF01` 扫描；也支持传入 Windows BLE 地址（例如 `AA:BB:CC:DD:EE:FF`）。UNO Q 应持续广播 `UNO-Q-FF01`，否则 Windows 按默认配置无法发现设备。
2. **连接顺序**：连接成功后，Windows 先订阅 TX Characteristic 的 Notify，再自动发送 `sync_request`，其 `data` 为 `{"fields":["all"]}`。Linux 应在收到后立即返回合法的 `sync_response`。
3. **序号**：Windows 发出的第一条消息 `seq` 为 `0`，之后按 uint32 递增并在 `2^32` 后回绕。Linux 发出的下行消息使用独立的序号空间，也应从 `0` 开始递增。Windows 会丢弃重复和乱序的下行消息；回绕按 uint32 序列距离判断。
4. **消息长度**：Windows 使用无空格紧凑 JSON、UTF-8 编码，单条 JSON 最大 240 字节；不会做分包或拼包。Linux 的每次 Notify 必须发送一条完整 JSON，不能把一条消息拆成多个 Notify。不要把上限放宽到 280 字节，单个 Characteristic Value 无法承载该长度。
5. **字段校验**：Windows 会校验所有已知下行消息的必填字段、枚举和数值范围。Linux 必须发送协议中定义的字段；未知的附加字段可以保留，Windows 会忽略它们。Windows 不接受未知的下行 `type`，Linux 不应发送未在 5.1 下行列表中定义的消息类型。
6. **紧凑同步**：`sync_response` 必须优先使用紧凑形式：保留 `state`、`focus_score`、`prev_state` 和 `device_status`，省略 `rest_countdown`；嵌套 `device_status` 可只发送 `eeg_connected`、`wristband_connected`、`tft_display`。完整电量字段和倒计时由后续 `device_status` / `rest_countdown` 消息发送。完整五字段 `device_status` 加上同步外层字段约 248 字节，即使省略 `rest_countdown` 也不符合 240 字节上限。
7. **心跳**：连接并完成 Notify 订阅后，Windows 每 10 秒发送一次上行 `heartbeat`。Linux 收到后应立即返回下行 `heartbeat`，并将收到的上行 `seq` 原样放入 `data.echo_seq`。连续 30 秒没有收到 Linux 的 heartbeat 响应，Windows 会主动断开并重连。
8. **重连**：Windows 默认每 3 秒尝试一次，单次断线最多尝试 5 次；重新连接成功后计数清零。长时间运行的主程序可以设置无限重连。Linux 应在断开后继续广播，不应要求 Windows 保存旧连接状态。
9. **致命错误**：Linux 发送 `error` 且 `data.fatal=true` 时，Windows 会通知主程序并主动进入重连流程；`fatal=false` 只通知主程序，不会主动断开。
10. **休息状态**：主程序在发送 `rest_command(start)` 前暂停摄像头和屏幕采集，因此休息期间不会发送 `eye_data`、`screen_data`。Linux 仍可正常发送 `rest_countdown`、`state_update` 和 `device_status`；收到 `stop` 或倒计时结束后，Windows 主程序恢复采集。
11. **回调分发**：Windows Qt 适配层会把 `state_update`、`focus_score`、`rest_countdown`、`display_content`、`device_status`、`vibration_feedback`、`heartbeat`、`sync_response` 分别转成 Qt signal，Linux 端只需保证消息格式正确，不需要适配 Windows GUI。

协议编码器的 `allowed_types` 参数用于区分方向：Windows 上行默认使用 `UPLINK_TYPES`；Linux 服务端编码下行 Notify 时应使用 `DOWNLINK_TYPES`，例如 `encode_message("state_update", data, seq, allowed_types=DOWNLINK_TYPES)`，或者使用等价的 `encode_downlink(...)` 包装函数。

---

## 11. 测试用例

### 11.1 单元测试用例

| ID | 用例 | 期望结果 |
|---|---|---|
| TC-01 | 发送 `eye_data`，所有字段合法 | UNO Q 收到并更新 last_eye_data |
| TC-02 | 发送 `eye_data`，`yaw` 超范围 | UNO Q 忽略 / 发送 error |
| TC-03 | 发送 `rest_command(action=start, duration=300)` | UNO Q 切换为 resting 状态，推送 state_update |
| TC-04 | 发送 `rest_command(action=stop)`（非 resting 状态） | UNO Q 忽略或发送 STATE_CONFLICT error |
| TC-05 | 发送畸形 JSON | UNO Q 静默丢弃 |
| TC-06 | 发送未知 type | UNO Q 发送 INVALID_MSG_TYPE error |
| TC-07 | 同一 `seq` 发送两次 | 接收方丢弃第二次 |
| TC-08 | 30 秒无 heartbeat | 接收方标记连接异常 |
| TC-09 | 发送 `sync_request` | UNO Q 返回 `sync_response` |
| TC-10 | 休息中发送 `eye_data` | UNO Q 忽略 |

### 11.2 集成测试用例

| ID | 用例 | 期望结果 |
|---|---|---|
| IT-01 | 启动 → 连接 → sync → 正常数据流 | 全流程跑通 |
| IT-02 | 模拟走神 5 秒 | UNO Q 推送 state_update(distracted) + 短振 |
| IT-03 | 模拟摸鱼（屏幕 B 站） | UNO Q 推送 state_update(procrastinating) + 双振 |
| IT-04 | 用户点击休息 5 分钟 | 状态切换 → 倒计时 → 结束自动恢复 + 振动 × 3 |
| IT-05 | 休息中尝试发送 eye_data | UNO Q 忽略 |
| IT-06 | 断开连接 → 自动重连 → 同步 | 30 秒内恢复 |
| IT-07 | 30 分钟连续运行 | 不掉线、不漏消息、不卡顿 |

### 11.3 性能测试用例

| ID | 用例 | 期望结果 |
|---|---|---|
| PT-01 | eye_data 5Hz 持续 10 分钟 | 端到端延迟 < 50ms |
| PT-02 | 连续 1000 次 rest_command 切换 | 无消息丢失、无顺序错乱 |
| PT-03 | MTU 协商失败，强制 23 字节 | Windows 拒绝超过 240 字节的消息并报告 `OUT_OF_RANGE`；本实现不自动分包 |

---

## 12. 协议变更记录

| 版本 | 日期 | 变更内容 | 作者 |
|---|---|---|---|
| v1.0 | 2026-07-21 | 初版协议发布 | FocusFlow 小组 |
| v1.0.1 | 2026-07-21 | 补充 Windows 实际客户端行为：设备发现、首个 seq、紧凑 JSON、Notify 顺序、心跳、重连和 Qt 线程调用约定 | FocusFlow 小组 |
| v1.0.2 | 2026-07-21 | 修正上下行编码类型选择；明确 240 字节应用上限；定义紧凑 `sync_response` 和精简嵌套 `device_status` | FocusFlow 小组 |

---

## 附录 A：完整 UUID 参考

| 名称 | UUID |
|---|---|
| Service | `19B10000-E8F2-537E-4F6C-D104768A1214` |
| RX Characteristic | `19B10001-E8F2-537E-4F6C-D104768A1214` |
| TX Characteristic | `19B10002-E8F2-537E-4F6C-D104768A1214` |

## 附录 B：错误码速查

| code | 含义 | 严重度 |
|---|---|---|
| `INVALID_JSON` | JSON 解析失败 | 低 |
| `INVALID_MSG_TYPE` | 未知消息类型 | 低 |
| `MISSING_FIELD` | 必填字段缺失 | 低 |
| `OUT_OF_RANGE` | 字段值超出范围 | 中 |
| `STATE_CONFLICT` | 状态机不允许的操作 | 中 |
| `DEVICE_BUSY` | 设备正在执行其他操作 | 中 |
| `INTERNAL_ERROR` | UNO Q 内部错误 | 高 |

## 附录 C：状态枚举速查

| state | 含义 | 对应行为 |
|---|---|---|
| `focused` | 专注 | 正常 |
| `distracted` | 走神 | 短振 + 通知 |
| `procrastinating` | 摸鱼 | 双振 + 弹窗 |
| `resting` | 休息 | 暂停推理、倒计时 |

---

**文档结束**

> 本协议是 Windows 端与 UNO Q 端开发的"合同"，任何对协议的修改需要更新本文档并通知双方开发者。
