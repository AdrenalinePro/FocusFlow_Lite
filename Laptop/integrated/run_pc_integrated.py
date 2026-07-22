#!/usr/bin/env python3
"""One-process Windows entry point for the complete laptop monitor.

This runner combines the teammate's camera/screen modules with the existing
Flowtime EEG collector and dashboard.  The final state is evaluated in the
strict order screen -> camera -> EEG.
"""

from __future__ import annotations

import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
import webbrowser


ROOT = Path(__file__).resolve().parent
MERGED_LAPTOP_DIR = ROOT.parent
DEFAULT_CAMERA_DIR = (
    MERGED_LAPTOP_DIR
    if (MERGED_LAPTOP_DIR / "eye_tracker.py").is_file()
    else Path.home() / "Documents" / "FocusFlow_Lite_team" / "Laptop"
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FocusFlow 完整电脑端")
    parser.add_argument("--camera-dir", type=Path, default=DEFAULT_CAMERA_DIR)
    parser.add_argument("--camera-id", type=int, default=0)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--screen-interval", type=float, default=30.0)
    parser.add_argument("--calibrate", type=float, default=10.0)
    parser.add_argument("--http-port", type=int, default=8000)
    parser.add_argument(
        "--display",
        choices=("dashboard", "mini"),
        default="dashboard",
        help="启动时打开完整仪表盘或笔记本小显示端",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--uno-device",
        default=os.environ.get("UNO_Q_DEVICE", "UNO-Q-FF01"),
        help="UNO Q 的蓝牙名称或 MAC 地址（默认：UNO-Q-FF01）",
    )
    parser.add_argument(
        "--no-uno",
        action="store_true",
        help="仅调试电脑端，不连接 UNO Q",
    )
    parser.add_argument(
        "--allow-screen-fallback",
        action="store_true",
        help="没有 MiniMax Key 时仍启动；只能识别黑屏，不能可靠判断摸鱼",
    )
    return parser.parse_args()


def load_api_key(camera_dir: Path) -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    key_file = camera_dir / "apikey.txt"
    if not key and key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
    return key


def ensure_module_paths(camera_dir: Path, *, require_uno: bool = True) -> None:
    required = [camera_dir / "eye_tracker.py", camera_dir / "screen_monitor.py"]
    if require_uno:
        required.extend([
            camera_dir / "ble" / "windows_ble_client.py",
            camera_dir / "ble" / "windows_ble_protocol.py",
        ])
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit(
            "找不到 GitHub 摄像头/屏幕模块：\n  "
            + "\n  ".join(missing)
            + "\n请确认 FocusFlow_Lite_team 已克隆，或使用 --camera-dir 指定 Laptop 目录。"
        )
    sys.path.insert(0, str(camera_dir))


def patch_enterble_windows_connection() -> None:
    """Make EnterBLE reuse the BLEDevice returned by the scanner on Windows.

    EnterBLE 1.1.6 passes only the address to BleakClient.  Bleak then scans a
    second time and can obtain a stale WinRT device id.  Reusing the actual
    BLEDevice removes that race.  The outer collector startup is separately
    bounded in ``eeg_reader.py`` so a stuck WinRT operation cannot freeze the
    whole camera/screen context loop.
    """
    from bleak import BleakClient
    from enterble.ble.device import Device

    async def connect_with_scanned_device(self) -> None:
        # Flowtime currently advertises a static random BLE address (the two
        # most-significant bits of the first octet are 11).  WinRT's overload
        # without an address type can see the advertisement but wait forever
        # for a GATT session using the wrong address kind.
        try:
            first_octet = int(self.identify.split(":", 1)[0], 16)
        except (AttributeError, TypeError, ValueError):
            first_octet = 0
        address_type = "random" if first_octet & 0xC0 == 0xC0 else "public"
        print(
            f"Windows BLE GATT: {self.identify} "
            f"(address_type={address_type}, cache=off)"
        )
        self._client = BleakClient(
            address_or_ble_device=self.device,
            disconnected_callback=self.disconnected_callback,
            timeout=15.0,
            winrt={
                "address_type": address_type,
                "use_cached_services": False,
            },
        )
        await self._client.connect(timeout=15.0)
        self.connected = True
        print("Windows BLE GATT session active")

    Device.connect = connect_with_scanned_device


def prepare_windows_ble_runtime() -> None:
    """Keep Bleak on an MTA-capable thread in this non-GUI process.

    Some imported Windows packages initialize the main thread as STA even
    though this launcher does not run a Windows message loop.  Bleak then
    rejects advertisement callbacks with "Windows GUI but callbacks are not
    working".  This is Bleak's documented recovery for console applications;
    the next WinRT BLE operation initializes the thread correctly.
    """
    if sys.platform != "win32":
        return
    from bleak.backends.winrt.util import uninitialize_sta

    uninitialize_sta()


def start_http_server(port: int) -> ThreadingHTTPServer:
    handler = partial(QuietHandler, directory=str(ROOT))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise SystemExit(
            f"无法启动网页端口 {port}: {exc}\n请关闭占用该端口的旧程序。"
        ) from exc
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def wait_for_camera(eye_tracker, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not eye_tracker.is_camera_active and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    if not eye_tracker.is_camera_active:
        raise RuntimeError("摄像头启动后 15 秒仍未产生画面")


async def calibrate_camera(eye_tracker, seconds: float) -> None:
    if seconds <= 0:
        return
    face_deadline = time.monotonic() + 15.0
    while not eye_tracker.has_seen_face and time.monotonic() < face_deadline:
        await asyncio.sleep(0.2)
    if not eye_tracker.has_seen_face:
        raise RuntimeError("校准失败：摄像头没有检测到人脸")
    print(f"请保持正常学习坐姿并看向屏幕，校准 {seconds:.0f} 秒...")
    eye_tracker.start_calibration(seconds)
    while eye_tracker.is_calibrating():
        await asyncio.sleep(0.2)
    yaw, pitch = eye_tracker.get_baseline()
    print(f"摄像头校准完成：yaw={yaw:.1f}°, pitch={pitch:.1f}°")


async def publish_context(eeg_reader, message: dict) -> None:
    writer = eeg_reader._session_writer_for_ws
    if writer is not None:
        writer.write(message)
    await eeg_reader.ws_broadcast(message)


async def context_loop(eeg_reader, eye_tracker, screen_monitor, screen_state_enum) -> None:
    """Feed camera and screen results into the hierarchical decision engine."""
    learning_states = {
        screen_state_enum.FOCUSED_WORK,
        screen_state_enum.CASUAL_BROWSE,
    }
    non_learning_states = {
        screen_state_enum.SLACKING,
        screen_state_enum.AWAY,
    }
    last_screen_signature = None
    sensors_paused = False

    while True:
        if eeg_reader.is_resting():
            if not sensors_paused:
                print("休息开始：暂停摄像头和屏幕截图分析，脑电业务输出静默")
                await asyncio.to_thread(screen_monitor.stop)
                await asyncio.to_thread(eye_tracker.stop)
                sensors_paused = True
            await asyncio.sleep(0.5)
            continue

        if sensors_paused:
            print("休息结束：正在恢复摄像头和屏幕监测...")
            camera_started = await asyncio.to_thread(eye_tracker.start)
            screen_started = await asyncio.to_thread(screen_monitor.start)
            if not camera_started or not screen_started:
                raise RuntimeError("休息结束后无法恢复摄像头或屏幕监测")
            await wait_for_camera(eye_tracker)
            sensors_paused = False
            last_screen_signature = None
            print("摄像头、屏幕和脑电业务输出已恢复")

        ts = round(eeg_reader._session_clock(), 2)
        gaze = eye_tracker.get_state()
        camera_message = {
            "type": "camera_state",
            "ts": ts,
            "state": gaze.state.value,
            "face_detected": bool(gaze.face_detected),
            "looking_at_screen": gaze.state.value == "专注",
            "is_focused": gaze.state.value == "专注",
            "state_duration": round(float(gaze.state_duration), 2),
            "confidence": round(float(gaze.confidence), 3),
            "yaw": round(float(gaze.head_pose.yaw), 2),
            "pitch": round(float(gaze.head_pose.pitch), 2),
        }
        eeg_reader._focus_engine.update_camera(camera_message)
        await publish_context(eeg_reader, camera_message)

        screen = screen_monitor.get_last_state()
        is_learning = None
        if screen.state in learning_states:
            is_learning = True
        elif screen.state in non_learning_states:
            is_learning = False
        screen_message = {
            "type": "screen_state",
            "ts": ts,
            "state": screen.state.value,
            "is_learning": is_learning,
            "confidence": round(float(screen.confidence), 3),
            "app": screen.app,
            "reason": screen.reason,
            "from_cache": bool(screen.from_cache),
        }
        eeg_reader._focus_engine.update_screen(screen_message)
        signature = json.dumps(screen_message, ensure_ascii=False, sort_keys=True)
        if signature != last_screen_signature:
            last_screen_signature = signature
            await publish_context(eeg_reader, screen_message)

        await eeg_reader.publish_focus_decision(ts=ts)
        await asyncio.sleep(1.0)


async def wait_for_eeg_connection(eeg_reader, eeg_task, timeout: float = 120.0) -> None:
    """Wait until BLE notifications are active before starting heavy camera work."""
    deadline = time.monotonic() + timeout
    while not bool(eeg_reader._latest_status.get("ble_connected")):
        if eeg_task.done():
            await eeg_task
            raise RuntimeError("EEG task ended before the Flowtime connection became ready")
        if time.monotonic() >= deadline:
            raise TimeoutError("等待 Flowtime 完成 BLE 连接超时")
        await asyncio.sleep(0.2)


async def run(args: argparse.Namespace) -> None:
    camera_dir = args.camera_dir.resolve()
    ensure_module_paths(camera_dir, require_uno=not args.no_uno)

    # The EEG module reads these values at import time.
    os.environ.setdefault("WS_HOST", "127.0.0.1")
    os.environ.setdefault("WS_PORT", "8765")

    prepare_windows_ble_runtime()
    patch_enterble_windows_connection()
    import eeg_reader

    api_key = load_api_key(camera_dir)
    if not api_key and not args.allow_screen_fallback:
        raise SystemExit(
            "缺少 MINIMAX_API_KEY，无法判断屏幕是否在学习。\n"
            "请先设置 $env:MINIMAX_API_KEY，或把 Key 写入 GitHub Laptop/apikey.txt。\n"
            "仅调试硬件时可添加 --allow-screen-fallback。"
        )

    http_server = start_http_server(args.http_port)
    dashboard_url = f"http://127.0.0.1:{args.http_port}/dashboard.html"
    mini_url = f"http://127.0.0.1:{args.http_port}/focusflow_mini.html"
    url = mini_url if args.display == "mini" else dashboard_url
    print(f"完整监测页面：{dashboard_url}")
    print(f"笔记本小显示端：{mini_url}")
    if not args.no_browser:
        webbrowser.open(url)

    eye_tracker = None
    screen_monitor = None
    context_task = None
    eeg_task = None
    uno_bridge = None
    uno_forward_task = None
    try:
        if not args.no_uno:
            from uno_q_bridge import UnoQBridge

            uno_bridge = UnoQBridge(
                camera_dir,
                device=args.uno_device,
                publisher=partial(publish_context, eeg_reader),
            )
            print(f"正在头环连接前预扫描 UNO Q：{args.uno_device}...")
            if not await uno_bridge.pre_discover():
                raise SystemExit(
                    "未发现 UNO Q 的 FocusFlow BLE 广播。请先启动 UNO Q 服务端，"
                    "或使用 --no-uno 仅调试电脑端。"
                )
            cached = uno_bridge.resolved_device
            print(
                "UNO Q 已缓存："
                f"{getattr(cached, 'name', args.uno_device)} "
                f"({getattr(cached, 'address', '地址未知')})"
            )

        # Finish Windows BLE/GATT setup before importing and starting MediaPipe.
        # Camera work can otherwise delay WinRT callbacks during the handshake.
        print("先连接 Flowtime，连接成功后再启动摄像头和屏幕检测...")
        eeg_task = asyncio.create_task(eeg_reader.main())
        await wait_for_eeg_connection(eeg_reader, eeg_task)
        print("Flowtime BLE 已就绪，正在启动摄像头和屏幕检测...")

        if uno_bridge is not None:
            await uno_bridge.start()
            uno_forward_task = asyncio.create_task(uno_bridge.forward_loop(eeg_reader))
            print(
                f"UNO Q 桥接已启动：使用预扫描设备直连 {args.uno_device}；"
                "连接后每秒发送一次 decision_update"
            )
        else:
            print("UNO Q 桥接已禁用（--no-uno）")

        from eye_tracker import EyeTracker
        from screen_monitor import ScreenMonitor, ScreenState

        eye_tracker = EyeTracker(
            camera_id=args.camera_id,
            fps=args.fps,
            enable_logging=False,
        )
        screen_monitor = ScreenMonitor(
            api_key=api_key,
            interval=args.screen_interval,
            enable_api=bool(api_key),
            enable_logging=False,
        )
        if not eye_tracker.start():
            raise SystemExit("摄像头启动失败，请检查是否被其他程序占用")
        screen_monitor.start()

        await wait_for_camera(eye_tracker)
        await calibrate_camera(eye_tracker, args.calibrate)
        context_task = asyncio.create_task(
            context_loop(eeg_reader, eye_tracker, screen_monitor, ScreenState)
        )
        print("屏幕、摄像头和脑电模块已进入统一监测流程")
        await eeg_task
    finally:
        if uno_forward_task is not None:
            uno_forward_task.cancel()
            await asyncio.gather(uno_forward_task, return_exceptions=True)
        if uno_bridge is not None:
            await uno_bridge.stop()
        if context_task is not None:
            context_task.cancel()
            await asyncio.gather(context_task, return_exceptions=True)
        if screen_monitor is not None:
            screen_monitor.stop()
        if eye_tracker is not None:
            eye_tracker.stop()
        if eeg_task is not None and not eeg_task.done():
            eeg_task.cancel()
            await asyncio.gather(eeg_task, return_exceptions=True)
        http_server.shutdown()
        http_server.server_close()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已退出完整电脑端监测")
    except (asyncio.TimeoutError, TimeoutError):
        print("\nFlowtime BLE 连接/订阅超时，程序已安全清理并退出。")
        print("请关闭再打开头环，等待 10 秒后重新运行；无需修改设备 ID。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
