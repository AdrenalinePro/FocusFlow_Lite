"""Command-line serial test for the UNO Q Linux side.

Counterpart of ``Laptop/serial_test.py``.  Opens the USB-serial device
and runs the protocol state machine over newline-delimited JSON frames.
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

# Allow both ``python3 -m linux.linux_serial_test`` and
# ``python3 linux_serial_test.py``.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from linux_serial_service import (  # type: ignore
        LinuxSerialService,
        SerialServerConfig,
        SerialServerState,
    )
else:
    from .linux_serial_service import (
        LinuxSerialService,
        SerialServerConfig,
        SerialServerState,
    )

LOGGER = logging.getLogger("linux.serial.test")


def timestamp() -> str:
    return time.strftime("%H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000)


def log_tx(msg_type: str, seq: int, payload: bytes, data: dict) -> None:
    LOGGER.info(
        "[%s] [TX ->] %-18s seq=%-4s size=%s",
        timestamp(), msg_type, seq, len(payload),
    )


def log_rx(message: Any) -> None:
    LOGGER.info(
        "[%s] [RX <-] %-18s seq=%-4s data=%s",
        timestamp(), message.type, message.seq,
        json.dumps(message.data, ensure_ascii=False, separators=(", ", ": "), sort_keys=True),
    )


def log_event(message: str) -> None:
    LOGGER.info("[%s] [EVT ] %s", timestamp(), message)


def log_fail(message: str) -> None:
    LOGGER.error("[%s] [FAIL] %s", timestamp(), message)


def log_ok(message: str) -> None:
    LOGGER.info("[%s] [OK  ] %s", timestamp(), message)


class TestSummary:
    def __init__(self) -> None:
        self.received: Counter[str] = Counter()
        self.transmitted: Counter[str] = Counter()
        self.errors = 0
        self.last_error = ""
        self.running = False
        self.running_event = asyncio.Event()

    def on_state(self, state: SerialServerState) -> None:
        if state == SerialServerState.RUNNING:
            self.running = True
            self.running_event.set()
            log_ok("串口服务已启动")
        elif state == SerialServerState.ERROR:
            log_fail("串口服务异常")
        elif state == SerialServerState.STOPPED:
            log_event("串口服务已停止")

    def on_message(self, message: Any) -> None:
        self.received[message.type] += 1
        log_rx(message)

    def on_error(self, message: str) -> None:
        self.errors += 1
        self.last_error = str(message)
        log_fail(message)

    def on_sent(self, msg_type: str, payload: bytes, data: dict) -> None:
        self.transmitted[msg_type] += 1
        log_tx(msg_type, -1, payload, data)


async def run_test(args: argparse.Namespace) -> int:
    config = SerialServerConfig(
        serial_port=args.serial_port,
        baudrate=args.baud,
        focus_score_interval=args.focus_score_interval,
        rest_countdown_interval=args.rest_interval,
        device_status_interval=args.device_status_interval,
        background_loops=not args.no_background_loops,
    )
    server = LinuxSerialService(config)

    summary = TestSummary()
    server.add_connection_handler(summary.on_state)
    server.add_message_handler(summary.on_message)
    server.add_error_handler(summary.on_error)

    # Hook ``send_message`` to log every outgoing frame.
    original_send = server.send_message

    async def logging_send(msg_type: str, data: dict) -> bool:
        # Snapshot the payload for logging before it hits the wire.
        payload = b""
        try:
            from linux_serial_service import _encode_frame
            # Cross-package re-export from windows_ble_protocol (works on the
            # UNO Q deployment as confirmed by the user).
            from linux_ble_protocol import encode_downlink  # type: ignore[import-not-found]
            payload = _encode_frame(encode_downlink(msg_type, data, server.next_seq()))
        except Exception:
            pass
        result = await original_send(msg_type, data)
        if result:
            summary.on_sent(msg_type, payload, data)
        return result

    server.send_message = logging_send  # type: ignore[method-assign]

    log_event("启动串口服务 (port=%s, baud=%s)" % (args.serial_port, args.baud))
    server_task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(summary.running_event.wait(), timeout=5.0)
        log_ok("串口已打开，等待 Windows 端数据...")

        if args.interactive:
            await interactive_shell(server)
        elif args.duration > 0:
            log_event("运行 %ds 后自动结束；按 Ctrl+C 中断" % args.duration)
            await asyncio.sleep(args.duration)
        else:
            log_event("持续运行中，按 Ctrl+C 结束。")
            await asyncio.Event().wait()
    except asyncio.TimeoutError:
        log_fail("串口打开超时：请确认 /dev/ttyGS0 可用于读写。")
        return 1
    except KeyboardInterrupt:
        log_event("收到 Ctrl+C，正在停止...")
    finally:
        await server.stop()
        await server_task

    print_summary(summary)
    return _evaluate(summary)


def print_summary(summary: TestSummary) -> None:
    log_event("==================================")
    log_event("接收: %d 条" % sum(summary.received.values()))
    for msg_type, count in summary.received.items():
        log_event("  RX %-20s x %d" % (msg_type, count))
    log_event("发送: %d 条" % sum(summary.transmitted.values()))
    for msg_type, count in summary.transmitted.items():
        log_event("  TX %-20s x %d" % (msg_type, count))
    log_event("错误数量: %d" % summary.errors)


def _evaluate(summary: TestSummary) -> int:
    if summary.errors:
        log_fail("RESULT: FAIL (errors=%d)" % summary.errors)
        return 1
    log_ok("RESULT: PASS")
    return 0


async def interactive_shell(server: LinuxSerialService) -> None:
    """Console REPL for sending downlink messages manually."""
    log_event("交互模式；输入: state、score、rest、display、quit")
    while True:
        command = (await asyncio.to_thread(input, "serial> ")).strip().lower()
        if command in {"quit", "exit", "q"}:
            return
        parts = command.split()
        head = parts[0] if parts else ""
        try:
            if head == "state" and len(parts) >= 2 and parts[1] in {
                "focused", "distracted", "procrastinating", "resting",
            }:
                await server.send_state_update(
                    state=parts[1], focus_score=80,
                    prev_state=server.driver.current_state,
                    duration_in_state=0.0,
                    triggered_feedback="vibrate_short",
                )
            elif head == "score":
                try:
                    value = int(parts[1]) if len(parts) >= 2 else 75
                except ValueError:
                    log_fail("用法: score <0..100>")
                    continue
                await server.send_focus_score(value, server.driver.current_state)
            elif head == "rest":
                action = parts[1] if len(parts) >= 2 else "query"
                if action not in {"start", "stop", "extend", "query"}:
                    log_fail("用法: rest <start|stop|extend|query> [duration]")
                    continue
                duration = int(parts[2]) if len(parts) >= 3 else None
                await server._handle_rest_command(
                    {"action": action, **({"duration": duration} if duration else {})},
                )
            elif head == "display":
                await server.send_display_content(
                    line1="Interactive", line2=time.strftime("%H:%M:%S"),
                    line3="score %d" % server.driver.focus_score,
                    line4=server.driver.current_state,
                )
            else:
                log_fail("未知命令。可用: state, score, rest, display, quit")
        except Exception as exc:
            log_fail("命令执行失败: %s" % exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FocusFlow UNO Q 串口通信测试")
    parser.add_argument("--serial-port", default="/dev/ttyGS0",
                        help="串口设备路径，默认 /dev/ttyGS0")
    parser.add_argument("--baud", type=int, default=115200,
                        help="波特率，默认 115200")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="运行秒数；0 表示持续运行")
    parser.add_argument("--rest-interval", type=float, default=10.0)
    parser.add_argument("--focus-score-interval", type=float, default=1.0)
    parser.add_argument("--device-status-interval", type=float, default=30.0)
    parser.add_argument("--no-background-loops", action="store_true")
    parser.add_argument("--interactive", action="store_true",
                        help="进入交互命令模式")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", stream=sys.stdout,
    )
    try:
        return asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
