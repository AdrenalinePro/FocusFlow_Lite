# FocusFlow Windows BLE 端

本目录实现 `FocusFlow_BLE_Protocol.md` 中定义的 Windows → UNO Q BLE 客户端。Windows 是 GATT Client：向 RX characteristic 写入 JSON，并订阅 TX characteristic 的 Notify。

## 文件说明

- `windows_ble_protocol.py`：UUID、消息编解码、字段校验、上行/下行类型选择和 240 字节限制。
- `windows_ble_client.py`：纯 asyncio BLE 客户端，包含按设备名扫描、自动重连、心跳和通知分发。
- `windows_ble_qt.py`：PyQt5 `QThread` 适配器，供现有 GUI 使用。BLE 的事件循环在独立线程内运行。
- `requirements-windows.txt`：Windows 端依赖。

## 安装和设备准备

```powershell
python -m pip install -r ble\requirements-windows.txt
```

UNO Q 端会自动通过 D-Bus 注册一个 `LEAdvertisement1`，广播 `LocalName=UNO-Q-FF01` + `ServiceUUIDs=[19B10000-...]`，不需要手动 `bluetoothctl discoverable on`（详见 `Arduino UNO Q/linux/README_Linux.md`）。Windows 蓝牙适配器需要支持 BLE；首次使用时应先在系统蓝牙设置中允许设备发现。

`WindowsBLEClient._resolve_device` 会先按名称 + service UUID 过滤扫描；若 UNO Q 端 `Alias` 覆盖了 LEAdvertisement 的 `LocalName`，会自动回退到 service UUID 兜底（详见 §"连接测试"）。命令行里也可以直接传 Windows 蓝牙地址，例如 `AA:BB:CC:DD:EE:FF`，跳过名称匹配。

## 主程序如何调用通信接口

### PyQt5 主程序（推荐）

主程序只需持有一个 `WindowsBLEClientThread`，在窗口初始化时连接信号并启动线程。发送函数可以直接从 Qt 主线程调用；它们会把协程安全地提交到 BLE 线程，不会阻塞界面。

```python
from ble.windows_ble_client import BleClientConfig
from ble.windows_ble_qt import WindowsBLEClientThread


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        config = BleClientConfig(
            device="UNO-Q-FF01",
            max_reconnect_attempts=None,  # 主程序运行期间持续重连
        )
        self.ble = WindowsBLEClientThread(config, self)
        self.ble.connection_state_signal.connect(self.on_ble_state)
        self.ble.state_update_signal.connect(self.on_state_update)
        self.ble.focus_score_signal.connect(self.on_focus_score)
        self.ble.rest_countdown_signal.connect(self.on_rest_countdown)
        self.ble.device_status_signal.connect(self.on_device_status)
        self.ble.error_signal.connect(self.on_ble_error)
        self.ble.start()

    def on_start_rest_clicked(self):
        # 协议要求休息期间暂停 eye_data/screen_data 的生产。
        self.eye_tracker.pause()
        self.screen_monitor.pause()
        self.ble.send_rest_command("start", 300, "manual")

    def on_stop_rest_clicked(self):
        self.ble.send_rest_command("stop")

    def on_eye_result(self, result):
        # 在采集回调或 200 ms 定时器中调用，频率不超过 5 Hz。
        yaw, pitch, is_focused, state_duration, confidence = result.feature_vector
        self.ble.send_eye_data(
            yaw, pitch, is_focused, state_duration, confidence
        )

    def on_screen_result(self, result):
        # ScreenMonitor 的枚举值是中文，先转换为协议枚举。
        state_map = {
            "FOCUSED_WORK": "focused",
            "CASUAL_BROWSE": "distracted",
            "SLACKING": "procrastinating",
            "AWAY": "away",
        }
        category_map = {
            "FOCUSED_WORK": "work",
            "CASUAL_BROWSE": "study",
            "SLACKING": "entertainment",
            "AWAY": "other",
        }
        state_name = result.state.name
        self.ble.send_screen_data(
            state_map.get(state_name, "away"),
            result.confidence,
            result.app,
            category_map.get(state_name, "other"),
        )

    def on_state_update(self, data):
        self.state_label.setText(data["state"])
        if data["state"] != "resting":
            self.eye_tracker.resume()
            self.screen_monitor.resume()

    def on_focus_score(self, score, state):
        self.score_label.setText(f"{score}%")

    def on_rest_countdown(self, remaining, total, phase):
        self.rest_label.setText(f"休息剩余: {remaining}s / {total}s")

    def on_device_status(self, data):
        self.ble_status_label.setText("UNO Q 已连接" if data.get("tft_display") else "设备状态未知")

    def on_ble_state(self, state):
        self.connection_label.setText(f"BLE: {state}")

    def on_ble_error(self, message):
        # 可在这里写入日志或显示非阻塞提示；不要在回调中重启线程。
        logger.warning("FocusFlow BLE: %s", message)

    def closeEvent(self, event):
        self.ble.stop()
        super().closeEvent(event)
```

发送 `eye_data` 的频率为 5 Hz，`screen_data` 为 0.5 Hz。休息开始后要暂停两个采集器；收到 `state_update(state="focused")` 后再恢复。心跳、启动同步请求、断线重连由通信线程自动处理。

### 不使用 PyQt5 的 asyncio 主程序

```python
import asyncio
from ble.windows_ble_client import BleClientConfig, WindowsBLEClient


async def main():
    ble = WindowsBLEClient(BleClientConfig(device="UNO-Q-FF01", max_reconnect_attempts=None))
    ble.add_state_handler(lambda state: print("BLE", state.value))
    ble.add_message_handler(lambda msg: print(msg.type, msg.data))
    task = asyncio.create_task(ble.run_forever())
    await asyncio.sleep(1)
    await ble.send_rest_command("query")
    try:
        await task
    finally:
        await ble.stop()


asyncio.run(main())
```

## 对外接口速查

| 接口 | 作用 |
|---|---|
| `send_eye_data(yaw, pitch, is_focused, state_duration, confidence)` | 发送头部姿态 |
| `send_screen_data(state, confidence, app, category)` | 发送屏幕分类 |
| `send_rest_command(action, duration, reason)` | `start`/`stop`/`extend`/`query` |
| `send_sync_request(fields)` | 请求 UNO Q 返回当前状态 |
| `connection_state_signal` | `connecting`/`connected`/`reconnecting`/`error` 等 |
| `state_update_signal` | 状态切换和反馈 |
| `focus_score_signal` | `(score, state)` |
| `rest_countdown_signal` | `(remaining, total, phase)` |
| `device_status_signal` | 外设连接、电量、TFT 状态 |
| `error_signal` | 协议错误、发送错误、心跳超时和重连提示 |

发送函数返回一个 `concurrent.futures.Future`；通常不需要等待。未连接时返回 `False`，应用应根据 `connection_state_signal` 更新 UI，而不要在 UI 线程中循环等待。

## Windows ↔ UNO Q 实机测试

使用 [windows_ble_test.py](windows_ble_test.py) 可以不启动 FocusFlow GUI，直接测试真实 BLE 链路。它复用正式的 `WindowsBLEClient`，所以会执行设备扫描、GATT 连接、TX Notify、自动 `sync_request`、心跳和协议校验。

### 扫描设备

```powershell
# 默认按 FocusFlow service UUID 过滤（推荐）
python ble\windows_ble_test.py --scan-only
```

`--scan-only` 默认会传 `service_uuids=[19B10000-E8F2-537E-4F6C-D104768A1214]` 给 bleak —— 这是 Windows WinRT 扫描器**唯一可靠**的姿势（无过滤的被动扫描经常扫不到 `peripheral` 模式的设备）。如果 UNO Q 端一切正常，10 秒内会看到一行：

```
[INFO]   - UNO-Q-FF01               14:B5:CD:F1:F4:AF  [RSSI=-58 dBm]  svc=[19b10000]
```

如果想看所有可见 BLE 设备，把过滤关掉：

```powershell
python ble\windows_ble_test.py --scan-only --scan-by-uuid all
```

> Linux 端 `Alias` 属性有时会覆盖 LEAdvertisement 的 `LocalName`，设备可能以 `arduino-UNO` / `ubuntu` 之类的名字出现。`--scan-by-uuid` 的默认过滤恰好能绕过这个坑。

### 连接测试

默认连接 `UNO-Q-FF01`，运行 30 秒，并发送一条 `eye_data`、一条 `screen_data` 和一条安全的 `rest_command(query)`：

```powershell
python ble\windows_ble_test.py --device UNO-Q-FF01 --duration 30
```

`WindowsBLEClient._resolve_device` 会先按名称 + service UUID 过滤找一次；如果 Linux 端 `Alias` 和 LEAdvertisement 的 `LocalName` 不一致（例如设备被广播成 `arduino-UNO`），**会自动回退**到 service UUID 兜底，日志会输出：

```
[INFO] 未按名称 'UNO-Q-FF01' 匹配到设备，但 14:B5:CD:F1:F4:AF 正在广播
       19B10000-E8F2-537E-4F6C-D104768A1214，已改用此设备（通常是 Linux adapter
       的 Alias 覆盖了 LEAdvertisement 的 LocalName）
```

如果扫描结果显示的是地址，也可以直接指定地址：

```powershell
python ble\windows_ble_test.py --device AA:BB:CC:DD:EE:FF --duration 30
```

### 流式上行数据

持续模拟正常上行数据流：

```powershell
python ble\windows_ble_test.py --stream-eye --stream-screen --duration 60
```

### 休息流程

测试休息流程时，`start` 会改变 UNO Q 状态，请明确指定：

```powershell
python ble\windows_ble_test.py --rest-action start --rest-duration 30 --duration 40
```

### 交互模式

也可以进入交互模式，输入 `eye`、`screen`、`sync`、`rest start`、`rest stop`、`rest query` 或 `quit`：

```powershell
python ble\windows_ble_test.py --interactive --duration 0
```

### 静默测试与离线日志

测试成功的最低标准是：日志显示 `connected`，并在测试期间收到 UNO Q 返回的 `heartbeat`；结束时应看到 `RESULT: PASS`。如果只想测试连接、同步和心跳，不发送样例业务消息：

```powershell
python ble\windows_ble_test.py --no-sample-messages
```

把全部日志镜像到文件（DEBUG + bleak 内部）：

```powershell
python ble\windows_ble_test.py --device UNO-Q-FF01 --duration 30 `
    --log-file focusflow-ble.log --verbose
```

UNO Q 端测试前请确认：

- Linux 日志出现 `LEAdvertisement registered on /org/bluez/hci0 (LocalName='UNO-Q-FF01', ...)`（见 `Arduino UNO Q/linux/README_Linux.md` §"广播行为"）。
- Service/RX/TX UUID 与协议一致。
- TX Characteristic 已允许 Notify，并且每条 Notify 是完整且不超过 240 字节的 UTF-8 JSON。

## 重连和错误行为

默认连接失败或断线后每 3 秒重试，最多 5 次；长期运行的主程序建议设置 `max_reconnect_attempts=None`。连接成功后立即订阅 TX Notify，并发送 `sync_request(fields=["all"])`。连续 30 秒收不到 UNO Q 的 heartbeat 响应会主动断开并进入重连流程。

所有入站 JSON 都会先检查 UTF-8、顶层字段、消息类型、数据字段、序号和长度；重复或乱序消息会丢弃。`sync_response` 可以省略 `rest_countdown`，并使用精简的嵌套 `device_status`，以适配 240 字节上限；完整设备状态应由后续 `device_status` 消息发送。协议错误通过 `error_signal` 报告，不会让 PyQt 主线程崩溃。

## 直接运行前的检查

```powershell
python -m py_compile ble\windows_ble_protocol.py ble\windows_ble_client.py ble\windows_ble_qt.py
```

实际连接测试需要 UNO Q 正在广播，并建议依次验证：启动连接、收到 `sync_response`、发送一条 `eye_data`、开始/停止休息、拔掉设备后自动重连。
