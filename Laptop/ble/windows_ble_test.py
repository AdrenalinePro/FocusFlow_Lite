"""Command-line hardware test for Windows <-> UNO Q BLE communication.

Examples (run from the repository root)::

    python ble/windows_ble_test.py --scan-only
    python ble/windows_ble_test.py --device UNO-Q-FF01 --duration 30
    python ble/windows_ble_test.py --device UNO-Q-FF01 --stream-eye --stream-screen
    python ble/windows_ble_test.py --device UNO-Q-FF01 --log-file ble_test.log --verbose

The script intentionally uses the same WindowsBLEClient as the application,
so a successful test exercises the real protocol validation, Notify handling,
heartbeat timeout, and reconnect path.

Logging
-------
* Console output is written through the ``logging`` module so that it can be
  filtered by ``--verbose`` and includes millisecond-precision timestamps.
* ``--log-file PATH`` mirrors every log line to a UTF-8 file in addition to
  the console, which is convenient for sharing debug logs.
* ``--verbose`` raises the root logger to ``DEBUG`` and also enables
  per-message TX logging, payload hex dumps, and bleak internal traces.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

# Allow both ``python -m ble.windows_ble_test`` and
# ``python ble/windows_ble_test.py`` from the repository root.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ble.windows_ble_client import (  # type: ignore
        BleClientConfig,
        BleConnectionState,
        WindowsBLEClient,
    )
else:
    from .windows_ble_client import BleClientConfig, BleConnectionState, WindowsBLEClient


CONSOLE_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s"
)
DATE_FORMAT = "%H:%M:%S"

STATE_LABELS = {
    BleConnectionState.STOPPED.value: "已停止",
    BleConnectionState.CONNECTING.value: "正在连接",
    BleConnectionState.CONNECTED.value: "已连接",
    BleConnectionState.DISCONNECTED.value: "已断开",
    BleConnectionState.RECONNECTING.value: "正在重连",
    BleConnectionState.ERROR.value: "错误",
}

LOGGER = logging.getLogger("ble.test")


class TestSummary:
    """Collect per-test metrics and log rich, structured lines as events arrive."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.connected = False
        self.received: Counter[str] = Counter()
        self.transmitted: Counter[str] = Counter()
        self.errors = 0
        self.last_error = ""
        self.connect_started_at: Optional[float] = None
        self.connected_at: Optional[float] = None
        self.disconnected_at: Optional[float] = None
        self.bytes_rx = 0
        self.bytes_tx = 0
        self.connected_event = asyncio.Event()

    # ----- state / message / error handlers --------------------------------

    def on_state(self, state: BleConnectionState) -> None:
        value = state.value if isinstance(state, BleConnectionState) else str(state)
        now = time.monotonic()
        label = STATE_LABELS.get(value, value)
        if value == BleConnectionState.CONNECTING.value and self.connect_started_at is None:
            self.connect_started_at = now
        if value == BleConnectionState.CONNECTED.value:
            self.connected = True
            self.connected_at = now
            self.connected_event.set()
            elapsed = (now - self.connect_started_at) if self.connect_started_at else 0.0
            LOGGER.info(
                "连接状态 -> %s（%s，建立耗时 %.2fs）",
                value, label, elapsed,
            )
        elif value == BleConnectionState.DISCONNECTED.value:
            self.disconnected_at = now
            self.connected = False
            LOGGER.warning("连接状态 -> %s（%s）", value, label)
        elif value == BleConnectionState.RECONNECTING.value:
            LOGGER.warning(
                "连接状态 -> %s（%s）", value, label,
            )
        elif value == BleConnectionState.ERROR.value:
            LOGGER.error("连接状态 -> %s（%s）", value, label)
        else:
            LOGGER.info("连接状态 -> %s（%s）", value, label)

    def on_message(self, message: Any) -> None:
        self.received[message.type] += 1
        payload_text = json.dumps(
            message.data, ensure_ascii=False, separators=(", ", ": "), sort_keys=True,
        )
        self.bytes_rx += len(payload_text)
        LOGGER.info(
            "RX %s seq=%s ts=%s data=%s",
            message.type, message.seq, message.ts, payload_text,
        )

    def on_error(self, message: str) -> None:
        self.errors += 1
        self.last_error = str(message)
        LOGGER.error("BLE 错误: %s", message)

    # ----- outgoing side, wired up by the test driver -----------------------

    def on_transmit(self, msg_type: str, payload: bytes) -> None:
        self.transmitted[msg_type] += 1
        self.bytes_tx += len(payload)
        if self.verbose:
            LOGGER.debug(
                "TX %s (%d 字节) 十六进制=%s",
                msg_type, len(payload), payload.hex(),
            )
        else:
            LOGGER.debug("TX %s (%d 字节)", msg_type, len(payload))


def configure_logging(verbose: bool, log_file: Optional[Path]) -> None:
    """Reset root loggers and attach a console handler + optional file handler."""

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    # Drop any handlers that basicConfig() may have left behind on re-entry.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(file_handler)
        LOGGER.info("日志将同时写入 %s", log_file)

    # Quiet bleak unless the user asked for verbose mode; otherwise everything
    # above DEBUG becomes noisy and unreadable.
    logging.getLogger("bleak").setLevel(logging.DEBUG if verbose else logging.WARNING)


async def scan_devices(timeout: float) -> int:
    try:
        from bleak import BleakScanner
    except ImportError:
        LOGGER.error("bleak 未安装，请执行: python -m pip install -r ble\\requirements-windows.txt")
        return 2

    LOGGER.info("开始扫描 BLE 设备，超时 %.1f 秒...", timeout)
    started = time.monotonic()
    devices = await BleakScanner.discover(timeout=timeout)
    elapsed = time.monotonic() - started
    if not devices:
        LOGGER.warning("未发现 BLE 设备（耗时 %.2fs）。请确认 UNO Q 正在广播，并检查 Windows 蓝牙适配器。", elapsed)
        return 1
    LOGGER.info("扫描完成（耗时 %.2fs），共发现 %d 个设备:", elapsed, len(devices))
    for device in devices:
        rssi = getattr(device, "rssi", None)
        rssi_text = "RSSI=%s dBm" % rssi if rssi is not None else "RSSI=未知"
        LOGGER.info("  - %-24s %s  [%s]", device.name or "<无名称>", device.address, rssi_text)
    return 0


async def send_sample_messages(client: WindowsBLEClient, args: argparse.Namespace) -> None:
    """Send safe protocol samples after the link and Notify are ready."""

    LOGGER.info("发送 eye_data 样例...")
    await client.send_eye_data(
        yaw=5.2,
        pitch=-3.1,
        is_focused=1,
        state_duration=2.5,
        confidence=0.95,
    )
    LOGGER.info("发送 screen_data 样例...")
    await client.send_screen_data(
        state="focused",
        confidence=0.92,
        app="FocusFlow BLE Test",
        category="work",
    )
    if args.rest_action:
        LOGGER.info("发送 rest_command(%s)...", args.rest_action)
        duration: Optional[int] = None
        if args.rest_action in {"start", "extend"}:
            duration = args.rest_duration
        await client.send_rest_command(args.rest_action, duration, args.reason)


async def eye_stream(client: WindowsBLEClient, hz: float, stop_event: asyncio.Event) -> None:
    interval = 1.0 / hz
    started = time.monotonic()
    while not stop_event.is_set():
        await client.send_eye_data(5.2, -3.1, 1, time.monotonic() - started, 0.95)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def screen_stream(client: WindowsBLEClient, interval: float, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await client.send_screen_data("focused", 0.92, "FocusFlow BLE Test", "work")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def interactive_shell(client: WindowsBLEClient) -> None:
    """Optional console commands without blocking the BLE event loop."""

    LOGGER.info("进入交互模式；支持: eye、screen、sync、rest start|stop|extend|query、quit")
    while True:
        command = (await asyncio.to_thread(input, "ble> ")).strip().lower()
        if command in {"quit", "exit", "q"}:
            LOGGER.info("退出交互模式")
            return
        if command == "eye":
            await client.send_eye_data(5.2, -3.1, 1, 2.5, 0.95)
        elif command == "screen":
            await client.send_screen_data("focused", 0.92, "FocusFlow BLE Test", "work")
        elif command == "sync":
            await client.send_sync_request(["all"])
        elif command.startswith("rest "):
            action = command.split(maxsplit=1)[1]
            if action in {"start", "extend"}:
                await client.send_rest_command(action, 30, "manual")
            elif action in {"stop", "query"}:
                await client.send_rest_command(action)
            else:
                LOGGER.warning("支持: rest start|stop|extend|query")
        elif command:
            LOGGER.warning("未知命令 %r。支持: eye、screen、sync、rest start|stop|query、quit", command)


class TransmitLogger:
    """Wrap the BLE client so that outgoing payloads surface in the test log.

    The wrapper exposes the same ``send_*`` coroutines the underlying client
    provides, but every payload is encoded locally so its byte length and hex
    contents can be logged before being written to the GATT characteristic.
    """

    def __init__(self, client: WindowsBLEClient, summary: TestSummary) -> None:
        self._client = client
        self._summary = summary
        self._original_send = client.send_message

    async def _tracked_send(self, msg_type: str, data: dict) -> bool:
        from .windows_ble_protocol import encode_message
        payload = encode_message(msg_type, data, self._client.next_seq())
        self._summary.on_transmit(msg_type, payload)
        return await self._original_send(msg_type, data)

    # Re-bind each high-level helper so it routes through ``_tracked_send``
    # instead of ``WindowsBLEClient.send_message`` directly.
    async def send_eye_data(self, yaw: float, pitch: float, is_focused: int,
                            state_duration: float, confidence: float) -> bool:
        return await self._tracked_send("eye_data", {
            "yaw": round(yaw, 2), "pitch": round(pitch, 2),
            "is_focused": int(is_focused),
            "state_duration": round(state_duration, 2),
            "confidence": round(confidence, 2),
        })

    async def send_screen_data(self, state: str, confidence: float,
                               app: Optional[str] = None,
                               category: Optional[str] = None) -> bool:
        data: dict = {"state": state, "confidence": round(confidence, 2)}
        if app is not None:
            data["app"] = app
        if category is not None:
            data["category"] = category
        return await self._tracked_send("screen_data", data)

    async def send_rest_command(self, action: str, duration: Optional[int] = None,
                                reason: Optional[str] = "manual") -> bool:
        data: dict = {"action": action}
        if duration is not None:
            data["duration"] = duration
        if reason is not None:
            data["reason"] = reason
        return await self._tracked_send("rest_command", data)

    async def send_heartbeat(self, uptime: Optional[int] = None) -> bool:
        if uptime is None:
            uptime = int(time.monotonic())
        return await self._tracked_send("heartbeat", {"uptime": uptime})

    async def send_sync_request(self, fields: Optional[list] = None) -> bool:
        data: dict = {}
        if fields is not None:
            data["fields"] = fields
        return await self._tracked_send("sync_request", data)

    async def stop(self) -> None:
        await self._client.stop()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)


async def run_test(args: argparse.Namespace) -> int:
    reconnect_attempts = None if args.reconnect_attempts == 0 else args.reconnect_attempts
    config = BleClientConfig(
        device=args.device,
        scan_timeout=args.scan_timeout,
        connect_timeout=args.connect_timeout,
        max_reconnect_attempts=reconnect_attempts,
        write_with_response=args.write_with_response,
    )
    LOGGER.info("启动 BLE 测试：device=%s scan_timeout=%.1fs connect_timeout=%.1fs "
                "max_reconnect_attempts=%s write_with_response=%s",
                config.device, config.scan_timeout, config.connect_timeout,
                config.max_reconnect_attempts, config.write_with_response)

    summary = TestSummary(verbose=args.verbose)
    raw_client = WindowsBLEClient(config)
    raw_client.add_state_handler(summary.on_state)
    raw_client.add_message_handler(summary.on_message)
    raw_client.add_error_handler(summary.on_error)

    client = TransmitLogger(raw_client, summary)

    ble_task = asyncio.create_task(raw_client.run_forever())
    stream_stop = asyncio.Event()
    stream_tasks = []
    test_started = time.monotonic()
    try:
        try:
            await asyncio.wait_for(summary.connected_event.wait(), timeout=args.connect_timeout)
        except asyncio.TimeoutError:
            LOGGER.error("连接超时：请确认设备名/地址、UNO Q 广播状态和 Windows 蓝牙权限。")
            return 1

        # run_forever subscribes Notify before signalling sync completion; give
        # the event loop a short turn before sending user test traffic.
        await asyncio.sleep(0.2)
        if not args.no_sample_messages:
            await send_sample_messages(client, args)
        if args.stream_eye:
            stream_tasks.append(asyncio.create_task(eye_stream(client, args.eye_hz, stream_stop)))
            LOGGER.info("已开启 eye_data 流（%.2f Hz）", args.eye_hz)
        if args.stream_screen:
            stream_tasks.append(asyncio.create_task(screen_stream(client, args.screen_interval, stream_stop)))
            LOGGER.info("已开启 screen_data 流（间隔 %.2fs）", args.screen_interval)

        if args.interactive:
            await interactive_shell(client)
        elif args.duration > 0:
            LOGGER.info("进入等待阶段，%.1fs 后自动结束（Ctrl+C 可提前退出）", args.duration)
            await asyncio.sleep(args.duration)
        else:
            LOGGER.info("持续运行中，按 Ctrl+C 结束测试。")
            await asyncio.Event().wait()
    except KeyboardInterrupt:
        LOGGER.warning("收到 Ctrl+C，正在停止...")
    finally:
        stream_stop.set()
        for task in stream_tasks:
            task.cancel()
        if stream_tasks:
            await asyncio.gather(*stream_tasks, return_exceptions=True)
        await raw_client.stop()
        await ble_task

    summary.elapsed = time.monotonic() - test_started
    print_summary(summary)
    # A real bidirectional test must receive at least one heartbeat response.
    if not summary.connected or summary.received["heartbeat"] == 0 or summary.errors:
        if summary.received["heartbeat"] == 0:
            reason = "未收到 heartbeat 响应"
        else:
            reason = "测试期间出现 BLE/协议错误"
        LOGGER.error("RESULT: FAIL（%s）", reason)
        return 1
    LOGGER.info("RESULT: PASS（已建立连接并收到 heartbeat 响应）")
    return 0


def print_summary(summary: TestSummary) -> None:
    elapsed = getattr(summary, "elapsed", 0.0)
    LOGGER.info(
        "\n========== BLE 测试汇总 ==========\n"
        "  运行耗时: %.2fs\n"
        "  收到的消息: %s\n"
        "  发送的消息: %s\n"
        "  RX 字节数: %d\n"
        "  TX 字节数: %d\n"
        "  错误数量: %d\n"
        "  最后错误: %s\n"
        "==================================",
        elapsed,
        dict(summary.received) if summary.received else "无",
        dict(summary.transmitted) if summary.transmitted else "无",
        summary.bytes_rx,
        summary.bytes_tx,
        summary.errors,
        summary.last_error or "无",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FocusFlow Windows <-> UNO Q BLE 通信测试")
    parser.add_argument("--device", default="UNO-Q-FF01", help="设备名或 Windows BLE 地址")
    parser.add_argument("--scan-only", action="store_true", help="只扫描设备，不建立连接")
    parser.add_argument("--scan-timeout", type=float, default=10.0, help="扫描超时秒数，默认 10")
    parser.add_argument("--connect-timeout", type=float, default=10.0, help="单次连接超时秒数，默认 10")
    parser.add_argument("--duration", type=float, default=30.0, help="连接后运行秒数；0 表示持续运行")
    parser.add_argument("--no-sample-messages", action="store_true", help="不发送 eye/screen/rest 样例，只测试同步和心跳")
    parser.add_argument("--rest-action", choices=["start", "stop", "extend", "query"], default="query",
                        help="连接后发送的休息指令，默认 query；start/extend 会改变设备状态")
    parser.add_argument("--rest-duration", type=int, default=30, help="start/extend 的秒数，默认 30")
    parser.add_argument("--reason", default="manual", help="rest_command reason，默认 manual")
    parser.add_argument("--stream-eye", action="store_true", help="持续发送 eye_data")
    parser.add_argument("--eye-hz", type=float, default=5.0, help="eye_data 频率，默认 5 Hz")
    parser.add_argument("--stream-screen", action="store_true", help="持续发送 screen_data")
    parser.add_argument("--screen-interval", type=float, default=2.0, help="screen_data 间隔秒数，默认 2")
    parser.add_argument("--interactive", action="store_true", help="连接后进入交互命令模式")
    parser.add_argument("--reconnect-attempts", type=int, default=5,
                        help="单次断线最大重连次数；0 表示无限重连，默认 5")
    parser.add_argument("--write-with-response", action="store_true", help="使用 GATT Write With Response")
    parser.add_argument("--verbose", action="store_true", help="显示 DEBUG 级别日志、TX 字节和 bleak 内部日志")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="将完整日志额外写入该文件（含 DEBUG 信息），便于离线分析")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.eye_hz <= 0:
        raise ValueError("--eye-hz 必须大于 0")
    if args.screen_interval <= 0:
        raise ValueError("--screen-interval 必须大于 0")
    if args.rest_duration <= 0:
        raise ValueError("--rest-duration 必须大于 0")
    if args.reconnect_attempts < 0:
        raise ValueError("--reconnect-attempts 不能小于 0")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    configure_logging(args.verbose, args.log_file)
    if args.scan_only:
        return asyncio.run(scan_devices(args.scan_timeout))
    try:
        return asyncio.run(run_test(args))
    except KeyboardInterrupt:
        LOGGER.warning("测试已取消。")
        return 130
    except Exception as exc:
        LOGGER.exception("测试异常: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())