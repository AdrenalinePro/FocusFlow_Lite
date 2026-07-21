# 电脑端 → UNO Q BLE 决策协议补充

电脑端作为 GATT Client，UNO Q 作为 GATT Server。沿用同学代码中的服务与特征 UUID：

- BLE 广播名 / LocalName: `UNO-Q-FF01`
- Service: `19B10000-E8F2-537E-4F6C-D104768A1214`
- 电脑写入 RX: `19B10001-E8F2-537E-4F6C-D104768A1214`
- UNO Q 通知 TX: `19B10002-E8F2-537E-4F6C-D104768A1214`
- 每条消息为一个 UTF-8 JSON，最大 240 bytes。

UNO Q 的 `LEAdvertisement1` 必须同时携带上述 `LocalName` 和 Service UUID。
使用仓库现有 `BleServerConfig()` 时名称已有默认值，但主程序仍必须实际调用
`LinuxBLEServer.run()`，否则仅有源文件不会产生蓝牙广播。

## 同学需要新增的上行消息

在 UNO Q 协议文件的 `UPLINK_TYPES` 中加入 `decision_update`，同时在
`validate_data()` 中加入该类型的字段校验（否则它仍会在函数末尾被当成未知类型），
最后注册处理函数。电脑端每秒发送一次：

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

字段约定：

| 字段 | 类型 | 含义 |
|---|---|---|
| `state` | string | `focused`、`distracted`、`procrastinating`、`waiting`、`resting` |
| `score` | integer/null | 0–100 的最终专注分；无有效脑电时为 null |
| `duration` | number | 当前屏幕状态持续秒数；可为 0 |
| `signal_ok` | boolean | 当前是否有可用脑电判断 |
| `app` | string/可省略 | 当前应用名，最多 24 个字符 |

UNO Q 收到后应：更新小屏幕；根据 `state` 更新手环反馈。`waiting` 不应触发惩罚性反馈，`resting` 必须立即停止屏幕以外的设备输出。

仓库旧状态机将 `resting` 映射为 `vibrate_continuous`，这与当前需求冲突，
必须改为 `none`，并在进入休息时调用手环服务的 `stop_vibration()`。

## 已有休息消息

电脑端继续发送已有的 `rest_command`：

```json
{"type":"rest_command","seq":13,"ts":1784600001,"data":{"action":"start","duration":300,"reason":"manual"}}
```

提前结束休息时发送 `action: "stop"`。正常倒计时结束时两端按相同时长自行结束，电脑端随后恢复发送正常的 `decision_update`。

## UNO Q 处理伪代码

```python
UPLINK_TYPES.add("decision_update")

# 同时给 validate_data() 增加 decision_update 分支，检查下表字段。
if message.type == "decision_update":
    state = message.data["state"]
    display.show_state(state, message.data.get("score"))
    wearable.set_state(state)       # resting/waiting 时内部必须静默
elif message.type == "rest_command":
    rest_controller.apply(message.data)
```

电脑端连接状态会同时显示在完整仪表盘和笔记本小显示端中。

## 现有仓库接入清单

同学不需要重写 BLE 底层，只需要完成以下接线：

1. 修正 `linux/linux_ble_protocol.py` 的共享协议导入。当前文件导入
   `windows.windows_ble_protocol`，但这份仓库实际文件位于
   `Laptop/ble/windows_ble_protocol.py`。
2. 在共享协议的 `UPLINK_TYPES` 和 `validate_data()` 中加入
   `decision_update`。
3. 在 `LinuxBLEServer._dispatch()` 中增加 `decision_update` 分支。
4. 根据状态向 UNO Q MCU/TFT 发送现有的 `focus`、`alert`、`break` JSON。
5. 调用现有 `HandGattServer.send_vibration()` / `stop_vibration()` 控制手环；
   `resting` 和 `waiting` 一律调用停止。
6. 提供统一主入口并实际运行；日志中必须出现
   `LEAdvertisement registered` 和 `advertising as 'UNO-Q-FF01'`。

本文件是当前“电脑完成最终判断，再通过 BLE 发送给 UNO Q”方案的接口依据。
