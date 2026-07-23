"""Command-line serial test for Windows <-> UNO Q communication.

Uses the same message types and validation as ``windows_ble_test.py`` but
transports everything over the USB serial port instead of BLE GATT.

Examples (run from the repository root)::

    # Auto-detect the Arduino COM port
    python serial_test.py --duration 30

    # Specify the port explicitly
    python serial_test.py --serial-port COM3 --duration 30

    # Stream simulated eye + screen traffic
    python serial_test.py --stream-eye --stream-screen --duration 60

    # Interactive mode
    python serial_test.py --interactive --duration 0
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

# Make ``Laptop/`` importable when run as ``python serial_test.py``.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ble.windows_ble_protocol import encode_message  # noqa: E402
from serial_client import SerialFocusFlowClient  # noqa: E402

LOGGER = logging.getLogger("serial.test")

CONSOLE_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"

STATE_LABELS = {
    "stopped": "已停止",
    "running": "运行中",
    "error": "错误",
    "connected": "已连接",
}


class TestSummary:
    """Collect per-test metrics, same shape as the BLE TestSummary."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.connected = False
        self.received: Counter[str] = Counter()
        self.transmitted: Counter[str] = Counter()
        self.errors = 0
        self.last_error = ""
        self.bytes_rx = 0
        self.bytes_tx = 0
        self.connected_event = asyncio.Event()

    def on_state(self, state: str) -> None:
        value = state if isinstance(state, str) else str(state)
        label = STATE_LABELS.get(value, value)
        if value == "connected":
            self.connected = True
            self.connected_event.set()
            LOGGER.info("连接状态 -> %s（%s）", value, label)
        elif value == "error":
            LOGGER.error("连接状态 -> %s（%s）", value, label)
        else:
            LOGGER.info("连接状态 -> %s（%s）", value, label)

    def on_message(self, message: Any) -> None:
        self.received[message.type] += 1
        payload_text = json.dumps(
            message.data, ensure_ascii=False, separators=(", ", ": "),
            sort_keys=True,
        )
        self.bytes_rx += len(payload_text)
        LOGGER.info(
            "RX %s seq=%s ts=%s data=%s",
            message.type, message.seq, message.ts, payload_text,
        )

    def on_error(self, message: str) -> None:
        self.errors += 1
        self.last_error = str(message)
        LOGGER.error("串口错误: %s", message)

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


class TransmitLogger:
    """Wrap the client so outgoing payloads appear in the test log."""

    def __init__(self, client: SerialFocusFlowClient, summary: TestSummary) -> None:
        self._client = client
        self._summary = summary
        self._original_send = client.send_message

    async def _tracked_send(self, msg_type: str, data: dict) -> bool:
        payload = encode_message(msg_type, data, self._client.next_seq())
        self._summary.on_transmit(msg_type, payload)
        return await self._original_send(msg_type, data)

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
        import time as _time
        return await self._tracked_send("heartbeat", {
            "uptime": uptime if uptime is not None else int(_time.monotonic()),
        })

    async def send_sync_request(self, fields: Optional[list] = None) -> bool:
        data: dict = {}
        if fields is not None:
            data["fields"] = fields
        return await self._tracked_send("sync_request", data)

    async def stop(self) -> None:
        await self._client.stop()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)


# ------------------------------------------------------------------
# sample traffic
# ------------------------------------------------------------------

async def send_sample_messages(client: Any, args: argparse.Namespace) -> None:
    """One-shot demo traffic to exercise the wire."""
    LOGGER.info("发送 eye_data 样例...")
    await client.send_eye_data(5.2, -3.1, 1, 2.5, 0.95)
    LOGGER.info("发送 screen_data 样例...")
    await client.send_screen_data("focused", 0.92, "FocusFlow Serial Test", "work")
    if args.rest_action:
        LOGGER.info("发送 rest_command(%s)...", args.rest_action)
        duration = (
            args.rest_duration if args.rest_action in {"start", "extend"} else None
        )
        await client.send_rest_command(args.rest_action, duration, args.reason)


async def eye_stream(client: Any, hz: float, stop_event: asyncio.Event) -> None:
    interval = 1.0 / hz
    started = time.monotonic()
    while not stop_event.is_set():
        await client.send_eye_data(5.2, -3.1, 1, time.monotonic() - started, 0.95)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def screen_stream(client: Any, interval: float,
                        stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await client.send_screen_data("focused", 0.92, "FocusFlow Serial Test", "work")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def interactive_shell(client: Any) -> None:
    LOGGER.info("进入交互模式；支持: eye、screen、sync、rest start|stop|extend|query、quit")
    while True:
        command = (await asyncio.to_thread(input, "serial> ")).strip().lower()
        if command in {"quit", "exit", "q"}:
            LOGGER.info("退出交互模式")
            return
        if command == "eye":
            await client.send_eye_data(5.2, -3.1, 1, 2.5, 0.95)
        elif command == "screen":
            await client.send_screen_data("focused", 0.92, "FocusFlow Serial Test", "work")
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
            LOGGER.warning(
                "未知命令 %r。支持: eye、screen、sync、rest start|stop|query、quit",
                command,
            )


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------

async def run_test(args: argparse.Namespace) -> int:
    summary = TestSummary(verbose=args.verbose)

    raw_client = SerialFocusFlowClient(port=args.serial_port, baudrate=args.baud)
    raw_client.add_state_handler(summary.on_state)
    raw_client.add_message_handler(summary.on_message)
    raw_client.add_error_handler(summary.on_error)

    client = TransmitLogger(raw_client, summary)

    LOGGER.info(
        "启动串口测试：port=%s baud=%s",
        args.serial_port or "<auto>", args.baud,
    )

    serial_task = asyncio.create_task(raw_client.run_forever())

    # Wait for the serial port to open.
    try:
        await asyncio.wait_for(summary.connected_event.wait(), timeout=5.0)
        LOGGER.info("串口已就绪，开始收发测试")
    except asyncio.TimeoutError:
        LOGGER.error("串口打开超时：请确认 UNO Q 已通过 USB 连接。")
        return 1

    stream_stop = asyncio.Event()
    stream_tasks: list[asyncio.Task] = []
    test_started = time.monotonic()
    try:
        if not args.no_sample_messages:
            await send_sample_messages(client, args)
        if args.stream_eye:
            stream_tasks.append(asyncio.create_task(
                eye_stream(client, args.eye_hz, stream_stop),
            ))
            LOGGER.info("已开启 eye_data 流（%.2f Hz）", args.eye_hz)
        if args.stream_screen:
            stream_tasks.append(asyncio.create_task(
                screen_stream(client, args.screen_interval, stream_stop),
            ))
            LOGGER.info("已开启 screen_data 流（间隔 %.2fs）", args.screen_interval)

        if args.interactive:
            await interactive_shell(client)
        elif args.duration > 0:
            LOGGER.info("运行 %ds 后自动结束（Ctrl+C 可提前退出）", args.duration)
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
        await serial_task

    summary.elapsed = time.monotonic() - test_started  # type: ignore[attr-defined]
    print_summary(summary)
    return 0 if summary.errors == 0 else 1


def print_summary(summary: TestSummary) -> None:
    elapsed = getattr(summary, "elapsed", 0.0)
    LOGGER.info("==================================")
    LOGGER.info("测试时长: %.1f 秒", elapsed)
    LOGGER.info("发送: %d 条 (%d 字节)", sum(summary.transmitted.values()), summary.bytes_tx)
    for msg_type, count in summary.transmitted.items():
        LOGGER.info("  TX %-20s x %d", msg_type, count)
    LOGGER.info("接收: %d 条 (%d 字节)", sum(summary.received.values()), summary.bytes_rx)
    for msg_type, count in summary.received.items():
        LOGGER.info("  RX %-20s x %d", msg_type, count)
    LOGGER.info("错误: %d", summary.errors)
    if summary.last_error:
        LOGGER.info("最后错误: %s", summary.last_error)
    if summary.errors:
        LOGGER.warning("RESULT: FAIL (errors=%d)", summary.errors)
    else:
        LOGGER.info("RESULT: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FocusFlow 串口通信测试")
    parser.add_argument("--serial-port", default=None,
                        help="COM 口号（如 COM3）；省略则自动探测 Arduino 设备")
    parser.add_argument("--baud", type=int, default=115200,
                        help="波特率，默认 115200")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="运行秒数；0 表示持续运行")
    parser.add_argument("--no-sample-messages", action="store_true",
                        help="不发送 eye/screen/rest 样例消息")
    parser.add_argument("--rest-action", choices=["start", "stop", "extend", "query"],
                        default="query", help="样例休息指令，默认 query")
    parser.add_argument("--rest-duration", type=int, default=30,
                        help="start/extend 时的休息时长秒数")
    parser.add_argument("--reason", default="manual", help="rest_command 原因")
    parser.add_argument("--stream-eye", action="store_true",
                        help="持续发送 eye_data")
    parser.add_argument("--eye-hz", type=float, default=5.0,
                        help="eye_data 频率，默认 5 Hz")
    parser.add_argument("--stream-screen", action="store_true",
                        help="持续发送 screen_data")
    parser.add_argument("--screen-interval", type=float, default=2.0,
                        help="screen_data 间隔秒数，默认 2")
    parser.add_argument("--interactive", action="store_true",
                        help="进入交互命令模式")
    parser.add_argument("--verbose", action="store_true",
                        help="显示 DEBUG 日志和 TX 字节")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format=CONSOLE_FORMAT, datefmt=DATE_FORMAT,
                        stream=sys.stdout)
    # Quiet pyserial's internal chatter.
    logging.getLogger("serial").setLevel(logging.WARNING)

    if args.eye_hz <= 0 or args.screen_interval <= 0 or args.rest_duration <= 0:
        parser.error("hz / interval / duration 必须大于 0")

    try:
        return asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
