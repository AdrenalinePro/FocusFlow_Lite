"""High-level serial service for the UNO Q Linux side.

Self-contained; depends only on the stdlib (``os``  ``termios``  ``asyncio``)
and the two existing protocol/state-machine modules.

Replaces :class:`linux_ble_server.LinuxBLEServer` — instead of registering a
BlueZ GATT application this service opens the USB-serial device and reads /
writes newline-delimited JSON frames.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import termios
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# -- import the two existing modules (survives both ``python3 -m`` and direct) --
_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

if __package__ in {None, ""}:
    from linux_ble_protocol import (  # type: ignore
        BLEMessage,
        DOWNLINK_TYPES,
        MAX_JSON_BYTES,
        ProtocolError,
        UPLINK_TYPES,
        decode_message,
        encode_downlink,
    )
    from linux_ble_state_machine import StateDriver  # type: ignore
else:
    from .linux_ble_protocol import (
        BLEMessage,
        DOWNLINK_TYPES,
        MAX_JSON_BYTES,
        ProtocolError,
        UPLINK_TYPES,
        decode_message,
        encode_downlink,
    )
    from .linux_ble_state_machine import StateDriver

LOGGER = logging.getLogger(__name__)

_BAUDRATE = 115200
_FRAME_DELIMITER = b"\n"


def _encode_frame(payload: bytes) -> bytes:
    """Wrap one encoded JSON payload for the wire."""
    return payload + _FRAME_DELIMITER


# ======================================================================
# lightweight async-safe serial transport (no pyserial needed on Linux)
# ======================================================================

class _LinuxSerialTransport:
    """Raw file-descriptor serial transport for Linux.

    Uses ``open()`` / ``termios`` / ``os.read`` / ``os.write`` behind
    ``asyncio.to_thread``, so it never blocks the event loop.
    """

    def __init__(self, port: str, baudrate: int = _BAUDRATE) -> None:
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        # Configure the tty: raw mode, no echo, canonical off.
        attrs = termios.tcgetattr(self._fd)
        # input flags
        attrs[0] &= ~(
            termios.IGNBRK | termios.BRKINT | termios.PARMRK
            | termios.ISTRIP | termios.INLCR | termios.IGNCR
            | termios.ICRNL | termios.IXON
        )
        # output flags
        attrs[1] &= ~termios.OPOST
        # control flags
        attrs[2] &= ~(termios.CSIZE | termios.PARENB)
        attrs[2] |= termios.CS8
        # local flags
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE
                      | termios.ECHONL | termios.ISIG | termios.IEXTEN)
        # set baud rate
        for attr_idx in (4, 5):  # ispeed, ospeed
            attrs[attr_idx] = getattr(termios, "B%d" % baudrate, termios.B115200)
        # min chars = 0, timeout = 1 decisecond
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        self._buffer = bytearray()
        self._closed = False

    async def write(self, data: bytes) -> None:
        if self._closed:
            return
        await asyncio.to_thread(os.write, self._fd, data)

    async def read_message(self) -> bytes:
        """Return the next ``\\n``-delimited line (without the trailing ``\\n``).

        Raises ``EOFError`` when the fd is closed or the cable pulls out.
        """
        while True:
            if self._closed:
                raise EOFError("serial port closed")
            idx = self._buffer.find(b"\n")
            if idx >= 0:
                line = bytes(self._buffer[:idx])
                del self._buffer[: idx + 1]
                return line
            try:
                chunk = await asyncio.to_thread(os.read, self._fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                raise EOFError("serial port disappeared (cable unplugged?)")
            self._buffer.extend(chunk)

    def close(self) -> None:
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass

    @property
    def is_open(self) -> bool:
        return not self._closed


# ======================================================================
# server config + state
# ======================================================================

class SerialServerState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class SerialServerConfig:
    device_name: str = "UNO-Q-FF01"
    serial_port: str = "/dev/ttyGS0"
    baudrate: int = _BAUDRATE
    focus_score_interval: float = 1.0
    device_status_interval: float = 30.0
    auto_rest_countdown: bool = True
    rest_countdown_interval: float = 10.0
    initial_state: Optional[str] = None
    driver: Any = None
    logger: Optional[logging.Logger] = None
    background_loops: bool = True


Handler = Callable[..., Any]


class LinuxSerialService:
    """Async serial service for the FocusFlow protocol on the UNO Q."""

    def __init__(
        self,
        config: Optional[SerialServerConfig] = None,
        driver: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config or SerialServerConfig()
        self.logger = self.config.logger or logger or LOGGER
        if driver is not None:
            self.config.driver = driver
        self._driver: Any = self.config.driver or StateDriver()
        if self.config.initial_state:
            self._driver.current_state = self.config.initial_state

        self.state = SerialServerState.STOPPED
        self._transport: Optional[_LinuxSerialTransport] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._send_lock: Optional[asyncio.Lock] = None
        self._seq = 0
        self._incoming_sequences = _IncomingSequenceTracker()
        self._rest_started_at: Optional[float] = None
        self._rest_duration: int = 0
        self._focus_score_task: Optional[asyncio.Task] = None
        self._rest_timer_task: Optional[asyncio.Task] = None
        self._device_status_task: Optional[asyncio.Task] = None

        self._handlers: Dict[str, List[Handler]] = {
            "message": [], "connection": [], "error": [],
        }

    # ---- handler wiring ------------------------------------------------

    def add_message_handler(self, handler: Handler) -> None:
        self._handlers["message"].append(handler)

    def add_connection_handler(self, handler: Handler) -> None:
        self._handlers["connection"].append(handler)

    def add_error_handler(self, handler: Handler) -> None:
        self._handlers["error"].append(handler)

    def _emit(self, kind: str, *args: Any) -> None:
        for handler in tuple(self._handlers.get(kind, ())):
            try:
                result = handler(*args)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception:
                self.logger.exception("serial %s handler failed", kind)

    def _set_state(self, state: SerialServerState) -> None:
        if self.state != state:
            self.state = state
            self.logger.debug("server state -> %s", state.value)
            self._emit("connection", state)

    @property
    def driver(self) -> Any:
        return self._driver

    # ---- lifecycle -----------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._stop_requested = False
        self._stop_event.clear()

        self._transport = _LinuxSerialTransport(
            port=self.config.serial_port,
            baudrate=self.config.baudrate,
        )
        self.logger.info(
            "串口已打开: %s @ %d baud",
            self.config.serial_port, self.config.baudrate,
        )
        self._set_state(SerialServerState.RUNNING)
        self._emit("connection", SerialServerState.RUNNING)

        if self.config.background_loops:
            self._focus_score_task = asyncio.create_task(self._focus_score_loop())
            self._rest_timer_task = asyncio.create_task(self._rest_timer_loop())
            self._device_status_task = asyncio.create_task(self._device_status_loop())

        try:
            await self._read_loop()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        self._stop_requested = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def _shutdown(self) -> None:
        for task_attr in ("_focus_score_task", "_rest_timer_task", "_device_status_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, task_attr, None)
        if self._transport is not None:
            self._transport.close()
        self._set_state(SerialServerState.STOPPED)

    # ---- read loop ------------------------------------------------------

    async def _read_loop(self) -> None:
        while self._transport is not None and not self._stop_requested:
            try:
                raw = await self._transport.read_message()
            except EOFError:
                self.logger.warning("串口已断开，停止服务")
                self._set_state(SerialServerState.ERROR)
                await self.stop()
                return
            if not raw:
                continue
            if len(raw) > MAX_JSON_BYTES:
                self._emit("error", "串口帧超过 240 字节上限 (%d)" % len(raw))
                continue
            try:
                message = decode_message(raw, UPLINK_TYPES)
            except ProtocolError as exc:
                self._emit("error", "%s: %s" % (exc.code, exc))
                continue
            if not self._incoming_sequences.accept(message.seq, message.ts):
                self.logger.debug("discard duplicate/out-of-order seq=%s", message.seq)
                continue
            await self._dispatch(message)

    # ---- dispatch -------------------------------------------------------

    async def _dispatch(self, message: BLEMessage) -> None:
        self.logger.debug("RX %-20s seq=%s data=%s", message.type, message.seq, message.data)
        if message.type == "eye_data":
            self._driver.update_inputs(eye=message.data)
        elif message.type == "screen_data":
            self._driver.update_inputs(screen=message.data)
        elif message.type == "rest_command":
            await self._handle_rest_command(message.data)
        elif message.type == "heartbeat":
            await self._handle_heartbeat(message)
            self._emit("message", message)
            return
        elif message.type == "sync_request":
            await self._handle_sync_request(message.data)
        else:
            self._emit("error", "INVALID_MSG_TYPE: unknown uplink type %r" % message.type)
            return
        self._emit("message", message)

    # ---- rest / sync / heartbeat handlers (same as BLE server) ----------

    async def _handle_heartbeat(self, message: BLEMessage) -> None:
        uptime = int(time.time() - self._start_time())
        await self.send_heartbeat(echo_seq=message.seq, uptime=uptime)

    def _start_time(self) -> float:
        if not hasattr(self, "_started_at"):
            self._started_at = time.time()  # type: ignore[attr-defined]
        return self._started_at  # type: ignore[attr-defined]

    async def _handle_rest_command(self, data: Dict[str, Any]) -> None:
        action = data.get("action")
        if action == "start":
            duration = int(data.get("duration", 300))
            self._rest_started_at = time.time()
            self._rest_duration = duration
            self._set_in_rest(True)
            await self.send_state_update(
                state="resting", focus_score=0,
                prev_state=self._driver.current_state,
                duration_in_state=0,
                triggered_feedback="vibrate_short",
            )
        elif action == "stop":
            if not self._rest_started_at:
                self._emit("error", "STATE_CONFLICT: rest_command(stop) while not resting")
                await self.send_error("STATE_CONFLICT", "rest_command(stop) while not resting")
                return
            elapsed = time.time() - (self._rest_started_at or time.time())
            self._rest_started_at = None
            self._set_in_rest(False)
            await self.send_state_update(
                state="focused", focus_score=self._driver.focus_score,
                prev_state="resting", duration_in_state=elapsed,
                triggered_feedback="vibrate_short",
            )
        elif action == "extend":
            if not self._rest_started_at:
                self._emit("error", "STATE_CONFLICT: rest_command(extend) while not resting")
                await self.send_error("STATE_CONFLICT", "rest_command(extend) while not resting")
                return
            self._rest_duration += int(data.get("duration", 60))
        elif action == "query":
            if self._rest_started_at:
                remaining = max(0, int(self._rest_started_at + self._rest_duration - time.time()))
                await self.send_rest_countdown(remaining=remaining, total=self._rest_duration)
            else:
                await self.send_rest_countdown(remaining=0, total=0)
        else:
            await self.send_error("INVALID_JSON", "unknown rest action %r" % action)

    def _set_in_rest(self, value: bool) -> None:
        if value:
            self._driver.current_state = "resting"
        elif self._driver.current_state == "resting":
            self._driver.current_state = "focused"

    async def _handle_sync_request(self, data: Dict[str, Any]) -> None:
        device_status = self._compact_device_status()
        rest_countdown: Optional[Dict[str, Any]] = None
        if self._rest_started_at:
            remaining = max(0, int(self._rest_started_at + self._rest_duration - time.time()))
            phase = (
                "ending" if remaining < 30 else
                ("start" if remaining > self._rest_duration * 0.8 else "middle")
            )
            rest_countdown = {
                "remaining": remaining, "total": self._rest_duration,
                "state": "resting", "phase": phase,
            }
        await self.send_sync_response(
            state=self._driver.current_state,
            focus_score=int(self._driver.focus_score),
            prev_state=self._driver.prev_state,
            rest_countdown=rest_countdown,
            device_status=device_status,
        )

    def _compact_device_status(self) -> Dict[str, Any]:
        s = self._snapshot_device_status()
        return {k: v for k, v in s.items()
                if not (k.endswith("_battery") and v == -1)
                and not (k.endswith("_connected") and v is False)}

    def _snapshot_device_status(self) -> Dict[str, Any]:
        return {
            "eeg_connected": False, "eeg_battery": -1,
            "wristband_connected": False, "wristband_battery": -1,
            "tft_display": "running",
        }

    # ---- background timers --------------------------------------------

    async def _focus_score_loop(self) -> None:
        interval = max(0.1, self.config.focus_score_interval)
        while not self._stop_requested:
            try:
                await self.send_focus_score(self._driver.focus_score, self._driver.current_state)
            except Exception:
                self.logger.exception("focus_score tick failed")
            await asyncio.sleep(interval)

    async def _rest_timer_loop(self) -> None:
        if not self.config.auto_rest_countdown:
            return
        interval = max(0.1, self.config.rest_countdown_interval)
        while not self._stop_requested:
            if self._rest_started_at:
                now = time.time()
                remaining = max(0, int(self._rest_started_at + self._rest_duration - now))
                if remaining == 0:
                    self._rest_started_at = None
                    self._set_in_rest(False)
                else:
                    total = self._rest_duration
                    phase = "ending" if remaining < 30 else (
                        "start" if remaining > total * 0.8 else "middle"
                    )
                    await self.send_rest_countdown(remaining=remaining, total=total, phase=phase)
            await asyncio.sleep(interval)

    async def _device_status_loop(self) -> None:
        interval = max(1.0, self.config.device_status_interval)
        while not self._stop_requested:
            try:
                await self.send_device_status(**self._snapshot_device_status())
            except Exception:
                self.logger.exception("device_status tick failed")
            await asyncio.sleep(interval)

    # ---- sequence helper ----------------------------------------------

    def next_seq(self) -> int:
        value = self._seq
        self._seq = (self._seq + 1) % 2**32
        return value

    # ---- send helpers -------------------------------------------------

    async def send_message(self, msg_type: str, data: Dict[str, Any]) -> bool:
        if msg_type not in DOWNLINK_TYPES:
            self._emit("error", "INVALID_MSG_TYPE: cannot send %r" % msg_type)
            return False
        try:
            payload = encode_downlink(msg_type, data, self.next_seq())
        except ProtocolError as exc:
            self._emit("error", "%s: %s" % (exc.code, exc))
            return False
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            if self._transport is None or not self._transport.is_open:
                return False
            try:
                await self._transport.write(_encode_frame(payload))
                return True
            except Exception as exc:
                self.logger.debug("serial write failed: %s", exc)
                return False

    async def send_state_update(self, state: str, focus_score: int,
                                prev_state: str, duration_in_state: float,
                                triggered_feedback: str) -> bool:
        return await self.send_message("state_update", {
            "state": state, "focus_score": int(focus_score),
            "prev_state": prev_state,
            "duration_in_state": round(float(duration_in_state), 2),
            "triggered_feedback": triggered_feedback,
        })

    async def send_focus_score(self, score: int, state: str) -> bool:
        return await self.send_message("focus_score", {
            "score": int(score), "state": state,
        })

    async def send_rest_countdown(self, remaining: int, total: int,
                                  phase: str = "middle") -> bool:
        if remaining <= 0:
            return False
        return await self.send_message("rest_countdown", {
            "remaining": int(remaining), "total": max(1, int(total)),
            "state": "resting", "phase": phase,
        })

    async def send_display_content(self, line1: Optional[str] = None,
                                   line2: Optional[str] = None,
                                   line3: Optional[str] = None,
                                   line4: Optional[str] = None) -> bool:
        data: Dict[str, Any] = {}
        for k, v in (("line1", line1), ("line2", line2),
                     ("line3", line3), ("line4", line4)):
            if v is not None:
                data[k] = v
        return await self.send_message("display_content", data)

    async def send_device_status(self, eeg_connected: bool, eeg_battery: int,
                                 wristband_connected: bool,
                                 wristband_battery: int,
                                 tft_display: str) -> bool:
        return await self.send_message("device_status", {
            "eeg_connected": bool(eeg_connected), "eeg_battery": int(eeg_battery),
            "wristband_connected": bool(wristband_connected),
            "wristband_battery": int(wristband_battery),
            "tft_display": tft_display,
        })

    async def send_vibration_feedback(self, mode: str, trigger: str,
                                      success: bool) -> bool:
        return await self.send_message("vibration_feedback", {
            "mode": mode, "trigger": trigger, "success": bool(success),
        })

    async def send_heartbeat(self, echo_seq: Optional[int] = None,
                             uptime: Optional[int] = None) -> bool:
        data: Dict[str, Any] = {}
        if echo_seq is not None:
            data["echo_seq"] = int(echo_seq)
        if uptime is None:
            uptime = int(time.time() - self._start_time())
        data["uptime"] = int(uptime)
        return await self.send_message("heartbeat", data)

    async def send_sync_response(self, state: str, focus_score: int,
                                 prev_state: str,
                                 rest_countdown: Optional[Dict[str, Any]],
                                 device_status: Dict[str, Any]) -> bool:
        return await self.send_message("sync_response", {
            "state": state, "focus_score": int(focus_score),
            "prev_state": prev_state,
            "rest_countdown": rest_countdown,
            "device_status": device_status,
        })

    async def send_error(self, code: str, message: str,
                         fatal: bool = False) -> bool:
        return await self.send_message("error", {
            "code": code, "message": message, "fatal": bool(fatal),
        })


class _IncomingSequenceTracker:
    """Drop duplicate / out-of-order uint32 sequence numbers with wraparound."""

    HALF_RANGE = 2**31

    def __init__(self) -> None:
        self.last_seq: Optional[int] = None
        self.last_ts: Optional[int] = None

    def accept(self, seq: int, ts: int) -> bool:
        if self.last_seq is None:
            self.last_seq, self.last_ts = seq, ts
            return True
        distance = (seq - self.last_seq) % 2**32
        if 0 < distance < self.HALF_RANGE:
            self.last_seq, self.last_ts = seq, ts
            return True
        if (distance != 0 and self.last_ts is not None
                and ts > self.last_ts and distance > self.HALF_RANGE):
            self.last_seq, self.last_ts = seq, ts
            return True
        return False
