# FocusFlow Lite — 开发日志 (DevLog)

> 项目: FocusFlow Lite 智能专注度辅助系统
> 开始日期: 2026-07-13
> 维护: D 同学 (Tony)

---

## 2026-07-20 (Day 8) — 笔记本 ↔ UNO Q 双向 BLE 通信协议设计 & 实现

### 14:00 - 18:00 | D 同学
**任务**: 设计完整的笔记本 ↔ UNO Q 双向蓝牙通信协议，并实现笔记本端 BLE 通信模块。

### 背景
之前 `camera_demo.py` 中的 `DataAggregator` 只是模拟了数据格式，没有真正的 BLE 通信实现。现在需要：
1. 打通笔记本和 UNO Q 开发板的真正双向 BLE 通信
2. 笔记本以 1Hz 频率发送 MediaPipe 头部位姿特征向量 + 屏幕识别结果
3. 笔记本即时发送走神事件判断
4. 笔记本和 UNO Q 同步程序运行状态（学习监控/休息/未运行）
5. 需要一个完整、可扩展、规范的通信协议

### 完成内容

#### 1. 通信协议设计 — `ble_protocol.md` (~500 行)

**设计原则**:
- **UNO Q 为 GATT Server，笔记本为 GATT Client**
  - 原因：UNO Q Linux 侧始终运行，笔记本按需连接
  - Server 端一经启动就等待连接，Client 端可随时连接/断开
- **JSON 文本优先，二进制可选**
  - JSON 可读性好、调试方便，开发阶段优先
  - 预留二进制压缩格式（29 bytes vs ~350 bytes JSON，12x 压缩），高频率场景可选
- **双向确认**：关键事件（走神、状态切换）需要 ACK 确认
- **优雅降级**：蓝牙断开时笔记本本地缓存数据，重连后补发
- **低延迟优先**：事件包（走神通知）比数据包（传感器数据）优先级更高
- **幂等设计**：所有数据包带序列号，接收方可去重

**BLE GATT 服务定义**:

| Characteristic | UUID | 属性 | 方向 | 说明 |
|---|---|---|---|---|
| LAPTOP_TX | 0000FF01-... | Write | 笔记本 → UNO Q | 传感器数据 (1Hz) + 事件通知 (即时) |
| UNO_TX | 0000FF02-... | Notify | UNO Q → 笔记本 | 反馈指令 + 状态确认 |
| STATE_SYNC | 0000FF03-... | Write+Notify | 双向 | 系统运行状态同步 |
| HEARTBEAT | 0000FF04-... | Write | 笔记本 → UNO Q | 心跳保活 (1Hz, 1 byte) |

**8 种数据包类型**:
1. `sensor_data` — 传感器数据包 (1Hz): 眼动 5D + 屏幕 3D = 8D 特征向量
2. `distraction_event` — 走神事件包 (即时): 摸鱼/走神/人脸丢失/恢复专注
3. `event_ack` — 事件确认包
4. `feedback_cmd` — UNO Q 反馈指令: 振动模式/OLED 显示/弹窗
5. `state_sync` — 系统状态同步: monitoring/resting/paused/error 等
6. `state_ack` — 状态确认
7. `error` — 错误通知: 定义了 E001-E010 共 10 种错误码
8. `heartbeat` — 心跳: 1 byte 二进制计数器

**系统状态机设计**:
```
offline → initializing → calibrating → monitoring ⇄ paused
                                       monitoring → resting → monitoring
                                       any → error → monitoring
                                       any → shutting_down → offline
```

**子状态 (监控中)**:
- `focused` — 高度专注
- `normal` — 一般专注
- `at_risk` — 有走神风险
- `distracted` — 已判定走神

**关键技术决策**:
- MTU 协商 ≥ 512 bytes（避免 JSON 分包）
- 心跳超时 3s → UNO Q 判定笔记本断连 → OLED 显示 "📵 笔记本已断开"
- 断线缓存最多 30 条数据包
- 重连指数退避: 1s → 2s → 4s → 8s → 16s → 30s(max) + 10% 随机抖动

**二进制优化方案（可选）**:
- sensor_data: JSON ~350 bytes → 二进制 29 bytes (12x 压缩)
- 通过 `ver=2` 启用，当前阶段 JSON 优先

#### 2. BLE 通信模块实现 — `ble_communication.py` (~1200 行)

**核心类**:

| 类 | 行数 | 职责 |
|---|---|---|
| `SystemState` / `SubState` / `EventType` | ~50 | 状态和事件枚举 |
| `BLEConfig` | ~30 | BLE 通信配置数据类 |
| `PacketBuilder` | ~200 | 数据包构建/序列化/去重/二进制编码 |
| `BLEClient` | ~600 | 笔记本端 GATT Client (扫描/连接/收发/重连) |
| `BLESimulator` | ~150 | BLE 模拟器 (无需 UNO Q 硬件，开发调试用) |
| `FocusFlowBridge` | ~200 | 一键桥接 EyeTracker + ScreenMonitor → BLE |

**BLEClient 关键特性**:
- 自动扫描过滤设备名 `UNO-Q-FF01`
- bleak 异步 API 封装（兼容跨平台 BLE）
- 4 个 work 线程: Main (连接管理) + Send (1Hz 数据) + Heartbeat (1Hz 心跳) + bleak 回调
- 重连: 指数退避 + 随机抖动，避免对称重连
- 断线缓存: deque(maxlen=30)，重连后自动重发
- 线程安全: `threading.Lock()` 保护所有共享状态
- 4 个回调: `on_feedback_cmd` / `on_state_change` / `on_connection_change` / `on_error`

**BLESimulator 特性**:
- 无需 bleak 依赖，无需 UNO Q 硬件
- 模拟完整 BLE 通信流程（发送/接收/ACK）
- 格式化打印所有数据包（JSON 友好显示）
- 模拟 UNO Q 反馈响应（包含振动指令/OLED 更新）
- 与 BLEClient 完全相同的接口 → 切换只需改一个参数

**FocusFlowBridge 特性**:
- 自动从 EyeTracker/ScreenMonitor 拉取特征 (1Hz)
- 自动检测走神事件:
  - 屏幕摸鱼 → high severity → 即时发送
  - 头部走神 > 10s → medium severity
  - 人脸丢失 > 5s → medium severity
  - 恢复专注 → low severity
- 休息状态管理: `set_resting(True/False)`

**测试结果**:
```
✅ 语法检查通过
✅ PacketBuilder 构建/序列化正常
✅ BLESimulator 模拟收发正常
✅ 状态机转换正常
```

#### 3. 与现有代码的集成点

**`ble_communication.py` 与现有模块的关系**:
- 读取 `eye_tracker.py` 的 `GazeResult.feature_vector` (5D)
- 读取 `screen_monitor.py` 的 `ScreenResult.feature_vector` (3D)
- 替代 `camera_demo.py` 的 `DataAggregator`（旧模拟格式）
- 可直接被 `focusflow_gui.py` 引用，实现 GUI + BLE 完整功能

**对接方式** (3 种灵活选择):
```python
# 方式 1: 完整桥接 (推荐)
bridge = FocusFlowBridge(eye_tracker, screen_monitor, use_simulator=True)
bridge.start()

# 方式 2: 手动控制
client = BLEClient(device_name="UNO-Q-FF01", eye_tracker=et, screen_monitor=sm)
client.start()
client.send_sensor_data(eye_features, screen_features, ...)

# 方式 3: 模拟器 (无硬件开发)
sim = BLESimulator()
sim.start()
sim.send_sensor_data(...)
```

### 下一步 / 待办
- [ ] 与 C 同学对接: UNO Q 端 BLE Server 实现 (bleak GATT Server)
- [ ] 真实 UNO Q ↔ 笔记本 BLE 联调测试
- [ ] 二进制格式压缩测试（如果 JSON 性能不够）
- [ ] `focusflow_gui.py` 集成 `FocusFlowBridge`，GUI 实时显示 BLE 状态
- [ ] 端到端延迟测试（摄像头采集 → BLE 发送 → UNO Q 推理 → 反馈）

### 文件变更清单
| 文件 | 操作 | 说明 |
|------|------|------|
| `ble_protocol.md` | 新增 | 完整 BLE 通信协议规范 (~500 行) |
| `ble_communication.py` | 新增 | 笔记本端 BLE 通信模块实现 (~1200 行) |
| `DEVLOP.md` | 更新 | 本日志条目 |

---

## 2026-07-14 (Day 2) — 笔记本端模块开发

### 19:00 - 23:00 | D 同学
**完成内容:**
- ✅ `eye_tracker.py` (~1000 行) — 基于 MediaPipe Face Mesh + OpenCV solvePnP 的头部姿态估计
  - 468 个面部关键点 → 6 点 PnP 解算 → yaw/pitch/roll
  - 二分类: 专注/走神（阈值可配置）
  - 30s 校准模式（采集基线角度）
  - PyQt5 Signal 线程封装 + 普通回调双模式
  - 运行统计: 专注时长/走神次数/累计走神时长
- ✅ `screen_monitor.py` (~1100 行) — 屏幕内容监控模块
  - mss 定时截屏 + 感知哈希变化检测（避免重复 API 调用）
  - minimax vision API 四分类: 专注工作/一般浏览/摸鱼/离开
  - API 降级策略（网络异常/限流/超时 → 使用缓存结果）
  - 白名单机制（用户可配置允许的应用列表）
  - PyQt5 Signal + 普通回调双模式
- ✅ `camera_demo.py` (~350 行) — 整合演示脚本
  - 同时启动 EyeTracker + ScreenMonitor
  - DataAggregator 整合为 8 维 BLE 数据格式（供 C 同学对接）
  - 支持 --camera-only / --screen-only / 完整模式
  - Ctrl+C 优雅退出 + 统计打印
- ✅ `config_camera.json` — 摄像头 + 屏幕监控配置
- ✅ `requirements_camera.txt` — Python 依赖清单

**技术选型确认:**
- 头部姿态: MediaPipe Face Mesh (GPU 加速可行，CPU 15+ FPS)
- 屏幕截图: mss (跨平台，<50ms)
- API: minimax vision API (性价比优于 GPT-4V)
- 图像去重: imagehash pHash (阈值 5%)

### 23:30 | Bug 修复
**问题**: `python camera_demo.py --calibrate 10` 校准后眼动状态全部显示"未知"

**根因分析**:
1. 🔴 **防抖状态机卡死** (主因): 校准期间 `_current_result.state` 始终为 `CALIBRATING`。校准结束后 `_apply_debounce()` 把 `CALIBRATING` 当作"当前稳定状态"，要求新状态持续 2 秒才切换。如果用户头部角度在边界附近波动 → `new_state` 在 FOCUSED/DISTRACTED 之间来回跳 → 防抖计时器不断重置 → **永远无法从 CALIBRATING 切换出去**。
2. 🔴 **校准计时器跨线程竞态**: `_calibration_timer` 在独立线程中访问 `_calib_samples`，与主循环的 `.append()` 无锁竞争。

**修复方案**:
- `_apply_debounce()`: 新增瞬态直通逻辑 — 当 `current_stable` 为 `CALIBRATING` 或 `UNKNOWN` 时，跳过防抖延迟，立即返回 `new_state`。这两个是瞬态，不应该被"稳定化"。
- `_calibration_timer()`: 
  - 用 `self._lock` 保护 `_calib_samples` 的拷贝和清空
  - 校准结束后主动重置防抖状态机（`_pending_state=None`, `_current_result.state=UNKNOWN`），确保下一帧立即分类
- 修改文件: `eye_tracker.py` (L787-830 `_apply_debounce`, L836-872 `_calibration_timer`)

### 23:45 | Bug 修复 #2
**问题**: 修复后仍有 9 秒延迟 + "校准样本不足 (< 10)"

**根因分析**:
1. 🔴 **摄像头预热期无人脸**: 启动后摄像头需要 1-3 秒曝光调整 → 前几帧无人脸 → `_last_face_time` 始终为 0 → 校准期间无法采集样本 → 校准后无脸帧返回 UNKNOWN (而非 DISTRACTED)
2. 🔴 **用户无感知**: 没有预热进度提示，用户不知道系统在做什么

**修复方案**:
- `eye_tracker.py`:
  - 新增 `_camera_active` 标志: 第一帧成功后设为 True
  - 无脸判断从 `_last_face_time > 0` 改为 `_camera_active`: 摄像头工作中 → DISTRACTED; 初始化阶段 → UNKNOWN
  - 新增 `is_camera_active` / `has_seen_face` 属性供外部查询
- `camera_demo.py`:
  - 校准前增加两步预热等待: (1) 等摄像头激活 (2) 等首次人脸检测
  - 超时提示: 15s 无帧 / 20s 无人脸 → 明确告知用户
  - 校准前检查: 无人脸时跳过校准并提示原因
  - 校准倒计时显示
  - 预热状态实时输出
- 修改文件: `eye_tracker.py` (L249, L603-627, L393-400), `camera_demo.py` (L275-347)

### 23:55 | Bug 修复 #3 — 手扶脸误判为走神
**问题**: 用户手扶着脸（下巴/脸颊）时一直被判定为"走神"

**根因分析**:
- 旧版 6 个 PnP 关键点中有 3 个在下半脸: `chin(152)`, `left_mouth(61)`, `right_mouth(291)`
- 手扶脸 → 下巴和嘴角被遮挡 → MediaPipe 关键点漂移 → solvePnP 解出错误角度 → 误判走神
- 另外手遮挡可能造成 1-2 帧短暂丢脸 → 无防抖直接标 DISTRACTED

**修复方案**:
- `eye_tracker.py` — 替换 PnP 关键点集 (L159-181):
  - 删除: `chin(152)`, `left_mouth(61)`, `right_mouth(291)` ← 下半脸
  - 新增: `left_eye_inner(133)`, `right_eye_inner(362)`, `glabella(151)`, `nose_bridge(6)` ← 全部上半脸
  - 7 个点全部位于眼周+鼻梁+眉间，手扶脸/戴口罩都不会遮挡
  - 水平跨度 65mm (双眼外角), 垂直跨度 40mm (鼻尖→眉间)
- `eye_tracker.py` — 新增丢脸防抖 `FACE_LOST_DEBOUNCE_SEC = 0.8s` (L217, L630-658):
  - 丢脸 < 0.8s: 维持上一帧状态 (容忍短暂遮挡/眨眼)
  - 丢脸 ≥ 0.8s: 确认走神
- 修改文件: `eye_tracker.py` (L159-181 关键点集, L217 丢脸容忍常量, L630-658 丢脸防抖逻辑, L670 动态点数)

### 7/14 23:59 | 新增 PyQt5 GUI 可视化 Demo
**新增文件**: `focusflow_gui.py` (~500 行)
- 深色主题 PyQt5 窗口应用
- 左侧: 摄像头实时画面 + 人脸网格 + PnP 关键点 + 角度/状态叠加
- 右侧: 眼动状态卡片 (yaw/pitch/roll/专注度) + 屏幕状态卡片 (应用名/置信度)
- 圆形专注度仪表盘 (QPainter 手绘, 绿/黄/红渐变)
- 告警面板: 摸鱼检测 / 持续走神 / 人脸丢失
- 底部状态栏: 会话计时 / 专注比 / 走神次数 / 校准按钮
- 支持 --calibrate N 自动校准, --camera-only, --screen-only
- 30 FPS 画面刷新 + 500ms 状态刷新

**eye_tracker.py 修改** (支持 GUI):
- 新增 `_last_frame_bgr` / `_last_landmarks_px` / `_frame_lock` (L264-267)
- 在 `_process_frame` 中存储 468 点像素坐标 (L690-700)
- 新增 `get_annotated_frame()` 方法: 绘制人脸网格 + PnP 关键点 (L427-472)

**用法**:
```
python focusflow_gui.py                     # 完整模式
python focusflow_gui.py --calibrate 10      # 自动校准
python focusflow_gui.py --camera-only        # 仅摄像头
```

---

## 2026-07-13 (Day 1) — 项目启动

### 上午
- 确定选题: "未来学习/办公" → FocusFlow Lite
- 完成 v1.0 方案: 脑电 + 眼动双模态，手环 + OLED + 风扇

### 下午
- v2.0 方案修订:
  - 删除风扇模块
  - 新增屏幕内容监控
  - 新增定时提醒模块
  - 调整 D 同学分工
- 小组分工确认
- 硬件清单确定

---

## 2026-07-15 (Day 3) — GUI 开发 & 优化

### 00:10 | PyQt5 可视化 GUI
**新增文件**: `focusflow_gui.py` (~550 行)
- 深色主题窗口，左侧摄像头+人脸网格，右侧状态卡片
- 圆形专注度仪表盘 (QPainter 手绘)
- 30 FPS 画面 / 500ms 状态刷新
- `eye_tracker.py` 新增 `get_annotated_frame()` 方法

### 00:15 | GUI 对齐修复 + 延迟说明 + 打包

### 00:30 | 恢复速度优化 — 解决"回头后很久才变专注"
**问题**: 低头看手机→回头屏幕→ 一直显示走神，要等很久才恢复专注

**根因**:
1. 走神→专注恢复范围过窄: 旧值 yaw ±20°, pitch ±12° (±12° pitch 几乎不容忍任何头部微动)
2. 防抖 2s: 中间 1 帧抖动就归零重来
3. 刚从丢脸恢复时头部未稳定，频繁踩线重置计时器

**修复**:
- HYSTERESIS_YAW: 10→5, HYSTERESIS_PITCH: 8→4
- 恢复阈值放宽: yaw ±20°→±25°, pitch ±12°→±16°
- FOCUS_HOLD_TIME: 2.0s→1.0s
- 新增 `FACE_RECOVER_GRACE_SEC=1.5s`: 人脸恢复后 1.5s 内用宽阈值(±35°/±24°)过渡
**修复**: StatusCard 固定高度/摄像头自适应/面板对齐统一
**新增**: 画面下方 "⏱ 分析延迟说明" 面板 (实时画面 / ~50ms 眼动 / ~3s 屏幕)
**新增文件**: `build_exe.bat` — PyInstaller 一键打包 → `dist/FocusFlow Lite.exe`


---
