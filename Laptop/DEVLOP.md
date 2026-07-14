# FocusFlow Lite — 开发日志 (DevLog)

> 项目: FocusFlow Lite 智能专注度辅助系统
> 开始日期: 2026-07-13
> 维护: D 同学 (Tony)

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
