#!/usr/bin/env python3
"""
FocusFlow Lite — 摄像头模块集成演示 (camera_demo.py)
====================================================

同时运行头部姿态检测 + 屏幕内容监控，模拟实际工作流程。

功能:
  1. 启动 EyeTracker (笔记本摄像头)
  2. 启动 ScreenMonitor (屏幕截图 + API)
  3. 每 1 秒汇总打印当前状态
  4. 模拟 BLE 数据发送格式 (供 C 同学的 BLE 模块对接)
  5. Ctrl+C 优雅退出并打印统计

用法:
    # 完整模式 (需 API Key)
    python camera_demo.py --api-key YOUR_KEY

    # 仅摄像头 (无需 API)
    python camera_demo.py --camera-only

    # 仅屏幕监控 (无需摄像头)
    python camera_demo.py --screen-only --api-key YOUR_KEY

    # 带校准
    python camera_demo.py --calibrate 10

作者: D 同学 (FocusFlow 小组)
日期: 2026-07-14
"""

import time
import os
import signal
import sys
import json
import threading
import argparse
from typing import Dict, Any

# ---- 内部模块 ----
from eye_tracker import EyeTracker, GazeResult, GazeState
from screen_monitor import ScreenMonitor, ScreenResult, ScreenState, SCREEN_STATE_CODES


# ---------------------------------------------------------------------------
# 数据整合器 — 模拟将眼动+屏幕数据打包为 BLE 发送格式
# ---------------------------------------------------------------------------

class DataAggregator:
    """
    将眼动追踪和屏幕监控的结果整合为 8 维数据包，
    模拟笔记本端通过 BLE 发送给 UNO Q 的格式。

    BLE 数据包格式 (与 C 同学对接):
    {
      "type": "sensor_data",
      "timestamp": 1234567890.123,
      "eye": {
        "yaw": 5.2, "pitch": -3.1, "roll": 0.5,
        "is_focused": 1,
        "state_duration": 12.5,
        "confidence": 0.92,
        "focus_score": 0.85,
        "state": "专注"
      },
      "screen": {
        "state_code": 1.0,
        "app_category": 3,
        "confidence": 0.88,
        "state": "专注工作",
        "app": "VSCode"
      },
      "combined": {
        "overall_focus": 0.86,
        "alerts": []
      }
    }
    """

    def __init__(self, eye_tracker: EyeTracker, screen_monitor: ScreenMonitor):
        self.eye = eye_tracker
        self.screen = screen_monitor

    def get_packet(self) -> Dict[str, Any]:
        """获取当前时刻的完整数据包。"""
        gaze = self.eye.get_state()
        screen = self.screen.get_last_state()

        # 组合专注度评分
        if gaze.face_detected and screen.state != ScreenState.UNKNOWN:
            overall = 0.4 * gaze.focus_score + 0.6 * screen.confidence
        elif gaze.face_detected:
            overall = gaze.focus_score
        elif screen.state != ScreenState.UNKNOWN:
            overall = screen.confidence
        else:
            overall = 0.5

        # 告警生成
        alerts = []
        if screen.state == ScreenState.SLACKING:
            alerts.append({"type": "slacking", "msg": f"检测到摸鱼: {screen.app}"})
        if gaze.state == GazeState.DISTRACTED and gaze.state_duration > 10:
            alerts.append({"type": "distracted", "msg": f"持续走神 {gaze.state_duration:.0f}s"})
        if not gaze.face_detected and screen.state == ScreenState.AWAY:
            alerts.append({"type": "away", "msg": "用户已离开"})

        packet = {
            "type": "sensor_data",
            "timestamp": time.time(),
            "eye": {
                "yaw": round(gaze.head_pose.yaw, 2),
                "pitch": round(gaze.head_pose.pitch, 2),
                "roll": round(gaze.head_pose.roll, 2),
                "is_focused": 1 if gaze.state == GazeState.FOCUSED else 0,
                "state_duration": round(gaze.state_duration, 2),
                "confidence": round(gaze.confidence, 3),
                "focus_score": round(gaze.focus_score, 3),
                "state": gaze.state.value,
            },
            "screen": {
                "state_code": SCREEN_STATE_CODES.get(screen.state, 0.5),
                "app_category": screen.feature_vector[2],
                "confidence": screen.confidence,
                "state": screen.state.value,
                "app": screen.app,
            },
            "combined": {
                "overall_focus": round(overall, 3),
                "alerts": alerts,
            },
        }
        return packet

    def get_feature_vector_13d(self) -> Dict[str, Any]:
        """
        生成 UNO Q 融合模型的 13 维特征向量。

        格式: f_EEG(5) + f_EYE(5) + f_SCREEN(3) = 13 维
        注意: f_EEG 由脑电头环直连 UNO Q，笔记本不负责。
              此处仅提供 f_EYE(5) + f_SCREEN(3) = 8 维给 BLE 传输。
        """
        gaze = self.eye.get_state()
        screen = self.screen.get_last_state()
        return {
            "f_EYE": list(gaze.feature_vector),       # [yaw, pitch, is_focused, duration, conf]
            "f_SCREEN": list(screen.feature_vector),   # [state_code, confidence, app_category]
            "combined_8d": (
                list(gaze.feature_vector) + list(screen.feature_vector)
            ),
        }


# ---------------------------------------------------------------------------
# 终端仪表盘
# ---------------------------------------------------------------------------

class TerminalDashboard:
    """简易终端仪表盘 — 实时刷新系统状态。"""

    def __init__(self, aggregator: DataAggregator):
        self.agg = aggregator
        self._shutdown = threading.Event()

    def print_header(self):
        """打印表头。"""
        print("\033[2J\033[H", end="")  # 清屏
        print("=" * 72)
        print("  FocusFlow Lite — 摄像头模块集成演示")
        print("=" * 72)
        print(f"  {'时间':<10s} {'眼动':<8s} {'屏幕':<10s} {'综合专注':<8s} {'告警'}")
        print("-" * 72)

    def print_status(self):
        """打印一行状态。"""
        packet = self.agg.get_packet()
        t = time.strftime("%H:%M:%S")
        eye_state = packet["eye"]["state"]
        scr_state = packet["screen"]["state"]
        scr_app = packet["screen"]["app"] or ""
        focus = packet["combined"]["overall_focus"]
        alerts = packet["combined"]["alerts"]

        # 图标
        eye_icon = {"专注": "🟢", "走神": "🔴", "未知": "⚪", "校准中": "🔵"}.get(eye_state, "❓")
        scr_icon = {"专注工作": "🟢", "一般浏览": "🟡", "摸鱼": "🔴", "离开": "⚫", "未知": "⚪"}.get(scr_state, "❓")

        alert_str = "; ".join(a["msg"] for a in alerts) if alerts else "—"

        bar_len = 10
        filled = int(focus * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        # 截断过长应用名 (中文约8字符, 英文约16字符)
        app_display = scr_app[:16] if scr_app else ""
        app_str = f"({app_display})" if app_display else ""

        print(
            f"  {t:<10s} "
            f"{eye_icon} {eye_state:<4s}  "
            f"{scr_icon} {scr_state:<6s}{app_str:<18s} "
            f"{bar} {focus:.0%}   "
            f"{alert_str[:30]}"
        )

    def run(self, interval: float = 1.0):
        """运行仪表盘。"""
        self.print_header()
        while not self._shutdown.is_set():
            self.print_status()
            time.sleep(interval)

    def stop(self):
        self._shutdown.set()


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="FocusFlow Lite — 摄像头模块集成演示"
    )
    parser.add_argument("--api-key", type=str, default="",
                        help="minimax API Key")
    parser.add_argument("--camera-only", action="store_true",
                        help="仅运行眼动追踪")
    parser.add_argument("--screen-only", action="store_true",
                        help="仅运行屏幕监控")
    parser.add_argument("--camera-id", type=int, default=0,
                        help="摄像头设备 ID (默认 0)")
    parser.add_argument("--fps", type=int, default=15,
                        help="眼动检测帧率")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="屏幕截图间隔 (秒)")
    parser.add_argument("--calibrate", type=float, default=0,
                        help="眼动校准时长 (秒, 0=跳过)")
    parser.add_argument("--duration", type=float, default=0,
                        help="运行时长 (秒, 0=无限)")
    parser.add_argument("--no-api", action="store_true",
                        help="禁用 minimax API")
    parser.add_argument("--json-output", type=str, default="",
                        help="将数据包输出到 JSON 文件")
    args = parser.parse_args()

    # 解析 API Key
    api_key = args.api_key or os.environ.get("MINIMAX_API_KEY", "")
    if not api_key:
        # 从 apikey.txt 读取
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apikey.txt")
        if os.path.exists(key_file):
            with open(key_file, "r") as f:
                api_key = f.read().strip()
    if not api_key and not args.no_api and not args.camera_only:
        print("⚠️  未提供 API Key，屏幕监控将使用降级模式")
        print("   使用 --api-key 提供 Key, 或 --no-api 跳过\n")
    enable_api = bool(api_key) and not args.no_api

    # ---- 创建模块 ----
    eye_tracker = None
    screen_monitor = None

    if not args.screen_only:
        eye_tracker = EyeTracker(
            camera_id=args.camera_id,
            fps=args.fps,
            enable_logging=False,
        )

    if not args.camera_only:
        screen_monitor = ScreenMonitor(
            api_key=api_key,
            interval=args.interval,
            enable_api=enable_api,
            enable_logging=False,
        )

    # ---- 启动模块 ----
    print("正在启动...")
    started_eye = False
    started_screen = False

    if eye_tracker:
        started_eye = eye_tracker.start()
        if not started_eye:
            print("❌ 眼动追踪启动失败 (请检查摄像头)")
            return 1
        print(f"✓ 眼动追踪已启动 (摄像头 {args.camera_id}, {args.fps} FPS)")

        # ---- 摄像头预热: 等待摄像头激活 & 首次人脸检测 ----
        print("\n⏳ 摄像头预热中...")
        print("   (1/2) 等待摄像头初始化...", end="", flush=True)
        warmup_start = time.time()
        while not eye_tracker.is_camera_active:
            if time.time() - warmup_start > 15:
                print("\n⚠️  摄像头预热超时 (15s)，可能未正确连接")
                break
            time.sleep(0.3)
        if eye_tracker.is_camera_active:
            warmup_elapsed = time.time() - warmup_start
            print(f" 完成 ({warmup_elapsed:.1f}s)")

        print("   (2/2) 等待首次人脸检测...", end="", flush=True)
        face_start = time.time()
        while not eye_tracker.has_seen_face:
            if time.time() - face_start > 20:
                print("\n⚠️  未检测到人脸 (20s)，请确认:")
                print("      - 您是否正对摄像头?")
                print("      - 环境光线是否充足?")
                print("      - 摄像头是否被遮挡?")
                break
            time.sleep(0.3)
        if eye_tracker.has_seen_face:
            face_elapsed = time.time() - face_start
            print(f" 完成 ({face_elapsed:.1f}s)")
            print("✅ 摄像头就绪，人脸已检测到\n")
        else:
            print("\n⚠️  继续运行，但眼动追踪可能不准确\n")

    if screen_monitor:
        started_screen = screen_monitor.start()
        print(f"✓ 屏幕监控已启动 (间隔 {args.interval}s, API={'启用' if enable_api else '禁用'})")

    if not started_eye and not started_screen:
        print("❌ 没有任何模块启动成功")
        return 1

    # ---- 校准 ----
    if eye_tracker and args.calibrate > 0:
        # 校准前状态检查
        if not eye_tracker.has_seen_face:
            print("⚠️  跳过校准: 尚未检测到人脸，校准无意义")
            print("    请调整坐姿后重新运行 --calibrate\n")
        else:
            print(f"🔵 开始校准 ({args.calibrate:.0f} 秒)")
            print(f"   请保持直视屏幕中央，头部不要移动...")
            eye_tracker.start_calibration(args.calibrate)

            # 倒计时显示
            for remaining in range(int(args.calibrate), 0, -1):
                print(f"\r   剩余 {remaining:2d} 秒...", end="", flush=True)
                time.sleep(1)
            print("\r   " + " " * 25, end="\r")  # 清除倒计时行

            # 等待校准计时器完成
            time.sleep(0.5)

            base_yaw, base_pitch = eye_tracker.get_baseline()
            print(f"✅ 校准完成: 基线 yaw={base_yaw:.2f}°, pitch={base_pitch:.2f}°")
            print(f"   (校准后状态将立即生效，不再有延迟)\n")

    # ---- 仪表盘 ----
    aggregator = DataAggregator(eye_tracker, screen_monitor) if (eye_tracker and screen_monitor) else None
    dashboard = TerminalDashboard(aggregator) if aggregator else None

    # JSON 日志
    json_log = []
    if args.json_output:
        print(f"\n数据将记录到: {args.json_output}")

    # 信号处理
    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda s, f: shutdown.set())

    # ---- 主循环 ----
    print("=" * 72)
    eye_status = "已就绪" if (eye_tracker and eye_tracker.has_seen_face) else "预热中"
    print(f"  系统运行中 (眼动: {eye_status}) | 按 Ctrl+C 退出")
    print("  提示: 前 1-2 秒可能显示'未知'，摄像头正在稳定帧")
    print("=" * 72 + "\n")

    try:
        start_time = time.time()

        if dashboard:
            dashboard_thread = threading.Thread(
                target=dashboard.run, args=(1.0,), daemon=True
            )
            dashboard_thread.start()

        while not shutdown.is_set():
            # JSON 日志记录
            if args.json_output and aggregator:
                packet = aggregator.get_packet()
                json_log.append(packet)

            # 运行时长限制
            if args.duration > 0:
                if time.time() - start_time >= args.duration:
                    print(f"\n已达到设定运行时长 ({args.duration}s)，退出...")
                    break

            time.sleep(0.5)

    except KeyboardInterrupt:
        pass
    finally:
        if dashboard:
            dashboard.stop()

        # 停止模块
        if eye_tracker:
            eye_tracker.stop()
        if screen_monitor:
            screen_monitor.stop()

    # ---- 输出统计 ----
    print("\n\n" + "=" * 72)
    print("  运行统计")
    print("=" * 72)

    if eye_tracker:
        stats = eye_tracker.get_stats()
        print("\n[眼动追踪]")
        print(f"  总帧数:      {stats['total_frames']}")
        print(f"  专注帧:      {stats['focused_frames']}")
        print(f"  走神帧:      {stats['distracted_frames']}")
        print(f"  无脸帧:      {stats['no_face_frames']}")
        print(f"  专注比:      {stats.get('focus_ratio', 0):.1%}")
        print(f"  走神事件:    {stats['distraction_events']}")
        print(f"  运行时长:    {stats.get('session_elapsed', 0):.0f}s")
        if args.calibrate > 0:
            print(f"  校准基线:    yaw={eye_tracker.get_baseline()[0]:.2f}°, "
                  f"pitch={eye_tracker.get_baseline()[1]:.2f}°")

    if screen_monitor:
        stats = screen_monitor.get_stats()
        print("\n[屏幕监控]")
        print(f"  总截图:      {stats['total_captures']}")
        print(f"  API 调用:    {stats['api_calls']}")
        print(f"  缓存命中:    {stats['cache_hits']}")
        print(f"  降级使用:    {stats['fallback_uses']}")
        print(f"  运行时长:    {stats.get('session_elapsed', 0):.0f}s")
        last = screen_monitor.get_last_state()
        print(f"  最后状态:    {last.state.value} ({last.app or 'N/A'})")

    # 保存 JSON 日志
    if args.json_output and json_log:
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(json_log, f, ensure_ascii=False, indent=2)
        print(f"\n✓ 数据已保存到: {args.json_output} ({len(json_log)} 条记录)")

    print("\n程序正常退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
