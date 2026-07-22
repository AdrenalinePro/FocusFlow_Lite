# FocusFlow Integration — UNO Q 端集成 App

把仓库里现有的三个独立模块组合成一个在 Arduino UNO Q 上运行的完整
专注力监测 App：

| 模块 | 来源 | 角色 |
|------|------|------|
| 笔记本通信 BLE GATT Server | `source_code/linux/` | 与 Windows 笔记本交换 FocusFlow 协议 JSON |
| TFT 显示 UI 驱动 | `source_code/TFT_UI/` | 软件 SPI 驱动 ILI9341V 240×320 |
| 手环通信 BLE GATT Server | `source_code/ble_server.py` | 控制 ESP32-C3 手环振动 |
| STM32 sketch + Router Bridge | `sketch/` | TFT 渲染 + MPU↔MCU 桥接 |

补充协议文档（仓库根目录）：

- `../UNO_Q_BLE_DECISION_PROTOCOL.md` — BLE GATT `decision_update`
  上行消息（笔记本做最终决策）；本 README 与代码依据这份协议。
- `../LINUX_WINDOWS_COMMUNICATION.md` — 一个 WebSocket（端口 8765）
  替代协议。本仓库**不实现**它（与原始需求 #1 的"通过蓝牙 GATT"冲突）；
  仅在本文末尾记录差异，供未来切换时参考。

> 本 App **不修改** `source_code/` 下的任何文件——所有改动都集中在本目录
> 里。TFT UI 驱动层（`focusflow_ui.{h,cpp}`、`focus_chinese_font.h`、
> `break_image.h`）按 `source_code/TFT_UI/README.md` 的指示原样复制。

---

## 架构

```
┌────────────────────────┐
│ Windows 笔记本           │
│ (FocusFlow 客户端)       │
│  做最终决策               │
└────────────┬───────────┘
             │ FocusFlow BLE 协议（JSON over GATT）
             │ Service UUID: 19B10000-...
             │ RX (Write):    19B10001-...   ← 含 decision_update
             │ TX (Notify):   19B10002-...
             │
             ▼
┌──────────────────────────────────────────────────────────┐
│ UNO Q Linux (MPU) — python/                              │
│                                                           │
│  ┌──────────────────────┐    ┌─────────────────────┐     │
│  │ LinuxBLEServer        │◄──►│ FocusFlowBLEServer  │     │
│  │ (source_code/linux/) │    │ (focusflow_server)  │     │
│  └──────────┬───────────┘    └──────────┬──────────┘     │
│             │                            │                 │
│             │   state changes             │                 │
│             ▼                            ▼                 │
│  ┌──────────────────────────────────────────────────┐     │
│  │ TFT JSON ──► Router Bridge ──► STM32 sketch     │     │
│  │         + wristband.send_vibration(intensity, n) │     │
│  │         + wristband.stop_vibration()             │     │
│  └──────────────────────────────────────────────────┘     │
└──────────────────────────┬───────────────────────────────┘
                           │
            ┌──────────────┴───────────────┐
            ▼                              ▼
   ┌──────────────────────┐    ┌─────────────────────────┐
   │ STM32 (MCU)           │    │ ESP32-C3 手环            │
   │ sketch.ino + TFT UI  │    │ (GATT Client)           │
   │ Bridge.provide       │    │ Service 7b3a0001-...   │
   │ ("tft_cmd", ...)     │    │ Notify  7b3a0002-...   │
   └──────────┬───────────┘    └────────────▲────────────┘
              │                              │
              ▼                              │ 3 字节
   ┌──────────────────────┐    [intensity, count_lo, count_hi]
   │ ILI9341 TFT           │
   │ 240×320 RGB565        │
   └──────────────────────┘
```

**笔记本是决策权威**——本集成的状态机只镜像并执行笔记本通过
`decision_update` 给出的最终状态，不再自行从 `eye_data`/`screen_data`
推断。本地 `StateDriver.decide()` 仍保留作为 `eye_data`/`screen_data`
的兼容路径（仅在 `decision_update` 缺席时生效）。

---

## 目录结构

```
focusflow_integrated/
├── README.md                       # 本文件
├── app.yaml                        # Arduino App Lab manifest
├── install.sh                      # symlink 安装到 ~/ArduinoApps/
├── python/
│   ├── main.py                     # 入口；asyncio 后台线程 + App.run
│   ├── requirements.txt            # dbus-fast, dbus-next, jeepney
│   ├── focusflow_server.py         # LinuxBLEServer 子类
│   ├── tft_bridge.py               # Bridge.notify("tft_cmd", json)
│   ├── wristband_controller.py     # 手环 GATT 封装 + 重试
│   ├── reconnect_supervisor.py     # 笔记本+手环定时重连
│   └── tests/
│       └── test_integration.py     # 集成测试（mock）
└── sketch/
    ├── sketch.ino                  # TFT 渲染 + Bridge 接收
    ├── sketch.yaml                 # Adafruit 库声明
    ├── focusflow_ui.h              # ← 来自 source_code/TFT_UI/
    ├── focusflow_ui.cpp            # ← 来自 source_code/TFT_UI/
    ├── focus_chinese_font.h        # ← 来自 source_code/TFT_UI/
    └── break_image.h               # ← 来自 source_code/TFT_UI/
```

---

## 快速开始

### 一次性环境准备（参考 `source_code/CLAUDE.md`）

```bash
# 1. 系统依赖
sudo apt-get install -y bluez libdbus-1-3
sudo systemctl enable --now bluetooth
sudo bluetoothctl power on

# 2. Python 依赖
pip3 install --user --break-system-packages -r \
    /home/arduino/Focusflow/focusflow_integrated/python/requirements.txt

# 3. dbus-fast 补丁（必须）
bash /home/arduino/Focusflow/source_code/linux/setup_dbus_fast.sh --apply

# 4. D-Bus 策略（必须）
# 见 source_code/linux/README_Linux.md "一次性安装步骤" 第 2 步
sudo tee /etc/dbus-1/system.d/com.focusflow.conf > /dev/null << 'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="arduino">
    <allow own="com.focusflow"/>
    <allow send_destination="com.focusflow"/>
  </policy>
  <policy group="bluetooth">
    <allow own="com.focusflow"/>
    <allow send_destination="com.focusflow"/>
  </policy>
  <policy context="default">
    <allow send_destination="com.focusflow"/>
  </policy>
</busconfig>
EOF
sudo chmod 644 /etc/dbus-1/system.d/com.focusflow.conf
sudo systemctl reload dbus
```

### 部署 App

```bash
bash /home/arduino/Focusflow/focusflow_integrated/install.sh
```

### 构建并运行

```bash
APP=~/ArduinoApps/focusflow_integrated
arduino-app-cli app build       "$APP"
arduino-app-cli app start       "$APP"
arduino-app-cli app logs        "$APP" --follow
arduino-app-cli monitor                       # MCU Serial 输出
```

### 跑集成测试

```bash
cd /home/arduino/Focusflow/focusflow_integrated/python
python3 -m unittest tests.test_integration -v
```

---

## 振动策略（需求 #3 + BLE 补充协议）

振动来源有两类：**`rest_command` 边沿** 与 **`decision_update` 状态切换**。

### 来自 `decision_update` 状态切换

| 状态切换 | 振动次数 | 强度 | 说明 |
|---------|----------|------|------|
| `*` → `distracted` | 3 | 40 | 走神 |
| `*` → `procrastinating` | 3 | 40 | 摸鱼 |
| `*` → `resting` | — | — | **先调用 `stop_vibration()`** 中断任何在途振动（BLE 补充协议强制要求） |
| `*` → `waiting` | — | — | 无惩罚性反馈（BLE 补充协议） |
| `*` → `focused` | — | — | 由 `rest_command(stop)` 负责振动 |

### 来自 `rest_command`

| 事件 | 振动次数 | 强度 | 说明 |
|------|----------|------|------|
| `action=start` | 1 | 40 | 进入休息 |
| `action=stop` | 2 | 40 | 休息结束 |

### `triggered_feedback` 字段（下行 `state_update` 的一部分）

| 状态 | 字段值 |
|------|--------|
| `focused` | `none` |
| `distracted` | `vibrate_short` |
| `procrastinating` | `vibrate_double` |
| `waiting` | `none` |
| `resting` | `none` ← **重要**：原状态机把 `resting` 映射成 `vibrate_continuous`，已被修正 |

默认强度由 `FOCUSFLOW_VIBRATION_INTENSITY` 环境变量覆盖（默认 `40`）。

```python
# focusflow_server.py 顶部常量
DEFAULT_VIBRATION_INTENSITY = 40
VIBRATION_REPEATS_DISTRACTED = 3
VIBRATION_REPEATS_PROCRASTINATING = 3
VIBRATION_REPEATS_REST_START = 1
VIBRATION_REPEATS_REST_END = 2
```

---

## 重连策略（需求 #5）

`reconnect_supervisor.py` 每 15 秒巡检一次两个 BLE 子系统：

### 笔记本侧（`LinuxBLEServer`）

| 状态 | 处理 |
|------|------|
| `ADVERTISING` / `CONNECTED` / `NOTIFY_READY` | 健康，无需动作 |
| `STOPPED` / `ERROR` | 立即重启（指数退避，上限 120s） |
| `STARTING` | 5s 内不打断，等它自己就绪 |

### 手环侧（`WristbandController`）

| 状态 | 处理 |
|------|------|
| `running + subscribed` | 健康 |
| `running + !subscribed > 60s` | 重启（手环断了未重连） |
| `!running` | 重启（GATT application 失效） |

退避：失败一次后下次间隔翻倍（上限 120s），成功立即清零。

---

## MPU ↔ MCU Bridge 接口

### Python → MCU（`Bridge.notify` / `Bridge.call`）

| 名称 | 方向 | 参数 | 用途 |
|------|------|------|------|
| `tft_cmd` | notify | JSON string | 触发 TFT 渲染 |
| `tft_status` | notify/call | — | MPU 探测 TFT 健康 |
| `integration_ready` | notify | "linux" | App 启动信号，MCU 用于日志 |

### MCU → Python（`Bridge.notify`）

| 名称 | 参数 | 用途 |
|------|------|------|
| `tft_heartbeat` | "running" / "error" / "offline" | 每 5s 推送一次 TFT 状态 |

Python 端 `tft_bridge.py` 在构造时通过 `Bridge.provide("tft_heartbeat", ...)`
注册回调，把 MCU 心跳缓存到 `last_status()`，供 `device_status.tft_display` 字段使用。

---

## 笔记本 ↔ UNO Q BLE 协议

### UPLINK（笔记本 → UNO Q）

| 消息 | 来源 | 用法 |
|------|------|------|
| `decision_update` | BLE 补充协议 | **主路径**：笔记本给最终状态，UNO Q 镜像并触发振动/TFT |
| `eye_data` | 旧版基类 | 兼容路径：喂给 `StateDriver.update_inputs(eye=...)` |
| `screen_data` | 旧版基类 | 兼容路径：喂给 `StateDriver.update_inputs(screen=...)` |
| `rest_command` | 基类 | rest start/stop/extend/query（驱动 1×/2× 振动） |
| `heartbeat` | 基类 | 回 echo |
| `sync_request` | 基类 | 回 `sync_response` snapshot |

> **重要**：`decision_update` **不在** upstream 协议的 `UPLINK_TYPES`
> 集合里。本集成在 `FocusFlowBLEServer._handle_rx` 里**旁路**解码
> （轻量级 JSON 解析 + 字段校验），不改 `source_code/`。

### `decision_update` 字段

| 字段 | 类型 | 必需 | 约束 |
|------|------|:---:|------|
| `state` | string | ✓ | `{focused, distracted, procrastinating, waiting, resting}` |
| `score` | int \| null | — | 0–100；null 时保留上次值 |
| `duration` | number | — | ≥ 0；不参与本集成逻辑 |
| `signal_ok` | bool | ✓ | 当前是否有可用脑电 |
| `app` | string | — | 当前应用名，**最长 24 字符** |

完整 payload 范例：

```json
{
  "type": "decision_update",
  "seq": 12,
  "ts": 1784600000,
  "data": {
    "state": "focused",
    "score": 82,
    "duration": 15.0,
    "signal_ok": true,
    "app": "Visual Studio Code"
  }
}
```

### DOWNLINK（UNO Q → 笔记本，已扩展字段含义）

| 消息 | 用法 |
|------|------|
| `state_update` | 状态切换时推送（含 `triggered_feedback`——本集成修复了原 `vibrate_continuous` 的错误） |
| `focus_score` | 1Hz 推送（基类行为） |
| `device_status` | 填入真实 `wristband_connected` 和 `tft_display`；`eeg_*` 留占位符（未实现） |
| `vibration_feedback` | 每次手环振动后回执（含 `trigger` 字段） |
| `rest_countdown` | 休息倒计时（基类行为） |
| `sync_response` | `sync_request` 响应（基类行为） |
| `heartbeat` | 心跳回应（基类行为） |

---

## 显式不做的事项

按你的指示跳过：

- **手环电量上报** — 当前 `device_status.wristband_battery` 固定 `-1`。
  需要 ESP32-C3 firmware 增加 Read Characteristic，超出"整合现有模块"范围。
- **EEG 头环状态** — 本项目未接入 EEG 硬件。`device_status.eeg_*`
  字段保留占位符。
- **`LINUX_WINDOWS_COMMUNICATION.md` WebSocket 协议** — 该协议与原始
  需求 #1（"通过蓝牙 GATT 接收"）冲突；本集成**不实现** WebSocket，
  BLE GATT 是唯一传输路径。如未来切到 WebSocket，需要新增一个独立的
  `WebSocketGateway` 模块并替换 `FocusFlowBLEServer` 为该模块的子类。

---

## 模块清单（详细）

### `python/wristband_controller.py`

封装 `ble_server.HandGattServer`，提供**线程安全**的同步 API：

```python
from wristband_controller import WristbandController

wb = WristbandController(loop=asyncio_loop,
                         on_subscription_change=my_callback)
await wb.start_async()           # 注册 GATT app + advertisement
wb.send_vibration(40, 3)         # 振动 3 次 @ 强度 40（同步、线程安全）
wb.stop_vibration()              # 立即停止任何在振
wb.is_subscribed()               # True/False
wb.is_running()                  # GATT app 是否注册成功
await wb.restart_async()         # 重启（supervisor 用）
```

### `python/tft_bridge.py`

封装 `Bridge.notify/provide`：

```python
from tft_bridge import TFTBridge

tft = TFTBridge()
tft.show_focus(pct=82, elapsed=1122, total=1500, screen="VS Code", status="高度专注")
tft.show_alert(screen="B站")
tft.show_break(remain=154, next_sess=1500)
tft.ping()
tft.last_status()                # "running" / "error" / "offline"
```

### `python/focusflow_server.py`

`LinuxBLEServer` 的子类，**不改 `source_code/linux/`**：

- 重写 `_handle_rx` —— 旁路解码 `decision_update`（upstream 协议尚不认识）
- 重写 `_dispatch` —— 路由 `decision_update` 到专用 handler
- 重写 `_handle_rest_command` —— 进入/退出休息时驱动手环+TFT
- 重写 `_snapshot_device_status` —— 真实注入 `wristband_connected` 和
  `tft_display`
- 注入 `add_message_handler` —— 在 `eye_data`/`screen_data` 后调用
  `driver.decide()/commit()`，状态切换时驱动手环+TFT+下行的 `state_update`

### `python/reconnect_supervisor.py`

`asyncio` 协程，每 15s 检查两个子系统；指数退避重启。

### `python/main.py`

入口。**关键技巧**：App Lab 的 `App.run()` 是同步阻塞的，但 BlueZ
需要 asyncio 事件循环。解决方案：

```python
# 后台 daemon 线程跑 asyncio loop
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()

# 调度异步任务
asyncio.run_coroutine_threadsafe(coro, loop)

# 主线程用 App.run() 阻塞
App.run(user_loop=lambda: time.sleep(1))
```

`Bridge.notify` / `Bridge.provide` 是同步、线程安全的，可以从任何
线程调用。

### `sketch/sketch.ino`

STM32 侧 TFT 渲染器：

- `Bridge.provide("tft_cmd", tft_cmd)` —— 接收 JSON 字符串，
  按 `cmd` 字段分派到 `ui.showFocusScreen / showAlertScreen /
  showBreakScreen`
- `Bridge.provide("tft_status", tft_status)` —— 返回 `"running"` /
  `"error"` / `"offline"`
- `Bridge.notify("tft_heartbeat", status)` —— 每 5s 推送一次状态

TFT JSON 协议完全沿用 `source_code/TFT_UI/focusflow_demo.ino` 的格式。

---

## 调试

```bash
# Python 日志
arduino-app-cli app logs ~/ArduinoApps/focusflow_integrated --follow

# MCU 串口日志
arduino-app-cli monitor

# 笔记本侧 BLE 状态
python3 /home/arduino/Focusflow/source_code/linux/dev_session.py app
python3 /home/arduino/Focusflow/source_code/linux/dev_session.py bluez
python3 /home/arduino/Focusflow/source_code/linux/dev_session.py test 30

# 提高日志详细度
FOCUSFLOW_LOG_LEVEL=DEBUG arduino-app-cli app start \
    ~/ArduinoApps/focusflow_integrated
arduino-app-cli app logs ~/ArduinoApps/focusflow_integrated --follow

# 单独重置 BLE adapter
python3 /home/arduino/Focusflow/source_code/linux/dev_session.py cleanup

# 跑集成测试（mock 模式，不需要硬件）
cd /home/arduino/Focusflow/focusflow_integrated/python
python3 -m unittest tests.test_integration -v
```

---

## 已使用的现有接口清单

> 需求 #6："所有的蓝牙通信请使用已有的接口和通信协议"

| 接口 | 来源 | 用法 |
|------|------|------|
| `LinuxBLEServer` | `source_code/linux/linux_ble_server.py` | 笔记本 GATT Server |
| `BleServerConfig` / `BleServerState` | 同上 | 配置 + 状态枚举 |
| `StateDriver.decide/commit/update_inputs` | `source_code/linux/linux_ble_state_machine.py` | 兼容路径状态机 |
| `decode_message` / `encode_downlink` | `source_code/linux/linux_ble_protocol.py` | 协议编解码 |
| `HandGattServer` | `source_code/ble_server.py` | 手环 GATT Server |
| `pack_command` | 同上 | 手环 3 字节指令打包 |
| `FocusFlowUI` | `source_code/TFT_UI/focusflow_ui.{h,cpp}` | TFT 渲染 |
| `focus_chinese_font.h` | `source_code/TFT_UI/` | 中文字库 |
| `break_image.h` | `source_code/TFT_UI/` | 休息界面全屏图 |
| `Bridge.notify/provide/call` | Arduino App framework | MPU↔MCU |
| `App.run(user_loop=...)` | Arduino App framework | App 生命周期 |

## 未修改的文件

`source_code/` 下任何文件均**未被改动**。所有 Python 模块通过
`main.py._setup_source_path()` 动态把 `source_code/` 加入 `sys.path`
后 import。`decision_update` 走 `_handle_rx` 的旁路路径，绕过了
upstream 协议校验。
