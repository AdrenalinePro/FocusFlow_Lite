"""Command-line hardware test for the UNO Q Linux <-> Windows BLE link.

This script is the Linux counterpart of ``windows/windows_ble_test.py``
and is designed to run side-by-side with it.  When both scripts run at
once, every line carries the same ``[HH:MM:SS.mmm]`` timestamp prefix,
so the operator can ``grep`` / align the two logs and immediately see
which ``TX`` on this side corresponds to which ``RX`` on the Windows
side (and vice versa).

Log format::

    [10:00:15.234] [EVT ]  Windows client subscribed to TX Notify
    [10:00:15.235] [TX →] state_update seq=1  state=focused score=82 ...
    [10:00:15.345] [RX ←] sync_request   seq=1
    [10:00:15.346] [TX →] sync_response  seq=5  size=179

The three prefixes are::

    ``[TX →]``  downlink message we sent to the Windows client
    ``[RX ←]``  uplink message we received from the Windows client
    ``[EVT ]``  server-side event (advertising, Notify change, errors)

Typical usage::

    # 1. Make sure the UNO Q is powered and BlueZ is up
    python3 -m linux.linux_ble_test --scan-only

    # 2. Run the server, default device name UNO-Q-FF01, 30 second window
    python3 -m linux.linux_ble_test --duration 30

    # 3. In another terminal, run the Windows test script:
    #       python windows\\windows_ble_test.py --device UNO-Q-FF01 --duration 30
    #
    #    Both windows will show aligned timestamped lines that you can
    #    copy-paste side by side to confirm round-trip behaviour.

    # 4. Drive a longer regression with simulated samples
    python3 -m linux.linux_ble_test --duration 60 --stream-focus --stream-state

    # 5. Drop into interactive mode for ad-hoc commands
    python3 -m linux.linux_ble_test --interactive --duration 0
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

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from linux.linux_ble_server import (  # type: ignore
        BleServerConfig,
        BleServerState,
        LinuxBLEServer,
    )
    from linux.linux_ble_protocol import BLEMessage, DOWNLINK_TYPES  # type: ignore
else:
    from .linux_ble_server import (
        BleServerConfig,
        BleServerState,
        LinuxBLEServer,
    )
    from .linux_ble_protocol import BLEMessage, DOWNLINK_TYPES


# --- pretty logging --------------------------------------------------------

_PREFIX_WIDTH = 6  # "TX →" / "RX ←" / "EVT " / "FAIL" / "OK  "


def timestamp() -> str:
    """``HH:MM:SS.mmm`` so two logs can be aligned line-by-line."""

    return time.strftime("%H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000)


def log(prefix: str, message: str) -> None:
    sys.stdout.write("[%s] [%s] %s\n" % (timestamp(), prefix.ljust(_PREFIX_WIDTH), message))
    sys.stdout.flush()


def log_tx(msg_type: str, seq: int, payload: bytes, data: dict) -> None:
    log("TX →", "%-16s seq=%-4d size=%-3d  %s" % (
        msg_type, seq, len(payload), _summarise_data(data),
    ))


def log_rx(message: BLEMessage) -> None:
    log("RX ←", "%-16s seq=%-4d        %s" % (
        message.type, message.seq, _summarise_data(message.data),
    ))


def log_event(message: str) -> None:
    log("EVT", message)


def log_fail(message: str) -> None:
    log("FAIL", message)


def log_ok(message: str) -> None:
    log("OK  ", message)


def _summarise_data(data: dict) -> str:
    """Render the data dict as a single short line."""

    if not data:
        return "{}"
    keys = list(data.keys())
    if len(keys) <= 4:
        return " ".join("%s=%s" % (k, _short(data[k])) for k in keys)
    head = " ".join("%s=%s" % (k, _short(data[k])) for k in keys[:3])
    return head + " ... (%d fields)" % len(keys)


def _short(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= 24 else value[:21] + "..."
    if isinstance(value, dict):
        return "{%s}" % ", ".join(_short(k) + ":" + _short(v) for k, v in list(value.items())[:3])
    if isinstance(value, list):
        return "[%d items]" % len(value)
    if value is None:
        return "null"
    return type(value).__name__


# --- test summary -----------------------------------------------------------


class TestSummary:
    """Aggregates the activity that ``print_summary`` reports at the end.

    The same instance is used to evaluate PASS/FAIL so the operator can
    line up the numbers with what they see in the Windows test script
    (which reports ``收到的消息`` = the same counter).
    """

    def __init__(self) -> None:
        self.advertising = False
        self.notify_ready = False
        self.notify_subscribed_at: Optional[float] = None
        self.first_rx_at: Optional[float] = None
        self.first_rx_type: Optional[str] = None
        self.last_heartbeat_at: Optional[float] = None
        self.received = Counter()
        self.sent = Counter()
        self.errors = 0
        self.last_error = ""
        # Track round-trip heartbeats specifically: client seq -> echoed by us?
        self.heartbeat_echoes = 0
        self.heartbeat_seen = 0

    # ---- callbacks ----------------------------------------------------
    def on_state(self, state: BleServerState) -> None:
        value = state.value if isinstance(state, BleServerState) else str(state)
        log_event("server state -> %s" % value)
        if value == BleServerState.ADVERTISING.value:
            self.advertising = True
        elif value == BleServerState.NOTIFY_READY.value:
            self.notify_ready = True
            self.notify_subscribed_at = time.monotonic()

    def on_message(self, message: BLEMessage) -> None:
        self.received[message.type] += 1
        log_rx(message)
        if self.first_rx_at is None:
            self.first_rx_at = time.monotonic()
            self.first_rx_type = message.type
        if message.type == "heartbeat":
            self.heartbeat_seen += 1
            self.last_heartbeat_at = time.monotonic()

    def on_error(self, message: str) -> None:
        self.errors += 1
        self.last_error = str(message)
        log_fail("error: %s" % message)

    def on_sent(self, msg_type: str, payload: bytes, data: dict) -> None:
        self.sent[msg_type] += 1
        # ``seq`` is included in the data dict by ``send_message`` after
        # the encode step.  We peek at the raw payload so callers don't
        # need to know about the seq counter internals.
        try:
            seq = __import__("json").loads(payload)["seq"]
        except Exception:
            seq = -1
        log_tx(msg_type, int(seq), payload, data)
        if msg_type == "heartbeat" and isinstance(data, dict) and data.get("echo_seq") is not None:
            self.heartbeat_echoes += 1


# --- scan-only path ---------------------------------------------------------


async def scan_only(_: argparse.Namespace) -> int:
    """Verify dbus-fast and BlueZ are usable without starting a GATT server."""

    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType
    except ImportError:
        log_fail("dbus-fast 未安装。请执行: pip3 install --user dbus-fast jeepney")
        return 2
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        log_fail("无法连接系统 D-Bus：%s" % exc)
        log_fail("请确认 bluetoothd 在运行，且当前用户有权限访问系统 bus。")
        return 1
    try:
        introspection = await bus.introspect("org.bluez", "/")
        manager = bus.get_proxy_object(
            "org.bluez", "/", introspection
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        adapters = []
        for path, ifaces in objects.items():
            if "org.bluez.Adapter1" in ifaces:
                props = ifaces["org.bluez.Adapter1"]
                alias = props.get("Alias")
                if alias is not None:
                    adapters.append((path, str(alias.value)))
        if not adapters:
            log_fail("未发现 BlueZ adapter。请确认蓝牙控制器已插入并被驱动识别。")
            return 1
        log_ok("发现 %d 个 BlueZ adapter：" % len(adapters))
        for path, alias in adapters:
            log("    ", "%-32s alias=%s" % (path, alias))
        return 0
    finally:
        bus.disconnect()


# --- sample traffic generators --------------------------------------------


async def focus_score_stream(server: LinuxBLEServer, hz: float,
                             stop_event: asyncio.Event) -> None:
    """Stream noise on the focus_score channel to exercise TX timing."""

    interval = 1.0 / max(0.1, hz)
    while not stop_event.is_set():
        score = 60 + int((time.time() * 10) % 40)
        await server.send_focus_score(score, "focused")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def state_jitter(server: LinuxBLEServer, interval: float,
                       stop_event: asyncio.Event) -> None:
    """Cycle through the four protocol states to test transitions."""

    states = ["focused", "distracted", "procrastinating"]
    idx = 0
    while not stop_event.is_set():
        state = states[idx % len(states)]
        await server.send_state_update(
            state=state, focus_score=70 + (idx * 3) % 30,
            prev_state=states[(idx - 1) % len(states)],
            duration_in_state=interval,
            triggered_feedback="vibrate_short",
        )
        idx += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# --- interactive shell ------------------------------------------------------


async def interactive_shell(server: LinuxBLEServer) -> None:
    """Console-driven command dispatch while the server keeps running.

    Available commands mirror the Windows test script (which accepts
    ``eye`` / ``screen`` / ``sync`` / ``rest …``) and add a few
    Linux-only ones for issuing raw downlink messages::

        state <focused|distracted|procrastinating|resting>
        score <0..100>
        rest <start|stop|extend|query> [duration]
        sync                      (manual sync_request reply)
        display                   (push a sample display_content)
        vibrate                   (push a sample vibration_feedback)
        quit
    """

    log("INFO", "交互模式：输入 state、score、rest、sync、display、vibrate、quit")
    while True:
        command = (await asyncio.to_thread(input, "ble> ")).strip().lower()
        if command in {"quit", "exit", "q"}:
            return
        parts = command.split()
        head = parts[0] if parts else ""
        try:
            if head == "state" and len(parts) >= 2 and parts[1] in {
                "focused", "distracted", "procrastinating", "resting"
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
                await server._handle_rest_command(  # type: ignore[attr-defined]
                    {"action": action, **({"duration": duration} if duration else {})},
                )
            elif head == "sync":
                await server._handle_sync_request({})  # type: ignore[attr-defined]
            elif head == "display":
                await server.send_display_content(
                    line1="Interactive",
                    line2=time.strftime("%H:%M:%S"),
                    line3="score %d" % server.driver.focus_score,
                    line4=server.driver.current_state,
                )
            elif head == "vibrate":
                await server.send_vibration_feedback(
                    mode="vibrate_short", trigger="interactive", success=True
                )
            else:
                log_fail("未知命令。可用: state, score, rest, sync, display, vibrate, quit")
        except Exception as exc:
            log_fail("命令执行失败: %s" % exc)


# --- main test loop ---------------------------------------------------------


async def run_test(args: argparse.Namespace) -> int:
    config = BleServerConfig(
        device_name=args.device,
        focus_score_interval=args.focus_score_interval,
        rest_countdown_interval=args.rest_interval,
        device_status_interval=args.device_status_interval,
        background_loops=not args.no_background_loops,
        emit_ready_pattern=not args.no_ready_pattern,
    )
    server = LinuxBLEServer(config)

    summary = TestSummary()
    server.add_connection_handler(summary.on_state)
    server.add_message_handler(summary.on_message)
    server.add_error_handler(summary.on_error)

    # Hook ``gatt.notify`` so every downlink line is logged with the
    # *actual* seq number that hit the wire.  We can't wrap
    # ``send_message`` because the encode step inside it calls
    # ``next_seq()`` and we'd end up double-incrementing, which would
    # show wrong seq numbers in the log.
    original_notify = server.gatt.notify

    async def logging_notify(payload: bytes) -> bool:
        try:
            doc = json.loads(payload.decode("utf-8"))
            msg_type = doc.get("type", "?")
            seq = int(doc.get("seq", -1))
            data = doc.get("data", {})
        except Exception:
            msg_type, seq, data = "?", -1, {}
        result = await original_notify(payload)
        summary.on_sent(msg_type, payload, data)
        return result

    server.gatt.notify = logging_notify  # type: ignore[assignment]

    log("INFO", "启动 UNO Q BLE 服务器 (adapter=%s, name=%s)" % (
        config.adapter, config.device_name,
    ))
    log_event("正在注册 GATT application + 开始广播…")

    server_task = asyncio.create_task(server.run())
    stream_stop = asyncio.Event()
    stream_tasks: list[asyncio.Task] = []
    try:
        # Wait until BlueZ confirms the application is registered.
        deadline = time.monotonic() + 10.0
        while not summary.advertising and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not summary.advertising:
            log_fail("启动超时：BlueZ 没有确认 GATT application。")
            log_fail("检查 bluetoothd 状态、capabilities 和 adapter 路径。")
            return 1
        log_ok("GATT application 已注册；等待 Windows 客户端订阅 Notify…")

        if args.stream_focus:
            stream_tasks.append(asyncio.create_task(
                focus_score_stream(server, args.focus_hz, stream_stop)
            ))
        if args.stream_state:
            stream_tasks.append(asyncio.create_task(
                state_jitter(server, args.state_interval, stream_stop)
            ))

        # Show where the Windows log is expected to start producing RX
        # lines: as soon as Notify is up, our ready pattern fires and
        # the Windows client immediately issues a sync_request.
        deadline = time.monotonic() + max(5.0, args.connect_timeout)
        while not summary.notify_ready and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if summary.notify_ready:
            elapsed = time.monotonic() - summary.notify_subscribed_at  # type: ignore[operator]
            log_ok("Windows 客户端已订阅 Notify（耗时 %.2fs）" % 0.0)
        else:
            log_fail("未在 %.1fs 内等到 Windows 客户端订阅 Notify" % max(5.0, args.connect_timeout))
            log_fail("在 Windows 端确认：python windows\\\\windows_ble_test.py --device %s --scan-only" % args.device)

        if args.interactive:
            await interactive_shell(server)
        elif args.duration > 0:
            log("INFO", "运行 %ds 后自动结束；按 Ctrl+C 中断" % args.duration)
            await asyncio.sleep(args.duration)
        else:
            log("INFO", "持续运行中，按 Ctrl+C 结束测试。")
            await asyncio.Event().wait()
    except KeyboardInterrupt:
        log("INFO", "收到 Ctrl+C，正在停止…")
    finally:
        stream_stop.set()
        for task in stream_tasks:
            task.cancel()
        if stream_tasks:
            await asyncio.gather(*stream_tasks, return_exceptions=True)
        await server.stop()
        await server_task

    print_summary(summary)
    return _evaluate(summary)


def _evaluate(summary: TestSummary) -> int:
    if not summary.advertising:
        log_fail("RESULT: FAIL（未注册 GATT application）")
        return 1
    if not summary.notify_ready:
        log_fail("RESULT: FAIL（Windows 客户端从未订阅 Notify，无法确认双向链路）")
        return 1
    if summary.heartbeat_seen == 0:
        log_fail("RESULT: FAIL（Windows 端没有发送 heartbeat；可能是协议握手未完成或 MTU 未协商）")
        return 1
    if summary.heartbeat_echoes == 0:
        log_fail("RESULT: FAIL（Linux 端没有发出 heartbeat echo；检查 send_heartbeat(echo_seq=…)）")
        return 1
    if summary.errors:
        log_fail("RESULT: FAIL（测试期间出现 %d 个错误）" % summary.errors)
        return 1
    log_ok("RESULT: PASS（GATT 已注册、Windows 已订阅、heartbeat 双向、%d 条上行 / %d 条下行）" % (
        sum(summary.received.values()), sum(summary.sent.values()),
    ))
    return 0


def print_summary(summary: TestSummary) -> None:
    log("INFO", "========== BLE 测试汇总 ==========")
    log("INFO", "客户端订阅 Notify: %s" % summary.notify_ready)
    if summary.first_rx_at and summary.notify_subscribed_at:
        latency = summary.first_rx_at - summary.notify_subscribed_at
        log("INFO", "首条上行延迟: %.2fs (first RX=%s)" % (latency, summary.first_rx_type))
    log("INFO", "上行消息统计 (RX): %s" % (
        dict(summary.received) if summary.received else "无"
    ))
    log("INFO", "下行消息统计 (TX): %s" % (
        dict(summary.sent) if summary.sent else "无"
    ))
    log("INFO", "heartbeat: 收到 %d 条上行、回了 %d 条 echo" % (
        summary.heartbeat_seen, summary.heartbeat_echoes,
    ))
    log("INFO", "错误数量: %d" % summary.errors)
    if summary.last_error:
        log("INFO", "最后错误: %s" % summary.last_error)
    log("INFO", "==================================")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FocusFlow UNO Q Linux <-> Windows BLE 通信测试"
    )
    parser.add_argument("--device", default="UNO-Q-FF01",
                        help="本端广播的设备名，默认 UNO-Q-FF01")
    parser.add_argument("--adapter", default="/org/bluez/hci0",
                        help="BlueZ adapter 路径，默认 /org/bluez/hci0")
    parser.add_argument("--scan-only", action="store_true",
                        help="只验证 dbus-fast 与 BlueZ 是否可连通")
    parser.add_argument("--connect-timeout", type=float, default=10.0,
                        help="等待 Windows 客户端订阅 Notify 的秒数，默认 10")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="连接后运行秒数；0 表示持续运行")
    parser.add_argument("--rest-interval", type=float, default=10.0,
                        help="rest_countdown_loop 的间隔秒数")
    parser.add_argument("--focus-score-interval", type=float, default=1.0,
                        help="focus_score_loop 的间隔秒数")
    parser.add_argument("--device-status-interval", type=float, default=30.0,
                        help="device_status_loop 的间隔秒数")
    parser.add_argument("--no-background-loops", action="store_true",
                        help="关闭自动 focus_score / rest_countdown / device_status 循环")
    parser.add_argument("--no-ready-pattern", action="store_true",
                        help="关闭客户端订阅 Notify 时自动推送的 5 条 burst")
    parser.add_argument("--stream-focus", action="store_true",
                        help="以 --focus-hz 持续推送 focus_score 噪声")
    parser.add_argument("--focus-hz", type=float, default=1.0,
                        help="focus_score 推送频率")
    parser.add_argument("--stream-state", action="store_true",
                        help="以 --state-interval 持续切换 state_update")
    parser.add_argument("--state-interval", type=float, default=5.0,
                        help="state_update 切换间隔秒数")
    parser.add_argument("--interactive", action="store_true",
                        help="进入交互命令模式")
    parser.add_argument("--verbose", action="store_true",
                        help="显示 dbus-fast 调试日志")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.focus_hz <= 0:
        raise ValueError("--focus-hz 必须大于 0")
    if args.state_interval <= 0:
        raise ValueError("--state-interval 必须大于 0")
    if args.rest_interval <= 0:
        raise ValueError("--rest-interval 必须大于 0")
    if args.focus_score_interval <= 0:
        raise ValueError("--focus-score-interval 必须大于 0")
    if args.device_status_interval <= 0:
        raise ValueError("--device-status-interval 必须大于 0")
    if args.duration < 0:
        raise ValueError("--duration 不能小于 0")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    if args.scan_only:
        return asyncio.run(scan_only(args))
    try:
        return asyncio.run(run_test(args))
    except KeyboardInterrupt:
        log("INFO", "测试已取消。")
        return 130
    except Exception as exc:
        log_fail("测试异常: %s" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
