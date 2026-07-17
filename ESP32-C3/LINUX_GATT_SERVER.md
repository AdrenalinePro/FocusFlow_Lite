# Linux 开发板 BLE GATT Server 说明

本文档说明 Linux 开发板如何作为 BLE Peripheral / GATT Server，向 ESP32-C3 手环发送控制命令。

手环端程序位于 [`hand.ino`](./hand.ino)。当前程序使用 NimBLE-Arduino 2.x 风格 API；依赖 `NimBLEDevice.h`。

## 1. 固定的 GATT 定义

Linux 开发板必须使用下面的 UUID：

```text
Service UUID:
7b3a0001-6a4f-4d91-9c10-123456789000

Control Characteristic UUID:
7b3a0002-6a4f-4d91-9c10-123456789000
```

GATT 树应当是：

```text
Primary Service
└── Control Characteristic
    └── Notify
```

Control Characteristic 只需要 `notify` 属性，不需要 `read`、`write` 或 `write-without-response` 属性。手环连接后会订阅该特征值，Linux 程序收到 `StartNotify` 后才允许发送命令。

BlueZ 的外部 GATT 服务必须通过 D-Bus 导出 Object Manager，并将服务、特征值等对象挂在同一棵对象树下，然后调用 `org.bluez.GattManager1.RegisterApplication` 注册。具体的对象层级和注册方法见 [BlueZ GATT D-Bus API](https://bluez.readthedocs.io/en/latest/gatt-api/)。

## 2. 命令协议

每次 Notification 固定发送 3 个字节：

```text
byte 0：强度 intensity，范围 0~100
byte 1：次数 repeat_count 的低 8 位
byte 2：次数 repeat_count 的高 8 位
```

次数是无符号 16 位整数，小端序。

例如，发送强度 80、重复 3 次：

```python
packet = bytes([80, 3, 0])
```

手环执行：

```text
GPIO0 输出 PWM，占空比 80%
GPIO1 输出 PWM，占空比 80%

500 ms 开
500 ms 关
重复 3 次
```

两路输出始终使用相同的占空比，同时开始、同时停止。

特殊情况：

```text
intensity = 0       立即停止当前动作
repeat_count = 0    立即停止当前动作
```

强度大于 100 的数据包会被手环丢弃。数据包长度不是 3 字节时也会被丢弃。

## 3. Linux Server 的程序结构

Linux 端可以使用 BlueZ D-Bus API，用 Python `dbus-next`、Python `dbus-python` 或 C 语言实现。推荐的程序结构如下：

```text
main
├── 连接 system D-Bus
├── 找到 /org/bluez/hci0
├── 导出 Application ObjectManager
│   ├── Service object
│   └── Characteristic object
├── 调用 GattManager1.RegisterApplication
├── 等待 StartNotify
└── 业务事件发生时发送 3 字节 Notification
```

特征值对象至少要实现这些 BlueZ 接口内容：

```text
org.bluez.GattCharacteristic1

UUID  = 7b3a0002-6a4f-4d91-9c10-123456789000
Service = 对应的 Service object path
Flags = ["notify"]

StartNotify()
StopNotify()
```

程序内部维护一个状态：

```python
notifying = False
```

处理逻辑应为：

```python
def StartNotify():
    global notifying
    notifying = True

def StopNotify():
    global notifying
    notifying = False

def send_command(intensity, repeat_count):
    if not notifying:
        return False

    if not 0 <= intensity <= 100:
        raise ValueError("intensity must be 0..100")

    if not 0 <= repeat_count <= 65535:
        raise ValueError("repeat_count must be 0..65535")

    packet = bytes([
        intensity,
        repeat_count & 0xff,
        (repeat_count >> 8) & 0xff,
    ])

    # 通过 org.freedesktop.DBus.Properties.PropertiesChanged
    # 将 Value 更新为 packet，BlueZ 会把它作为 Notification 发给订阅者。
    update_characteristic_value(packet)
    return True
```

不同语言的 BlueZ 封装函数名称可能不同，但核心行为必须一致：

1. `StartNotify` 被手环调用后，将 `notifying` 置为 `True`。
2. 业务层产生事件时检查 `notifying`。
3. 将 `Value` 更新为 3 字节数据，并发出 `PropertiesChanged`。
4. `StopNotify` 被调用后停止发送。

不要在 Linux 端创建 Write Characteristic，也不要等待手环写回确认。这个项目只需要开发板向手环发送业务数据。

## 4. 广播配置

开发板的广播数据必须包含上面的 Service UUID，手环通过 Service UUID 进行扫描匹配。

建议：

```text
设备名：Hand-Control-Board
广播 Service UUID：7b3a0001-6a4f-4d91-9c10-123456789000
广播间隔：100~200 ms
```

广播名称不是安全认证依据。手环当前默认只检查 Service UUID，开发板地址检查和地址白名单已经在 `hand.ino` 中预留，但默认关闭。

## 5. 预留开发板地址检查和白名单

先在 Linux 开发板上查看蓝牙地址：

```bash
bluetoothctl show
bluetoothctl list
btmgmt info
```

然后修改 [`hand.ino`](./hand.ino) 中的配置：

```cpp
constexpr bool ENABLE_BOARD_ADDRESS_CHECK = true;
constexpr char EXPECTED_BOARD_ADDRESS[] = "AA:BB:CC:DD:EE:FF";
```

或者启用白名单：

```cpp
constexpr bool ENABLE_BOARD_WHITELIST = true;
constexpr const char *BOARD_ADDRESS_WHITELIST[] = {
    "AA:BB:CC:DD:EE:FF",
};
```

当前程序不检查手环自己的蓝牙地址，因为项目中只有一个手环。若以后改为多个手环，再增加手环端地址或身份检查。

## 6. Linux 端测试顺序

先确认 BlueZ 和适配器正常：

```bash
sudo systemctl enable --now bluetooth
bluetoothctl power on
bluetoothctl show
```

启动 GATT Server 后，用手机 nRF Connect 或其他 BLE 调试工具检查：

```text
1. 能扫描到 Hand-Control-Board
2. 能发现 Service UUID
3. 能发现 Control Characteristic UUID
4. Characteristic 具有 Notify 属性
5. 订阅后，Linux 程序能收到 StartNotify
6. 发送 bytes([80, 3, 0])
7. 手环 GPIO0 和 GPIO1 执行 3 次同步动作
```

建议先用 nRF Connect 验证 Linux Server，再让 ESP32-C3 手环连接。这样可以把 Linux BlueZ 问题与手环 PWM/动作问题分开排查。

## 7. 运行维护

Linux Server 注册的 GATT 应用会随着 D-Bus 进程退出而注销，因此正式使用时建议创建 systemd 服务：

```ini
[Unit]
Description=Hand BLE GATT Server
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/hand/ble_server.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

如果 Linux 开发板只有一个蓝牙适配器，默认使用 `hci0`。如果系统有多个适配器，需要在程序配置中明确选择正确的适配器路径。
