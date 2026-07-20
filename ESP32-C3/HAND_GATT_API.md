# 手环 GATT 下发接口使用说明

> 面向「需要向 ESP32-C3 手环发送动作指令」的其它模块。本文只讲怎么调用，
> 不重复协议本身的设计理由。如需了解协议设计、UUID 选取、广播等背景，
> 请阅读 [`LINUX_GATT_SERVER.md`](./LINUX_GATT_SERVER.md)。

## 1. 这份文档给谁看

如果你正在做的是这些事情之一，就读这份文档：

- 你写的是**业务模块**（比如手势识别、跌倒检测、按键事件、远程调度等），
  想要在某个事件触发时让手环震动 / 提示。
- 你写的是**上层服务**（例如 HTTP 接口、状态机、调度器），需要在某个动作点
  向手环发一条「强度 + 重复次数」指令。
- 你需要从**其它线程**或**其它进程**触发手环动作。

你不需要关心：

- BlueZ D-Bus 通信
- NimBLE / ESP32 固件细节
- 3 字节指令的具体编码

这些事情都已经封装在 [`ble_server.py`](./ble_server.py) 的 `HandGattServer`
类里，你只需要 import 它并调用对应方法。

## 2. 一分钟架构回顾

```text
┌────────────────────────────┐         ┌──────────────────────────┐
│  Linux 开发板 (本仓库代码)  │  BLE    │  ESP32-C3 手环 (hand.ino)│
│  GATT Server / Peripheral  │ ──────► │  GATT Client / Central   │
│                            │ Notify  │                          │
│  HandGattServer            │         │  订阅 Control Char.      │
│  ↑ 你的业务模块调用这里的 API         │  收到 3 字节后执行 PWM 动作 │
└────────────────────────────┘         └──────────────────────────┘
```

关键前提：**手环必须先订阅**（调用 `StartNotify`），Linux 端的 `send_*`
调用才会真正发出去。在订阅到达之前调用发送接口会直接返回 `False`，
数据包被丢弃，业务层需要自己决定是忽略、重试，还是先等待订阅。

协议层的固定值（不要改）：

| 项 | 值 |
| --- | --- |
| Service UUID | `7b3a0001-6a4f-4d91-9c10-123456789000` |
| Control Characteristic UUID | `7b3a0002-6a4f-4d91-9c10-123456789000` |
| 广播名 | 默认 `Hand-Control-Board`（可在启动时指定） |
| 指令长度 | 严格 3 字节 |
| byte 0 | 强度 `intensity`，`0..100` |
| byte 1..2 | 重复次数 `repeat_count`，无符号 16 位，小端 |
| `intensity == 0` 或 `repeat_count == 0` | 手环立即停止当前动作 |

## 3. 快速上手

### 3.1 同进程内作为模块使用（推荐）

```python
import asyncio
from ble_server import HandGattServer

async def main():
    server = HandGattServer(adapter="hci0", device_name="Hand-Control-Board")

    # 1) 启动：注册 GATT 服务 + 广播
    await server.start()

    # 2) 业务触发：直接发指令
    #    手环还没连上也没关系，没订阅时这里会返回 False
    server.send_vibration(intensity=80, repeat_count=3)

    # 3) 退出前清理
    await server.stop()

asyncio.run(main())
```

### 3.2 等待手环订阅后再发送

```python
async def on_event(server: HandGattServer):
    # 想等到手环真的连上再发，避免被默默丢掉
    if not await server.wait_until_subscribed(timeout=10.0):
        print("手环 10 秒内没有订阅，先放弃")
        return
    server.send_vibration(60, 5)
```

### 3.3 一次性「等到订阅就发」

```python
ok = await server.send_when_subscribed(50, 2, timeout=15.0)
if not ok:
    print("超时未订阅，指令未发送")
```

### 3.4 从其它线程触发

```python
# 这是摄像头 / 串口 / 状态机线程，不是 asyncio 线程
def on_camera_alert(server: HandGattServer):
    fut = server.send_command_threadsafe(90, 1)
    # 可选：阻塞等结果，知道是否真的发出去了
    sent = fut.result(timeout=1.0)
    if not sent:
        print("手环还没订阅，本次告警没有触达")
```

## 4. API 参考

所有公开接口都来自 `ble_server.py`：

```python
from ble_server import (
    HandGattServer,    # 主类
    pack_command,      # 单独编码 3 字节指令的工具函数
)
```

### 4.1 `HandGattServer` 构造器

```python
HandGattServer(
    adapter: str = "hci0",
    device_name: str = "Hand-Control-Board",
    on_subscription_changed: Callable[[bool], None] | None = None,
)
```

参数：

- `adapter`：BlueZ 适配器名（`hci0`、`hci1`...）或完整 D-Bus 路径
  （`/org/bluez/hci0`）。默认 `hci0`。多适配器场景必须显式传正确名字。
- `device_name`：广播里携带的本地名。手环目前只检查 Service UUID，
  名字改了不影响连接，但建议保持稳定方便调试。
- `on_subscription_changed`：手环订阅 / 退订时回调，
  收到 `True` = 开始订阅，`False` = 已退订。任何异常都会被记录但不会
  传到 BlueZ。

### 4.2 生命周期

| 方法 | 作用 |
| --- | --- |
| `await server.start()` | 申请系统 D-Bus、注册 GATT 服务、注册广播。幂等。 |
| `await server.stop()` | 注销广播、注销 GATT 服务、断开 D-Bus。幂等。 |
| `server.running` | `True` 表示 GATT 应用已成功注册。 |
| `server.notifying` | `True` 表示手环当前已订阅（最近一次通知能送达）。 |

### 4.3 发送指令（核心接口）

业务模块只需要关心以下三个发送方法：

| 方法 | 何时调用 | 返回值 |
| --- | --- | --- |
| `server.send_command(intensity, repeat_count)` | 在 asyncio 事件循环线程内调用 | `True` 已发，`False` 未订阅 / 未启动 |
| `server.send_vibration(intensity, repeat_count)` | 同上，是 `send_command` 的语义化别名 | 同上 |
| `server.stop_vibration()` | 想立即打断当前动作 | 等价于 `send_command(0, 0)`，同上 |
| `server.send_command_threadsafe(intensity, repeat_count)` | **从其它线程**调用 | 返回 `concurrent.futures.Future[bool]` |
| `await server.wait_until_subscribed(timeout=None)` | 想等到手环连上再继续 | `True` 订阅已就绪，`False` 超时 |
| `await server.send_when_subscribed(intensity, repeat_count, timeout=None)` | 启动期一次性发送 | `True` 已发，`False` 超时 |

参数取值范围（会被 `pack_command` 校验，越界抛 `ValueError` / `TypeError`）：

- `intensity`：`int`，`0..100`。
- `repeat_count`：`int`，`0..65535`（无符号 16 位）。
- 任何一项为 0 → 手环立即停止当前动作。

**没有重试、没有队列**。本项目是单手环原型，发送策略是「最新指令覆盖
旧的」——如果你在短时间内连续发多条，前面的可能被覆盖。手环固件内部
会丢弃 `intensity > 100` 或长度 ≠ 3 字节的数据包，所以非法值在这里就被
拦掉，不要自己构造裸 `bytes` 绕过 `pack_command`。

### 4.4 订阅状态变化回调

构造时传入 `on_subscription_changed`：

```python
def on_subscription_changed(notifying: bool) -> None:
    if notifying:
        print("手环已连上，可以发了")
    else:
        print("手环断开 / 退订")

server = HandGattServer(on_subscription_changed=on_subscription_changed)
```

注意回调运行在 BlueZ D-Bus 派发的 asyncio 协程里，**不要在回调里做
阻塞或耗时操作**。如果要把状态推到别的线程 / 事件总线，用
`loop.call_soon_threadsafe` 或自己加队列。

### 4.5 `pack_command(intensity, repeat_count) -> bytes`

独立的纯函数，适合两种场景：

- 单元测试中你想构造一个原始 3 字节指令做断言。
- 你想先校验参数再决定要不要发（`pack_command` 抛错即视为非法）。

```python
from ble_server import pack_command

packet = pack_command(80, 3)        # b'\x50\x03\x00'
packet = pack_command(0, 0)         # 立即停止
```

它不依赖 `HandGattServer` 实例，不连蓝牙，纯粹做编码 + 范围校验。

## 5. 典型集成场景

### 5.1 业务模块与 GATT 服务在同一进程（最常见）

```python
import asyncio
from ble_server import HandGattServer

class VibrationBus:
    """业务模块持有 HandGattServer，对外只暴露 send_*。"""

    def __init__(self, adapter="hci0", name="Hand-Control-Board"):
        self.server = HandGattServer(adapter=adapter, device_name=name)

    async def start(self):
        await self.server.start()

    async def stop(self):
        await self.server.stop()

    def trigger(self, intensity: int, repeats: int) -> bool:
        # 这个方法会在 asyncio 线程里被调用，直接 send_command
        return self.server.send_command(intensity, repeats)

    def stop_now(self) -> bool:
        return self.server.stop_vibration()

async def main():
    bus = VibrationBus()
    await bus.start()
    # 业务事件来了：
    bus.trigger(70, 2)
    # ... 主程序运行 ...
    await bus.stop()
```

### 5.2 业务模块在另一个线程（相机 / 串口 / 调度器）

```python
import threading
from ble_server import HandGattServer

# 1) GATT 跑在主线程的 asyncio 循环里
async def run_gatt():
    server = HandGattServer()
    await server.start()
    global SHARED_SERVER
    SHARED_SERVER = server
    try:
        await asyncio.Event().wait()  # 永久阻塞
    finally:
        await server.stop()

SHARED_SERVER: HandGattServer | None = None
threading.Thread(target=lambda: asyncio.run(run_gatt()), daemon=True).start()
```

```python
# 2) 任何其它线程触发震动
def on_alert():
    assert SHARED_SERVER is not None
    fut = SHARED_SERVER.send_command_threadsafe(90, 1)
    try:
        sent = fut.result(timeout=1.0)
    except Exception:
        sent = False
    if not sent:
        log.warning("alert dropped: wristband not subscribed")
```

### 5.3 启动期 / 重连期等待

```python
async def boot_sequence(server: HandGattServer):
    await server.start()

    # 业务期望：上线后第一时间告诉手环当前默认节奏
    ok = await server.send_when_subscribed(40, 1, timeout=30.0)
    if not ok:
        log.warning("wristband not ready in 30s, will retry on subscribe")
```

如果你只关心「有没有订阅」这一个事件，而不是立刻发指令：

```python
async def on_connect(server: HandGattServer):
    if await server.wait_until_subscribed(timeout=None):
        enable_business_features()
```

### 5.4 退订时自动停业务

```python
def _on_sub(notifying: bool):
    if not notifying:
        log.warning("wristband went away; pausing alerts")
        pause_alert_pipeline()

server = HandGattServer(on_subscription_changed=_on_sub)
```

## 6. 错误处理与边界

| 现象 | 原因 | 处理建议 |
| --- | --- | --- |
| `send_*` 返回 `False` | 手环还没订阅，或服务没 `start()` | 用 `wait_until_subscribed` 或 `send_when_subscribed`；调用前检查 `server.running` 和 `server.notifying` |
| `ValueError("intensity must be in the range 0..100")` | 入参越界 | 在业务层做合法化，不要把脏数据推到 GATT 层 |
| `TypeError("intensity must be an integer")` | 传了 `bool` / `float` / `str` | 转成 `int` 再传 |
| `RuntimeError("the GATT server is not running")` | `start()` 没成功或已经 `stop()` | 检查 `server.running`，失败时上层决定是否重试 |
| `dbus_next.errors.DBusError`（`start()` / `stop()` 抛出） | BlueZ 没起 / 适配器不对 / 权限不足 | 启动前 `systemctl status bluetooth`、`bluetoothctl power on`；进程需要能访问系统 D-Bus |
| 手环长时间没反应 | UUID / Flags 不匹配，或 `Value` 没 emit | 用 `nRF Connect` 单独验证（详见 `LINUX_GATT_SERVER.md` §6） |

参数校验是 **同步抛错** 的——`send_command` 在校验通过之后才检查订阅
状态。所以业务层调用 `send_command` 时要么包 `try/except` 处理
`ValueError` / `TypeError`，要么提前用 `pack_command` 校验。

`send_command_threadsafe` 把校验放在了事件循环线程里执行，**`Future`
里拿到的不是参数异常，而是 `send_command` 的返回值 `False`**——意味着
你在工作线程拿不到参数越界的提示，需要在调用前自己保证参数合法。

## 7. CLI 模式（调试用）

`ble_server.py` 可以直接当独立进程跑，提供一个交互控制台：

```bash
python3 ble_server.py                # 默认 hci0, 交互控制台
python3 ble_server.py --adapter hci1 --name Dev-Board-01
python3 ble_server.py --no-console   # 不开控制台，适合 systemd
python3 ble_server.py --verbose      # 打开 debug 日志
```

控制台命令：

```text
send <intensity> <repeats>   发一条指令，例如 send 80 3
<intensity> <repeats>        send 的简写
stop                         立即停止
status                       看 running / notifying
help                         帮助
quit                         退出
```

业务模块**不需要也不应该**自己跑这个 CLI 进程——`HandGattServer` 是给
同进程 import 用的。如果你必须用独立进程，可以围绕 `ble_server.py`
加一层 IPC，但优先用同进程 import。

## 8. 与 `LINUX_GATT_SERVER.md` 的关系

| 文档 | 给谁看 | 讲什么 |
| --- | --- | --- |
| `LINUX_GATT_SERVER.md` | 维护 GATT Server / 协议的人 | UUID、Flags、广播、BlueZ 注册流程、调试步骤 |
| `HAND_GATT_API.md`（本文） | 业务模块开发者 | 怎么 import、怎么 `send_vibration`、怎么等订阅、线程安全 |

如果只关心「我现在要发一条震动」，按本文 §3 + §4 抄就够了。
如果发现协议层要改（UUID、字节布局、广播格式），请同时改
`hand.ino`、`ble_server.py` 和 `LINUX_GATT_SERVER.md`，并在这里
的 §2 同步更新表格。

## 9. 检查清单（接入新业务模块时过一遍）

- [ ] 已经 `import HandGattServer`，没有直接 import `dbus_next` / BlueZ 私有类。
- [ ] `send_*` 之前已确保参数合法（`0..100` / `0..65535`）。
- [ ] 关键发送点用 `wait_until_subscribed` 或 `send_when_subscribed` 兜底，
      而不是假设手环一定在线。
- [ ] 跨线程触发一律走 `send_command_threadsafe`，并在 `Future` 上做超时。
- [ ] 进程退出 / 服务关闭前调用了 `await server.stop()`。
- [ ] 没有改 `SERVICE_UUID` / `CHARACTERISTIC_UUID`，如果要改，
      同步改 `hand.ino`、`LINUX_GATT_SERVER.md` 和本文 §2。
