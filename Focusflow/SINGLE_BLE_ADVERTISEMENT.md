# UNO Q 单广播说明

UNO Q 同时提供两套 GATT 服务，但只注册一份 BLE 广告，避免 `hci0`
只有一个可用 advertising instance 时发生冲突。

## 运行结构

- 广播名称：`UNO-Q-FF01`
- 广播中的发现 UUID：`7b3a0001-6a4f-4d91-9c10-123456789000`
- 手环 GATT 服务：`7b3a0001-6a4f-4d91-9c10-123456789000`
- Windows FocusFlow GATT 服务：`19B10000-E8F2-537E-4F6C-D104768A1214`

手环继续按原 UUID 扫描，无需修改 ESP32 固件。Windows 也用广播中的发现
UUID 找到同一个 UNO Q；连接建立后，再发现并使用 `19B10000-...` 服务。

不要同时启动旧的独立 `ble_server.py` 命令行进程，否则它仍会申请第二份广告。
只运行 `focusflow_integrated` App。

## Windows 验证

```powershell
python ble\windows_ble_test.py --scan-only
python ble\windows_ble_test.py --device UNO-Q-FF01 --duration 30
```

新版测试程序默认使用统一广播 UUID 扫描。如果手工指定过滤条件，应使用：

```powershell
python ble\windows_ble_test.py --scan-only `
  --scan-by-uuid 7b3a0001-6a4f-4d91-9c10-123456789000
```

## UNO Q 正常日志

启动时应同时出现以下含义的日志：

```text
wristband GATT server running (... advertise=False)
Registered FocusFlow GATT application ...
LEAdvertisement registered ... ServiceUUIDs=['7b3a0001-...']
FocusFlow BLE server is advertising as 'UNO-Q-FF01'
```

不应再出现 `advertising as Hand-Control-Board`。如果
`RegisterAdvertisement` 失败，程序现在会进入 `ERROR` 并输出真实异常，不再把失败
状态显示为已广播。
