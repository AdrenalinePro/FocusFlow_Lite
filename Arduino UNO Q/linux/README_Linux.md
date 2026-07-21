# FocusFlow UNO Q Linux BLE 端

本目录实现 `FocusFlow_BLE_Protocol.md` 中定义的 UNO Q Linux ↔ Windows BLE 通信协议。Linux 端是 GATT Server：暴露 RX characteristic（被 Windows 写入）和 TX characteristic（Notify 推送给 Windows）。

## 文件说明

- `linux_ble_protocol.py`：协议 UUID、常量以及 `encode_downlink` 的再导出。校验逻辑直接复用 `windows/windows_ble_protocol.py`，因为协议本身是对称的。
- `linux_ble_gatt.py`：BlueZ GATT 服务器的 D-Bus 适配层（基于 `dbus-fast`），负责 `GattService1` / `GattCharacteristic1` 注册、Notify 订阅跟踪和 GATT write 回调。
- `linux_ble_state_machine.py`：示例状态机驱动，可被替换为实际的脑电/ONNX 推理。
- `linux_ble_server.py`：高层 asyncio API（`LinuxBLEServer`），把协议解析、消息分发、状态机、心跳回显和休息控制连成一条调用链。
- `linux_ble_test.py`：与 Windows 端 `windows_ble_test.py` 对应的命令行测试脚本。设计目标就是「两端同时运行即可定位问题」（详见下文 §「双向对端调试」）。
- `requirements-linux.txt`：Linux 端依赖。

## 安装和设备准备

```bash
pip3 install --user --break-system-packages -r linux/requirements-linux.txt
```

Debian/Ubuntu 还需要：

```bash
sudo apt-get install bluez libdbus-1-3
```

启动前请确认：

```bash
sudo systemctl enable --now bluetooth
sudo bluetoothctl power on
sudo bluetoothctl discoverable on
```

UNO Q 端建议在 `/etc/bluetooth/main.conf` 中设置 `ControllerMode = le`，并把 MTU 协商到 244 字节（BLE 5.0 允许的最大 ATT MTU）。Linux 侧只要运行了 `dbus-fast` 注册的 GATT application，BlueZ 会自动协商 MTU。

## 一次性安装步骤（必读）

```bash
# 1. 安装依赖
pip3 install --user --break-system-packages -r linux/requirements-linux.txt
sudo apt-get install -y bluez libdbus-1-3
sudo systemctl enable --now bluetooth
sudo bluetoothctl power on
sudo bluetoothctl discoverable on

# 2. UNO Q 必须能拥有 com.focusflow 这个 D-Bus 总线名
sudo tee /etc/dbus-1/system.d/com.focusflow.conf > /dev/null << 'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
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
sudo chown root:root /etc/dbus-1/system.d/com.focusflow.conf
sudo chmod 644 /etc/dbus-1/system.d/com.focusflow.conf
sudo systemctl reload dbus 2>/dev/null || sudo killall -HUP dbus-daemon

# 3. 一次性应用 dbus_fast 补丁
bash linux/setup_dbus_fast.sh --apply
```

### 为什么需要 `setup_dbus_fast.sh`

上游 `dbus_fast` (>= 1.83) 有两个行为会阻止 UNO Q 端注册 GATT application：

1. **默认的 `GetManagedObjects` handler 会过滤掉被查询的根路径本身**（用 `node.startswith(msg.path + "/")` 加斜杠匹配）。BlueZ 的 `GattManager1.RegisterApplication` 调用 GetManagedObjects 期望根路径在结果里；没有它，BlueZ 报 "No valid external GATT objects found" 并静默丢弃注册。
2. **默认 handler 通过 `@dbus_property` 装饰器读取属性值**，但我们的 GATT 类用 `get_properties()` 方法，返回 `{}`。

`setup_dbus_fast.sh` 把 Cython `.so` 改名 `.so.bak`，让 Python 走 `message_bus.py` 源文件，然后注入一个 `_focusflow_get_managed_objects` 方法和分发逻辑。脚本是幂等的，再运行一次也是 no-op；`pip install --upgrade dbus-fast` 之后 .so 文件被覆盖，再次运行脚本即可重新打补丁。

```bash
# 查看当前状态
bash linux/setup_dbus_fast.sh --status

# 恢复 Cython 加速（同时回滚补丁）
bash linux/setup_dbus_fast.sh --restore
```

性能影响：把 `.so` 挪开后，dbus-fast 走纯 Python 分发，对一个 GATT 服务的负载（每秒几条消息）完全可接受；如果你需要跑高吞吐量的其他 dbus_fast 应用，可以先用 `setup_dbus_fast.sh --restore` 临时关掉。

## 主程序如何调用通信接口

### 推荐用法：纯 asyncio 主程序

主程序只需要一个 `LinuxBLEServer` 实例，把它放进 asyncio 事件循环，并在合适的地方读取状态变化、应答消息、推送下行数据。

```python
import asyncio
import logging
from linux.linux_ble_server import (
    BleServerConfig,
    BleServerState,
    LinuxBLEServer,
)
from linux.linux_ble_state_machine import StateDriver
from linux.linux_ble_protocol import BLEMessage


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = BleServerConfig(
        device_name="UNO-Q-FF01",
        focus_score_interval=1.0,
        device_status_interval=30.0,
        rest_countdown_interval=10.0,
    )
    server = LinuxBLEServer(config, driver=StateDriver())

    # 1. 监听连接状态：打印到主程序日志
    server.add_connection_handler(_on_connection)

    # 2. 接收上行消息：交给业务层处理
    server.add_message_handler(_on_message)

    # 3. 接收协议错误/STATE_CONFLICT/心跳超时
    server.add_error_handler(_on_error)

    # 4. 业务层产生的事件推回 UNO Q
    asyncio.create_task(_status_watcher(server))
    asyncio.create_task(_focus_watcher(server))

    # 5. 阻塞运行直到 stop()
    await server.run()


def _on_connection(state: BleServerState) -> None:
    if state == BleServerState.NOTIFY_READY:
        logging.info("Windows 客户端已订阅 Notify，可以开始上行通信")
    elif state == BleServerState.ADVERTISING:
        logging.info("UNO Q 已开始广播，等待 Windows 连接")
    elif state == BleServerState.ERROR:
        logging.error("BLE 服务异常")


def _on_message(message: BLEMessage) -> None:
    if message.type == "eye_data":
        logging.debug("eye_data: %s", message.data)
    elif message.type == "screen_data":
        logging.debug("screen_data: %s", message.data)
    elif message.type == "rest_command":
        logging.info("rest_command: %s", message.data)


def _on_error(message: str) -> None:
    logging.warning("FocusFlow BLE: %s", message)


async def _status_watcher(server: LinuxBLEServer) -> None:
    while True:
        await asyncio.sleep(30.0)
        await server.send_device_status(
            eeg_connected=_eeg_link_ok(),
            eeg_battery=_eeg_battery(),
            wristband_connected=_wristband_ok(),
            wristband_battery=_wristband_battery(),
            tft_display="running",
        )


async def _focus_watcher(server: LinuxBLEServer) -> None:
    while True:
        await asyncio.sleep(1.0)
        new_state, score, prev, feedback = server.driver.decide()
        if server.driver.commit(new_state, score, prev):
            await server.send_state_update(
                state=new_state, focus_score=score,
                prev_state=prev, duration_in_state=0.0,
                triggered_feedback=feedback,
            )
        else:
            await server.send_focus_score(score, server.driver.current_state)


def _eeg_link_ok() -> bool: return False
def _eeg_battery() -> int: return -1
def _wristband_ok() -> bool: return False
def _wristband_battery() -> int: return -1


if __name__ == "__main__":
    asyncio.run(main())
```

要点：

1. `LinuxBLEServer.run()` 是阻塞调用，会启动 BlueZ GATT application、开始广播并维持后台循环，直到 `LinuxBLEServer.stop()` 被调用。
2. 主程序不需要操作 asyncio.Lock 或 GATT path —— 服务器内部已经管理好了 Notify 订阅、TX 排队、序号自增和去重。
3. `driver=StateDriver()` 是默认的占位状态机，传入自定义对象即可替换为真实 ONNX 推理；自定义对象只要满足 `decide()` / `commit()` / `update_inputs()` / `focus_score` 属性的接口即可。
4. `add_message_handler` / `add_connection_handler` / `add_error_handler` 是线程不安全的（应当在事件循环所在线程中注册）。
5. `BleServerConfig.emit_ready_pattern=True`（默认）会在 Windows 订阅 Notify 后立刻推送 `heartbeat + state_update + focus_score + device_status + display_content` 五条 burst —— **仅供测试脚本使用**；主程序应该设置 `emit_ready_pattern=False`。

### 推送下行消息

发送接口与 Windows 客户端 `WindowsBLEClient` 的方法一一对应：

| 方法 | 用途 |
|---|---|
| `send_state_update(state, focus_score, prev_state, duration_in_state, triggered_feedback)` | 状态机切换时立即推送 |
| `send_focus_score(score, state)` | 每秒定时推送（也由后台循环自动处理） |
| `send_rest_countdown(remaining, total, phase)` | 休息中推送倒计时 |
| `send_display_content(line1=…, line2=…, line3=…, line4=…)` | 镜像 TFT 彩屏内容 |
| `send_device_status(eeg_connected, eeg_battery, wristband_connected, wristband_battery, tft_display)` | 外设状态变化或每 30 秒定时 |
| `send_vibration_feedback(mode, trigger, success)` | 手环振动后回执 |
| `send_heartbeat(echo_seq=None, uptime=None)` | 心跳响应 |
| `send_sync_response(state, focus_score, prev_state, rest_countdown, device_status)` | 响应 `sync_request` |
| `send_error(code, message, fatal=False)` | 协议错误 |

返回 `True` 表示 payload 已经写入 BlueZ Notify 队列；返回 `False` 表示当前没有 Windows 客户端订阅 Notify（这是正常的启动期行为）。

### 自定义状态机

`BleServerConfig.driver` 字段可以传入任何带有以下属性的对象：

```python
class MyDriver:
    current_state: str          # 当前状态（focused/distracted/procrastinating/resting）
    focus_score: int            # 当前专注度分数
    prev_state: str             # 上一次状态
    duration_in_state: float    # 当前状态持续时间（秒）
    last_transition_at: float   # 上一次状态切换时间戳

    def update_inputs(self, eye: dict | None, screen: dict | None) -> None:
        """每次收到 eye_data / screen_data 时被调用。"""

    def decide(self) -> tuple[str, int, str, str]:
        """返回 ``(new_state, focus_score, prev_state, feedback)``。"""

    def commit(self, new_state: str, focus_score: int, prev_state: str) -> bool:
        """状态实际变化时返回 True；未变化时返回 False。"""
```

主程序也可以完全不依赖 `driver`，自己驱动状态切换并调用 `send_state_update` / `send_focus_score`。

### 关闭

调用 `LinuxBLEServer.stop()` 即可取消 `run()`、停止广播并注销 BlueZ application。建议在主程序的退出钩子或信号处理中调用：

```python
async def shutdown() -> None:
    await server.stop()
```

## 对外接口速查

| 接口 | 作用 |
|---|---|
| `add_connection_handler(cb)` | `BleServerState` 变化时回调（advertising / connected / notify_ready / error / stopped） |
| `add_message_handler(cb)` | 收到上行消息时回调（已通过协议校验） |
| `add_error_handler(cb)` | 协议错误、STATE_CONFLICT、TX Notify 失败 |
| `driver` 属性 | 读取或替换状态机 |
| `notifying` 属性 | Windows 客户端当前是否订阅了 Notify |
| `state` 属性 | 当前 `BleServerState` |
| `send_*` 系列 | 上述推送下行消息的方法 |

## 心跳、重连和错误

* **心跳**：Windows 每 10 秒上行一次。`LinuxBLEServer` 自动用 `send_heartbeat(echo_seq=…)` 回显，无需业务层介入。
* **重连**：Linux 侧不需要保存连接状态。`RegisterApplication` 成功后持续 `advertise` 即可。Windows 默认每 3 秒重连，单次最多 5 次，主程序可以配置为无限。
* **去重**：服务器内部维护 `SequenceTracker`，重复或回退的 `seq` 会直接丢弃（与 Windows 客户端的策略一致，包括 uint32 回绕）。
* **错误**：
  - 协议校验失败的 `error` 消息通过 `add_error_handler` 上报（同时也会通过 GATT Notify 推送给 Windows，方便其日志聚合）。
  - Windows 在收到 `error(fatal=True)` 时会主动断开重连。
  - 本地 `send_*` 在未连接时会安静返回 `False`，主程序应通过 `state`/`notifying` 判断是否可以推送。

## 双向对端调试（核心使用方式）

`linux_ble_test.py` 和 `windows_ble_test.py` 设计为可以**同时运行**——两端采用统一的日志格式（`[HH:MM:SS.mmm]` 时间戳 + `[TX →]` / `[RX ←]` / `[EVT]` 前缀 + 消息类型 + `seq=` + 数据摘要），运维只需把两份日志贴在一起逐行对齐即可定位问题：

```
Windows 端（客户端视角）                Linux 端（服务端视角）
─────────────────────────              ─────────────────────────
[10:00:00.000] [INFO] scanning …        [10:00:00.000] [INFO] starting server …
[10:00:05.123] [INFO] connected          [10:00:05.456] [EVT] Windows 客户端订阅 Notify
[10:00:05.234] [RX] sync_response seq=7  [10:00:05.235] [TX →] sync_response seq=7 size=179
[10:00:05.345] [TX] eye_data seq=1       [10:00:05.346] [RX ←] eye_data seq=1
[10:00:05.567] [TX] rest_command seq=2    [10:00:05.568] [RX ←] rest_command seq=2
[10:00:15.234] [TX] heartbeat seq=5       [10:00:15.234] [RX ←] heartbeat seq=5
                                       [10:00:15.235] [TX →] heartbeat seq=10 echo_seq=5
[10:00:15.236] [RX] heartbeat seq=10      ← seq=10 对应 ←
```

Linux 端在 Windows 客户端订阅 Notify 后**立即推送一组 5 条 burst**（`heartbeat + state_update + focus_score + device_status + display_content`，`BleServerConfig.emit_ready_pattern=True` 默认开启），Windows 端只要 `bleak.start_notify` 完成就能收到——这是定位「Notify 是否真在推送」的最快方法。

### 推荐调试步骤

```bash
# 在 UNO Q 终端
python3 linux/linux_ble_test.py --duration 0

# 在 Windows 终端（另一个 shell / 远程）
python windows\windows_ble_test.py --device UNO-Q-FF01 --duration 0

# 任意一端 Ctrl+C 结束；汇总会显示：
#   - 收到的上行消息计数
#   - 发送的下行消息计数
#   - heartbeat 双向回环是否成功
#   - 失败原因（如果 RESULT: FAIL）
```

失败原因会精确到具体阶段：

| 日志特征 | 可能原因 |
|---|---|
| Linux: `advertising` 但 Windows: `scanning 0 个设备` | BlueZ 未广播 / Windows 蓝牙未启用 / 防火墙挡了 BLE 广播 |
| Linux: `等待 Windows 客户端订阅 Notify` 超时 | Windows 端 GATT Subscribe 没成功 / CCCD 描述符缺失 |
| Windows: `RX sync_response` 缺失 | Linux 端没收到 `sync_request` 或 `sync_response` 编码失败 |
| Linux: `心跳回环 0 次` | Windows 端没收到 `heartbeat` echo_seq 或 Linux 端 echo_seq 字段错误 |
| 两端都有 `INVALID_MSG_TYPE` | 协议版本不匹配，确认 UUID + 字段名 |

### 常用开关

```bash
# 只校验 BlueZ 适配器是否可用（不需要 Windows 客户端）
python3 linux/linux_ble_test.py --scan-only

# 关闭 1Hz focus_score + 30s device_status 后台循环，便于观察 burst / 心跳
python3 linux/linux_ble_test.py --no-background-loops --duration 30

# 关闭客户端订阅 Notify 时推送的 5 条 burst（保留默认背景循环）
python3 linux/linux_ble_test.py --no-ready-pattern --duration 30

# 持续以 1Hz 推送 focus_score；以 5s 切换一次 state_update
python3 linux/linux_ble_test.py --duration 60 --stream-focus --stream-state

# 测试休息流程（start 会改变 UNO Q 状态，请明确指定）
python3 linux/linux_ble_test.py --rest-interval 5 --duration 40 \
    && python windows\windows_ble_test.py --rest-action start --rest-duration 30 --duration 40

# 进入交互模式，可以手动触发 state / score / rest / sync / display / vibrate
python3 linux/linux_ble_test.py --interactive --duration 0
```

### 交互命令

`--interactive` 模式下支持的命令与 Windows 端脚本风格一致：

```
state <focused|distracted|procrastinating|resting>
score <0..100>
rest <start|stop|extend|query> [duration]
sync                      (手动触发 sync_response)
display                   (推送示例 display_content)
vibrate                   (推送示例 vibration_feedback)
quit
```

## 直接运行前的检查

```bash
python3 -m py_compile linux/linux_ble_protocol.py linux/linux_ble_gatt.py \
    linux/linux_ble_state_machine.py linux/linux_ble_server.py linux/linux_ble_test.py
```

实际连接测试需要 UNO Q 正在广播，并建议依次验证：客户端连接成功 → 订阅 Notify → 周期性 heartbeat 双向正常 → `rest_command(start/stop)` 切换状态 → 拔掉 Windows 端后 UNO Q 持续广播等待重连。

## 已知限制

* **BlueZ GATT server 仅支持 LE**：本实现不广播 BR/EDR，Windows 端必须能识别 LE-only 设备。某些老版本 Windows 蓝牙驱动需要先用 `bluetoothctl` 配对再扫描。
* **MTU 协商依赖 BlueZ + 内核**：`dbus-fast` 不会主动请求 MTU 交换，需要 BlueZ 内核模块已经支持 ≥ 244 字节（Linux 5.10+ 默认值）。如果 ATT write 失败，请升级内核或在 `/etc/bluetooth/main.conf` 增加 `MinLEMTU = 244`。
* **CAP_NET_ADMIN**：注册 GATT application 通常需要 `CAP_NET_ADMIN` 权限（普通用户可以使用 `sudo setcap cap_net_admin+eip $(which python3)` 或以 root 身份运行 UNO Q 主程序）。
