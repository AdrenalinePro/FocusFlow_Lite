# FocusFlow Lite — 笔记本 ↔ UNO Q 双向 BLE 通信协议

> **版本**: v1.0
> **日期**: 2026-07-20
> **作者**: D 同学 (Tony) — 协议设计与笔记本端实现
> **适用**: 笔记本端 (GATT Client) ↔ UNO Q Linux 端 (GATT Server)

---

## 目录

1. [架构概览](#1-架构概览)
2. [BLE GATT 服务定义](#2-ble-gatt-服务定义)
3. [数据包格式](#3-数据包格式)
4. [系统状态机](#4-系统状态机)
5. [通信时序](#5-通信时序)
6. [错误处理与重连](#6-错误处理与重连)
7. [二进制优化方案](#7-二进制优化方案)
8. [笔记本端 API](#8-笔记本端-api)
9. [UNO Q 端 API](#9-uno-q-端-api)

---

## 1. 架构概览

### 1.1 拓扑结构

```
┌─────────────────────────┐       BLE GATT        ┌─────────────────────────┐
│      笔记本 (Client)     │ ◄──────────────────► │    UNO Q Linux (Server)  │
│                          │                      │                          │
│  ble_communication.py   │   Write (FF01)       │  ble_server.py           │
│  ├─ EyeTracker          │ ──────────────────► │  ├─ 接收眼动+屏幕数据     │
│  ├─ ScreenMonitor       │                      │  ├─ 接收事件通知         │
│  ├─ StateManager        │   Notify (FF02)      │  ├─ 跑 ONNX 融合推理     │
│  └─ DataAggregator      │ ◄────────────────── │  ├─ 决策状态机           │
│                          │                      │  └─ 推送反馈指令         │
│                          │   Write (FF03)       │                          │
│                          │ ◄────────┬────────► │  State Sync (双向)       │
│                          │                      │                          │
│                          │   Write (FF04)       │                          │
│                          │ ──────────────────► │  Heartbeat               │
└─────────────────────────┘                      └─────────────────────────┘
```

### 1.2 设计原则

1. **UNO Q 为 Server，笔记本为 Client**: UNO Q Linux 侧启动 GATT Server，笔记本扫描并连接；Server 端始终存在、Client 端按需连接
2. **JSON 文本优先，二进制可选**: JSON 可读性好、调试方便；对高频数据提供二进制压缩选项
3. **双向确认**: 关键事件（走神、状态切换）需要 ACK 确认
4. **优雅降级**: 蓝牙断开时笔记本本地缓存数据，重连后补发
5. **低延迟优先**: 事件包（走神通知）比数据包（传感器数据）优先级更高
6. **幂等设计**: 所有数据包带序列号，接收方可去重

### 1.3 数据流方向

```
笔记本 ──Write──► UNO Q:  传感器数据 (1Hz)、事件通知 (即时)、心跳 (1Hz)
笔记本 ◄──Notify── UNO Q: 反馈指令、状态确认、OLED 更新确认
笔记本 ◄──Write──► UNO Q: 系统状态同步 (双向)
```

---

## 2. BLE GATT 服务定义

### 2.1 服务 UUID

| 项目 | UUID |
|------|------|
| **Primary Service** | `0000FF00-0000-1000-8000-00805F9B34FB` |

> 使用 16-bit UUID 的 128-bit 扩展形式。`FF00` 为自定义服务，其余部分遵循 Bluetooth SIG 标准模板。

### 2.2 Characteristic 一览

| # | Characteristic | UUID | 属性 | 方向 | 说明 |
|---|---|---|---|---|---|
| C1 | **LAPTOP_TX** | `0000FF01-...` | **Write** | 笔记本 → UNO Q | 传感器数据 + 事件通知 |
| C2 | **UNO_TX** | `0000FF02-...` | **Notify** | UNO Q → 笔记本 | 反馈指令 + 状态确认 |
| C3 | **STATE_SYNC** | `0000FF03-...` | **Write, Notify** | 双向 | 系统运行状态同步 |
| C4 | **HEARTBEAT** | `0000FF04-...` | **Write** | 笔记本 → UNO Q | 心跳保活 |

### 2.3 Characteristic 详细定义

#### C1 — LAPTOP_TX (Write)

```
UUID:         0000FF01-0000-1000-8000-00805F9B34FB
Properties:   Write (无响应)
MTU:          建议 ≥ 512 bytes (避免 JSON 分包)
最大写入:     512 bytes / packet
频率:         1 Hz (传感器数据) + 即时 (事件)
```

**用途**: 笔记本向 UNO Q 发送三类数据：
- **sensor_data**: 眼动特征 (5维) + 屏幕特征 (3维) = 8维特征向量，每秒发送一次
- **distraction_event**: 走神事件通知（即时发送，优先于传感器数据）
- **event_ack**: 对 UNO Q 反馈指令的确认

#### C2 — UNO_TX (Notify)

```
UUID:         0000FF02-0000-1000-8000-00805F9B34FB
Properties:   Notify
需要:         笔记本端 CCCD 订阅
频率:         事件驱动 (非周期性)
```

**用途**: UNO Q 向笔记本发送：
- **feedback_cmd**: 反馈指令（振动、OLED 更新、弹窗）
- **state_ack**: 状态同步确认
- **error**: 错误通知

#### C3 — STATE_SYNC (Write + Notify)

```
UUID:         0000FF03-0000-1000-8000-00805F9B34FB
Properties:   Write (无响应) + Notify
频率:         状态变化时 + 每 5s 定时同步
```

**用途**: 双向同步系统运行状态。
- 笔记本 → UNO Q (Write): 告知当前状态变更
- UNO Q → 笔记本 (Notify): 确认状态、推送 UNO Q 侧状态

#### C4 — HEARTBEAT (Write)

```
UUID:         0000FF04-0000-1000-8000-00805F9B34FB
Properties:   Write (无响应)
频率:         1 Hz
数据:         单字节递增计数器 (0-255 循环)
```

**用途**: 笔记本定期写入心跳。UNO Q 监听心跳超时 (≥3s 无心跳) → 判定笔记本断连，OLED 显示 "笔记本已断开"。

---

## 3. 数据包格式

### 3.1 通用外层信封

所有数据包（无论类型）共享同一个 JSON 外层结构：

```json
{
  "ver": 1,
  "type": "<packet_type>",
  "seq": 12345,
  "ts": 1721464820.123456,
  "payload": { ... }
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `ver` | int | ✅ | 协议版本号 (当前=1)，用于向后兼容 |
| `type` | string | ✅ | 数据包类型 (见 §3.2) |
| `seq` | uint32 | ✅ | 单调递增序列号 (0~2³²-1, 溢出回绕), 用于去重和丢包检测 |
| `ts` | float64 | ✅ | Unix 时间戳 (秒, 精度到微秒) |
| `payload` | object | ✅ | 类型相关的负载数据 |

> **序列号规则**: 笔记本和 UNO Q 各自维护独立的 seq 计数器。接收方记录最后一次收到的 seq，若新 seq ≤ 旧 seq 则丢弃（去重）。

### 3.2 数据包类型表

| type | 方向 | 频率 | MTU 要求 | 说明 |
|------|------|------|----------|------|
| `sensor_data` | 笔记本→UNO Q | 1 Hz | ≥ 350 B | 眼动+屏幕特征向量 |
| `distraction_event` | 笔记本→UNO Q | 事件驱动 | ≥ 200 B | 走神/摸鱼事件 |
| `event_ack` | 笔记本→UNO Q | 事件驱动 | ≥ 100 B | 事件确认 |
| `feedback_cmd` | UNO Q→笔记本 | 事件驱动 | ≥ 200 B | 反馈指令 |
| `state_sync` | 双向 | 状态变化 + 5s | ≥ 150 B | 状态同步 |
| `state_ack` | 双向 | 事件驱动 | ≥ 80 B | 状态确认 |
| `error` | 双向 | 事件驱动 | ≥ 150 B | 错误通知 |
| `heartbeat` | 笔记本→UNO Q | 1 Hz | 1 B | 心跳（二进制） |

### 3.3 sensor_data — 传感器数据包

**方向**: 笔记本 → UNO Q (Write to LAPTOP_TX)
**频率**: 1 Hz（每秒一次）
**大小**: ~350 bytes (JSON)

```json
{
  "ver": 1,
  "type": "sensor_data",
  "seq": 1042,
  "ts": 1721464820.123,
  "payload": {
    "eye": {
      "yaw": 5.2,
      "pitch": -3.1,
      "roll": 0.5,
      "is_focused": 1,
      "state_duration": 12.5,
      "confidence": 0.92,
      "focus_score": 0.85,
      "state": "专注"
    },
    "screen": {
      "state_code": 1.0,
      "confidence": 0.88,
      "app_category": 3,
      "state": "专注工作",
      "app": "VSCode"
    },
    "combined": {
      "overall_focus": 0.86,
      "alerts": []
    }
  }
}
```

**字段说明**:

| 路径 | 类型 | 说明 |
|------|------|------|
| `payload.eye.yaw` | float | 偏航角（度），正=右转 |
| `payload.eye.pitch` | float | 俯仰角（度），正=抬头 |
| `payload.eye.roll` | float | 翻滚角（度），正=右歪 |
| `payload.eye.is_focused` | int (0/1) | 眼动二分类结果 |
| `payload.eye.state_duration` | float | 当前状态已持续秒数 |
| `payload.eye.confidence` | float | 人脸检测置信度 (0-1) |
| `payload.eye.focus_score` | float | 眼动专注度评分 (0-1) |
| `payload.eye.state` | string | 眼动状态: "专注"/"走神"/"未知"/"校准中" |
| `payload.screen.state_code` | float | 屏幕状态编码: 0=离开, 0.3=摸鱼, 0.6=一般浏览, 1.0=专注工作 |
| `payload.screen.confidence` | float | 屏幕分类置信度 (0-1) |
| `payload.screen.app_category` | int | 应用类别: -1=未知, 0=离开, 1=摸鱼, 2=一般浏览, 3=专注工作 |
| `payload.screen.state` | string | 屏幕状态: "专注工作"/"一般浏览"/"摸鱼"/"离开"/"未知" |
| `payload.screen.app` | string | 识别到的应用名称 |
| `payload.combined.overall_focus` | float | 综合专注度 (0-1) |
| `payload.combined.alerts` | array | 当前活跃的告警列表 |

**UNO Q 端处理逻辑**:
1. 收到 sensor_data → 提取 `eye(5D) + screen(3D)` 拼接为 8D 特征向量
2. 等待脑电头环的 EEG(5D) 特征 → 拼接完整 13D 向量
3. 跑 ONNX 融合推理 → 输出走神概率
4. 融合决策: 走神概率 > 0.5 + 持续时间 > 阈值 → 触发反馈

### 3.4 distraction_event — 走神事件包

**方向**: 笔记本 → UNO Q (Write to LAPTOP_TX)
**频率**: 即时发送（检测到走神/摸鱼时）
**优先级**: 高于 sensor_data（插队发送）
**大小**: ~200 bytes (JSON)

```json
{
  "ver": 1,
  "type": "distraction_event",
  "seq": 2048,
  "ts": 1721464845.678,
  "payload": {
    "event_id": "dist_20260720_143045_001",
    "event_type": "slacking",
    "severity": "high",
    "source": "screen",
    "details": {
      "app": "哔哩哔哩",
      "reason": "检测到B站视频播放页",
      "duration_sec": 15.0
    },
    "eye_snapshot": {
      "yaw": 5.2,
      "pitch": -3.1,
      "is_focused": 0
    },
    "screen_snapshot": {
      "state_code": 0.3,
      "app_category": 1,
      "confidence": 0.92
    }
  }
}
```

**字段说明**:

| 路径 | 类型 | 说明 |
|------|------|------|
| `payload.event_id` | string | 全局唯一事件ID: `{type}_{YYYYMMDD}_{HHMMSS}_{seq}` |
| `payload.event_type` | string | 事件类型 (见下表) |
| `payload.severity` | string | 严重程度: "low"/"medium"/"high" |
| `payload.source` | string | 检测来源: "eye"/"screen"/"fusion" |
| `payload.details` | object | 事件详情 (根据 event_type 变化) |
| `payload.eye_snapshot` | object | 事件触发时的眼动快照 |
| `payload.screen_snapshot` | object | 事件触发时的屏幕快照 |

**event_type 枚举**:

| event_type | 说明 | 默认 severity |
|------------|------|---------------|
| `slacking` | 屏幕检测到摸鱼（B站/淘宝/游戏等） | high |
| `distracted_eye` | 头部姿态持续偏离屏幕 > 10s | medium |
| `distracted_eeg` | 脑电专注度持续下降 > 15s | medium |
| `face_lost` | 人脸丢失 > 5s（用户离开屏幕） | medium |
| `multi_modal` | 多模态联合判定走神 | high |
| `resumed_focus` | 从走神恢复到专注 | low |
| `rest_started` | 进入休息状态 | low |
| `rest_ended` | 休息结束 | low |

**UNO Q 处理**:
1. 收到 distraction_event → 立即处理（不等下一次 sensor_data）
2. 根据 severity 选择反馈策略:
   - `high` → 双振 + OLED 告警 + 笔记本弹窗
   - `medium` → 短振 + OLED 提示
   - `low` → 仅 OLED 状态更新
3. 返回 ACK（通过 UNO_TX Notify）

### 3.5 feedback_cmd — 反馈指令包

**方向**: UNO Q → 笔记本 (Notify on UNO_TX)
**频率**: 事件驱动
**大小**: ~200 bytes (JSON)

```json
{
  "ver": 1,
  "type": "feedback_cmd",
  "seq": 512,
  "ts": 1721464850.000,
  "payload": {
    "cmd_id": "fb_20260720_143050_001",
    "cmd_type": "vibrate",
    "target": "wristband",
    "params": {
      "pattern": "double_pulse",
      "count": 2
    },
    "display": {
      "line1": "⚠️ 摸 鱼 提 醒",
      "line2": "屏幕: 哔哩哔哩",
      "line3": "状态: 走神",
      "line4": "请回到工作"
    },
    "notify_laptop": true,
    "notify_msg": "检测到摸鱼行为，请回到工作状态"
  }
}
```

**cmd_type 枚举**:

| cmd_type | 说明 | target |
|----------|------|--------|
| `vibrate` | 触发振动 | wristband |
| `oled_update` | 更新 OLED 显示 | oled |
| `vibrate_and_display` | 振动 + OLED 显示 | wristband, oled |
| `rest_mode` | 切换休息模式显示 | oled |
| `work_mode` | 切换工作模式显示 | oled |

**振动 pattern 枚举**:

| pattern | 时序 | 适用场景 |
|---------|------|---------|
| `short` | 200ms on | 走神提醒 5-15s |
| `double_pulse` | 200ms on → 100ms off → 200ms on | 摸鱼 / 疲劳 |
| `sustained` | 500ms on → 200ms off × 3 | 困倦 / 休息结束 |
| `triple_short` | 200ms on × 3 (200ms 间隔) | 休息结束恢复工作 |

### 3.6 state_sync — 系统状态同步包

**方向**: 双向 (Write on STATE_SYNC / Notify on STATE_SYNC)
**频率**: 状态变化时 + 每 5s 定时
**大小**: ~150 bytes (JSON)

```json
{
  "ver": 1,
  "type": "state_sync",
  "seq": 256,
  "ts": 1721464900.000,
  "payload": {
    "system_state": "monitoring",
    "sub_state": "focused",
    "pomodoro": {
      "phase": "work",
      "elapsed_sec": 720,
      "total_sec": 1500
    },
    "timer": {
      "total_focus_sec": 4320,
      "session_elapsed_sec": 5400
    },
    "errors": [],
    "warnings": []
  }
}
```

**系统状态 (system_state) 枚举**:

| system_state | 说明 | 触发条件 |
|---|---|---|
| `initializing` | 系统初始化中 | 程序启动 |
| `calibrating` | 眼动校准中 | 用户点击校准 |
| `monitoring` | 正在监控 | 正常工作状态 |
| `resting` | 休息中 | 番茄钟到 / 强制休息 / 手动暂停 |
| `paused` | 监控暂停 | 用户手动暂停 |
| `error` | 错误状态 | 摄像头丢失 / BLE 断开 / API 不可用 |
| `shutting_down` | 正在关闭 | 用户点击退出 |
| `offline` | 未运行 | 程序未启动 / UNO Q 未连接 |

**子状态 (sub_state) 枚举**:

| sub_state | 适用 system_state | 说明 |
|---|---|---|
| `focused` | monitoring | 用户处于高度专注 |
| `normal` | monitoring | 一般专注 |
| `at_risk` | monitoring | 专注度下降，有走神风险 |
| `distracted` | monitoring | 已判定为走神 |
| `short_rest` | resting | 短休息 (番茄钟 5min) |
| `long_rest` | resting | 强制休息 (连续学习 1h+) |
| `manual_rest` | resting | 用户手动休息 |

**状态转换图**:

```
                    ┌─────────────┐
                    │  offline    │
                    └──────┬──────┘
                           │ 程序启动
                           ▼
                    ┌─────────────┐
              ┌────►│initializing │
              │     └──────┬──────┘
              │            │ 初始化完成
              │            ▼
              │     ┌─────────────┐
              │     │ calibrating │ (可选)
              │     └──────┬──────┘
              │            │ 校准完成
              │            ▼
              │     ┌─────────────┐     番茄钟/强制休息
              │     │ monitoring  │────────────────────┐
              │     └──────┬──────┘                    │
              │            │                           ▼
              │            │ 用户暂停            ┌─────────────┐
              │            ▼                     │  resting    │
              │     ┌─────────────┐              └──────┬──────┘
              │     │   paused    │                     │ 休息结束
              │     └──────┬──────┘                     │
              │            │ 恢复                       │
              │            ▼                            │
              │     ┌─────────────┐                     │
              └─────│ monitoring  │◄────────────────────┘
                    └──────┬──────┘
                           │ 退出
                           ▼
                    ┌─────────────┐
                    │shutting_down│
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │   offline   │
                    └─────────────┘
```

### 3.7 heartbeat — 心跳包

**方向**: 笔记本 → UNO Q (Write to HEARTBEAT)
**频率**: 1 Hz
**大小**: 1 byte (二进制)

**格式**: 单字节无符号整数 (0-255)，每秒递增。UNO Q 端只需检测是否有新数据到达。

```
Byte 0: counter (uint8, 0-255 循环)
```

**超时判定**:
- UNO Q 记录最后一次收到心跳的时间戳
- 若 `now - last_heartbeat > 3.0s` → 判定笔记本断连
- OLED 显示 "📵 笔记本已断开"
- 保留最后收到的状态，继续独立工作（仅脑电+眼动本地推理）

---

## 4. 系统状态机

### 4.1 完整状态转换表

| 当前状态 | 事件 | 下一状态 | 触发动作 |
|----------|------|----------|----------|
| offline | program_start | initializing | OLED: "FocusFlow Lite v2.0" |
| initializing | init_complete | monitoring | OLED: "就绪" |
| initializing | init_complete + calibrate | calibrating | 开始校准 |
| calibrating | calib_done | monitoring | OLED: "就绪" |
| monitoring | user_pause | paused | OLED: "⏸ 已暂停" |
| monitoring | pomodoro_end | resting | OLED: "☕ 休息中" + 振动×1 |
| monitoring | force_rest | resting | OLED: "☕ 强制休息" + 振动×2 |
| monitoring | distraction_detected | monitoring(sub=distracted) | 振动 + OLED 告警 |
| monitoring | resumed_focus | monitoring(sub=focused) | OLED: 恢复专注显示 |
| monitoring | ble_disconnected | monitoring | 笔记本本地缓存，OLED: "📵 笔记本断开" |
| paused | user_resume | monitoring | OLED: "就绪" |
| resting | rest_end | monitoring | OLED: "继续加油💪" + 振动×3 |
| any | error_detected | error | OLED: "⚠ 错误" + 记录日志 |
| error | error_cleared | monitoring | OLED: "已恢复" |
| any | user_quit | shutting_down | OLED: "Bye" |
| shutting_down | cleanup_done | offline | 释放资源 |

### 4.2 状态同步流程

```
笔记本                                 UNO Q
  │                                      │
  │  ─── state_sync {monitoring} ──────► │  (Write STATE_SYNC)
  │                                      │  状态变更: initializing → monitoring
  │  ◄─── state_ack {monitoring} ────── │  (Notify UNO_TX)
  │                                      │  确认状态
  │                                      │
  │  ─── sensor_data (seq=1) ──────────► │  开始传输数据
  │  ─── sensor_data (seq=2) ──────────► │
  │  ...                                 │
  │                                      │
  │  ─── distraction_event ────────────► │  走神检测
  │  ◄─── feedback_cmd {vibrate} ─────── │  反馈指令
  │  ─── event_ack ────────────────────► │  确认执行
  │                                      │
  │  ─── state_sync {resting} ──────────► │  番茄钟到 → 休息
  │  ◄─── state_ack {resting} ────────── │
```

---

## 5. 通信时序

### 5.1 连接建立

```
笔记本 (Client)                         UNO Q (Server)
  │                                         │
  │  1. 开始 BLE 扫描                        │  0. 启动 GATT Server
  │     filter: name="UNO-Q-FF01"           │     等待连接
  │                                         │
  │  2. 发现 UNO-Q-FF01                     │
  │  ───── connect() ────────────────────►  │  3. 接受连接
  │                                         │
  │  4. 发现服务 FF00                       │
  │  5. 订阅 UNO_TX (CCCD)                 │
  │                                         │
  │  6. 发送 state_sync {initializing}      │
  │  ───── Write(FF03) ──────────────────►  │
  │                                         │
  │  7. 开始心跳 (FF04)                     │
  │  ───── Write(FF04, byte=0) ──────────►  │  8. 记录心跳
  │                                         │
  │  ◄─── Notify(FF02) state_ack ──────── │  9. 确认状态
  │                                         │
  │  ✅ 连接建立完成                          │  ✅ 连接建立完成
  │                                         │
```

### 5.2 正常数据流 (1 秒周期)

```
笔记本                                 UNO Q
  │                                      │
  │  ─── Write(FF01) sensor_data ──────► │  T+0ms
  │  ─── Write(FF04) heartbeat ────────► │  T+0ms
  │                                      │  T+10ms  解析 sensor_data
  │                                      │  T+15ms  提取 f_EYE(5) + f_SCREEN(3)
  │                                      │  T+20ms  拼接 EEG(5) → 13D
  │                                      │  T+50ms  ONNX 推理
  │                                      │  T+55ms  决策判断
  │                                      │  T+60ms  更新 OLED (RPC → STM32)
  │                                      │
  │  ... 等待下一个 1s 周期 ...            │
```

### 5.3 走神事件处理 (高优先级)

```
笔记本                                 UNO Q
  │                                      │
  │  screen_monitor 检测到摸鱼             │
  │  ─── Write(FF01) distraction_event ─► │  立即处理
  │                                      │  决策: severity=high
  │                                      │  → 振动双振 + OLED 告警
  │                                      │
  │  ◄─── Notify(FF02) feedback_cmd ──── │  反馈指令
  │                                      │
  │  笔记本收到 feedback_cmd:              │
  │  - 弹窗提醒                            │
  │  ─── Write(FF01) event_ack ─────────► │  确认
  │                                      │
```

### 5.4 断开与重连

```
笔记本                                 UNO Q
  │                                      │
  │  ... 正常工作中 ...                    │
  │  ─── heartbeat ────────────────────► │
  │                                      │
  │  ~~ BLE 连接断开 ~~                    │  3s 无心跳
  │                                      │  判定笔记本断连
  │                                      │  OLED: "📵 笔记本已断开"
  │                                      │  继续本地推理 (脑电+眼动)
  │                                      │
  │  笔记本检测到断开                       │
  │  - 缓存 sensor_data 到本地队列         │
  │  - GUI 显示 "蓝牙已断开"               │
  │  - 开始重连计时器 (指数退避)            │
  │                                      │
  │  ... 尝试重连 ...                      │
  │  1s → 2s → 4s → 8s → 16s → 30s(max) │
  │                                      │
  │  ───── connect() ──────────────────► │  重新连接
  │                                      │
  │  重连成功                              │  重连成功
  │  - 发送缓存的 state_sync              │
  │  - 从上次断点序列号继续                │
  │  - 发送缓存的 sensor_data (最多 30条)  │
  │                                      │  OLED: 恢复正常显示
```

---

## 6. 错误处理与重连

### 6.1 错误码定义

| 错误码 | 说明 | 处理方式 |
|--------|------|----------|
| `E001` | BLE 连接超时 | 重试扫描 (指数退避) |
| `E002` | BLE 意外断开 | 自动重连 |
| `E003` | 写入失败 (characteristic not found) | 重新发现服务 |
| `E004` | UNO Q 处理超时 (ACK 超时) | 重发 (最多 3 次) |
| `E005` | 数据格式错误 (JSON 解析失败) | 丢弃 + 记录日志 |
| `E006` | 序列号异常 (跳跃 > 100) | 记录警告，接受新数据 |
| `E007` | MTU 不足 (数据包 > MTU) | 自动分包 (见 §7) |
| `E008` | UNO Q 内部错误 | 笔记本降级模式 |
| `E009` | 脑电头环断连 | UNO Q 通知笔记本 |
| `E010` | 手环断连 | UNO Q 通知笔记本 |

### 6.2 错误包格式

```json
{
  "ver": 1,
  "type": "error",
  "seq": 99,
  "ts": 1721465000.000,
  "payload": {
    "error_code": "E002",
    "message": "BLE connection lost unexpectedly",
    "severity": "warning",
    "source": "laptop",
    "recoverable": true
  }
}
```

### 6.3 重连策略

```python
# 指数退避重连
RECONNECT_POLICY = {
    "initial_delay": 1.0,    # 首次重连等待 1s
    "max_delay": 30.0,       # 最大等待 30s
    "backoff_factor": 2.0,   # 每次等待翻倍
    "jitter": 0.1,           # ±10% 随机抖动 (避免对称重连)
    "max_retries": 0,        # 无限重试 (0=不限)
}
```

### 6.4 降级策略

| 场景 | 笔记本降级行为 | UNO Q 降级行为 |
|------|---------------|---------------|
| BLE 断开 | 本地缓存数据、GUI 告警 | 仅脑电+眼动推理、OLED 显示"笔记本断开" |
| UNO Q 无响应 > 10s | 本地独立运行、定期尝试重连 | — |
| 脑电头环断连 | 继续发送眼动+屏幕数据 | 仅用笔记本 8D 特征推理 |
| API 不可用 | 使用降级结果（亮度检测+缓存） | 收到 screen_state_code=0.5 |

---

## 7. 二进制优化方案

> **适用场景**: 在高频率（≥10 Hz）或低带宽场景下，可使用二进制格式替代 JSON。
> **当前阶段**: JSON 优先。二进制格式作为可选优化，标注 `ver=2` 时启用。

### 7.1 二进制 sensor_data 格式

```
Byte Offset | Size | Field              | Type    | Description
------------|------|--------------------|---------|-------------------
0           | 1    | version            | uint8   | 协议版本 = 2
1           | 1    | packet_type        | uint8   | 0x01 = sensor_data
2           | 4    | seq                | uint32  | 序列号 (大端)
6           | 4    | ts_sec             | uint32  | Unix 秒 (大端)
10          | 4    | ts_usec            | uint32  | 微秒部分 (大端)
14          | 2    | eye_yaw            | int16   | yaw × 100 (度)
16          | 2    | eye_pitch          | int16   | pitch × 100 (度)
18          | 2    | eye_roll           | int16   | roll × 100 (度)
20          | 1    | eye_is_focused     | uint8   | 0/1
21          | 2    | eye_state_duration | uint16  | 持续时间 × 10 (秒)
23          | 1    | eye_confidence     | uint8   | 0-200 (÷200)
24          | 1    | eye_focus_score    | uint8   | 0-200 (÷200)
25          | 1    | screen_state_code  | uint8   | 0-200 (÷200)
26          | 1    | screen_confidence  | uint8   | 0-200 (÷200)
27          | 1    | screen_app_category| int8    | -1~3
28          | 1    | combined_focus     | uint8   | 0-200 (÷200)
------------|------|--------------------|---------|-------------------
Total: 29 bytes (vs JSON ~350 bytes = 12x 压缩)
```

### 7.2 二进制心跳格式

```
Byte 0: 0x02 (version)
Byte 1: 0x10 (packet_type = heartbeat)
Byte 2: counter (uint8, 0-255)
```

---

## 8. 笔记本端 API

### 8.1 BLEClient 类

```python
from ble_communication import BLEClient, SystemState

# 创建 BLE 客户端
client = BLEClient(
    device_name="UNO-Q-FF01",      # 扫描过滤名
    eye_tracker=eye_tracker,       # EyeTracker 实例
    screen_monitor=screen_monitor, # ScreenMonitor 实例
)

# 注册回调
client.on_feedback_cmd = lambda cmd: print(f"收到反馈: {cmd}")
client.on_state_change = lambda old, new: print(f"状态: {old} → {new}")
client.on_connection_change = lambda connected: print(f"连接: {connected}")

# 启动
client.start()  # 自动扫描、连接、开始数据流

# 状态变更
client.set_state(SystemState.RESTING, sub_state="short_rest")

# 获取连接状态
print(client.is_connected)
print(client.connection_stats)

# 停止
client.stop()
```

### 8.2 回调签名

```python
# 收到 UNO Q 反馈指令
def on_feedback_cmd(cmd: dict) -> None: ...

# 系统状态发生变化
def on_state_change(old_state: SystemState, new_state: SystemState) -> None: ...

# BLE 连接状态变化
def on_connection_change(connected: bool, info: dict) -> None: ...

# 收到错误
def on_error(error: dict) -> None: ...
```

---

## 9. UNO Q 端 API

### 9.1 BLEServer 类 (运行在 UNO Q Linux 上)

```python
# UNO Q Linux 端伪代码
from ble_server import BLEServer

server = BLEServer(device_name="UNO-Q-FF01")

@server.on_sensor_data
def handle_sensor(packet):
    """接收笔记本发来的传感器数据"""
    eye = packet["payload"]["eye"]
    screen = packet["payload"]["screen"]
    # → 融合推理

@server.on_distraction_event
def handle_event(packet):
    """接收走神事件"""
    severity = packet["payload"]["severity"]
    # → 决策反馈

@server.on_state_sync
def handle_state(packet):
    """接收状态同步"""
    new_state = packet["payload"]["system_state"]
    # → 更新本地状态 + OLED

server.start()  # 启动 GATT Server，等待连接
```

---

## 附录 A: 完整 UUID 列表

| 名称 | UUID |
|------|------|
| FocusFlow Primary Service | `0000FF00-0000-1000-8000-00805F9B34FB` |
| LAPTOP_TX Characteristic | `0000FF01-0000-1000-8000-00805F9B34FB` |
| UNO_TX Characteristic | `0000FF02-0000-1000-8000-00805F9B34FB` |
| STATE_SYNC Characteristic | `0000FF03-0000-1000-8000-00805F9B34FB` |
| HEARTBEAT Characteristic | `0000FF04-0000-1000-8000-00805F9B34FB` |

## 附录 B: 数据包类型速查表

| type | 方向 | Characteristic | 频率 |
|------|------|---------------|------|
| sensor_data | Laptop → UNO Q | LAPTOP_TX (Write) | 1 Hz |
| distraction_event | Laptop → UNO Q | LAPTOP_TX (Write) | 事件驱动 |
| event_ack | Laptop → UNO Q | LAPTOP_TX (Write) | 事件驱动 |
| feedback_cmd | UNO Q → Laptop | UNO_TX (Notify) | 事件驱动 |
| state_sync | 双向 | STATE_SYNC (Write/Notify) | 变化+5s |
| state_ack | 双向 | UNO_TX (Notify) / LAPTOP_TX (Write) | 事件驱动 |
| error | 双向 | UNO_TX (Notify) / LAPTOP_TX (Write) | 事件驱动 |
| heartbeat | Laptop → UNO Q | HEARTBEAT (Write) | 1 Hz |

## 附录 C: 与 C 同学 BLE 模块对接要点

1. **UNO Q 端需实现**: GATT Server（bleak）、特征值回调处理、JSON 解析
2. **笔记本端需实现**: GATT Client（bleak）、自动扫描连接、心跳、重连
3. **BLE 设备名约定**: `UNO-Q-FF01`（需在 UNO Q Linux 侧配置）
4. **MTU 协商**: 笔记本端连接后请求 MTU=512，确保 JSON 包不拆分
5. **测试方式**:
   - 单元测试: 两台电脑互测（一台跑 Server 模拟 UNO Q）
   - 集成测试: 笔记本 ↔ UNO Q 直连，Python 脚本验证数据收发

---

> **文档版本**: v1.0 | **最后更新**: 2026-07-20 | **作者**: D 同学 (Tony)
