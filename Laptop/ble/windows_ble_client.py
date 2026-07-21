"""Asyncio Windows BLE client for the FocusFlow UNO Q protocol."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

from .windows_ble_protocol import (
    BLEMessage,
    DOWNLINK_TYPES,
    RX_CHARACTERISTIC_UUID,
    TX_CHARACTERISTIC_UUID,
    ProtocolError,
    encode_message,
    decode_message,
)

LOGGER = logging.getLogger(__name__)
Handler = Callable[..., Any]
WINDOWS_ADDRESS_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


class BleConnectionState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class NotConnectedError(RuntimeError):
    """Raised when an application tries to send without an active link."""


@dataclass
class BleClientConfig:
    """Runtime settings. ``device`` may be a Windows BLE address or name."""

    device: str = "UNO-Q-FF01"
    connect_timeout: float = 10.0
    scan_timeout: float = 10.0
    reconnect_delay: float = 3.0
    max_reconnect_attempts: Optional[int] = 5
    heartbeat_interval: float = 10.0
    heartbeat_timeout: float = 30.0
    write_with_response: bool = False


class SequenceTracker:
    """Drop duplicate/out-of-order uint32 sequence numbers, including wraparound."""

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
        # A sender may restart its sequence close to a timestamp boundary.
        # Only accept that fallback when the timestamp clearly moves forward;
        # it prevents a delayed old notification from being delivered twice.
        if distance != 0 and self.last_ts is not None and ts > self.last_ts and distance > self.HALF_RANGE:
            self.last_seq, self.last_ts = seq, ts
            return True
        return False


class WindowsBLEClient:
    """A reconnecting, validated, asyncio-based BLE GATT client.

    Call :meth:`run_forever` from an asyncio event loop.  GUI applications
    should normally use :class:`ble.windows_ble_qt.WindowsBLEClientThread`.
    """

    def __init__(self, config: Optional[BleClientConfig] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.config = config or BleClientConfig()
        self.logger = logger or LOGGER
        self.client: Any = None
        self.connected = False
        self.state = BleConnectionState.STOPPED
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._stop_requested = False
        self._disconnect_event: Optional[asyncio.Event] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        # Create the lock in the event loop that owns the BLE client.  This
        # keeps the object safe to construct before asyncio.run() (and is
        # important on older Python versions where locks bind eagerly).
        self._send_lock: Optional[asyncio.Lock] = None
        self._seq = 0
        self._incoming_sequences = SequenceTracker()
        self._last_heartbeat = 0.0
        self._handlers: Dict[str, List[Handler]] = {
            "message": [], "state": [], "error": []
        }

    def add_message_handler(self, handler: Handler) -> None:
        self._handlers["message"].append(handler)

    def add_state_handler(self, handler: Handler) -> None:
        self._handlers["state"].append(handler)

    def add_error_handler(self, handler: Handler) -> None:
        self._handlers["error"].append(handler)

    def _emit(self, kind: str, *args: Any) -> None:
        for handler in tuple(self._handlers.get(kind, ())):
            try:
                result = handler(*args)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception:
                self.logger.exception("BLE %s handler failed", kind)

    def _set_state(self, state: BleConnectionState) -> None:
        if self.state != state:
            self.state = state
            self._emit("state", state)

    def next_seq(self) -> int:
        value = self._seq
        self._seq = (self._seq + 1) % 2**32
        return value

    async def stop(self) -> None:
        self._stop_requested = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._disconnect_event is not None:
            self._disconnect_event.set()
        if self.client is not None:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception:
                self.logger.debug("BLE disconnect failed", exc_info=True)
        self.connected = False

    async def run_forever(self) -> None:
        """Connect, subscribe, reconnect after drops, and return on stop."""

        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            self._set_state(BleConnectionState.ERROR)
            self._emit("error", "未安装 bleak，请执行: pip install bleak")
            raise RuntimeError("bleak is required for the Windows BLE client") from exc

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        if self._stop_requested:
            self._stop_event.set()
        attempt = 0
        self._set_state(BleConnectionState.DISCONNECTED)

        while not self._stop_event.is_set():
            attempt += 1
            self._set_state(BleConnectionState.CONNECTING if attempt == 1 else BleConnectionState.RECONNECTING)
            try:
                target = await self._resolve_device(BleakScanner)
                self.client = BleakClient(target, timeout=self.config.connect_timeout,
                                          disconnected_callback=self._on_disconnected)
                await self.client.connect()
                if not self.client.is_connected:
                    raise ConnectionError("Bleak connected call returned a disconnected client")
                self.connected = True
                self._disconnect_event = asyncio.Event()
                self._last_heartbeat = time.monotonic()
                self._set_state(BleConnectionState.CONNECTED)
                await self.client.start_notify(TX_CHARACTERISTIC_UUID, self._notification_callback)
                await self.send_sync_request(["all"])
                # The retry limit applies to one outage, not to the whole
                # lifetime of a long-running FocusFlow process.
                attempt = 0
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                await self._disconnect_event.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self._set_state(BleConnectionState.ERROR)
                self._emit("error", str(exc))
                self.logger.warning("BLE connection attempt failed: %s", exc)
            finally:
                await self._cleanup_connection()

            if self._stop_event.is_set():
                break
            if self.config.max_reconnect_attempts is not None and attempt >= self.config.max_reconnect_attempts:
                self._emit("error", "BLE 重连次数已达到上限")
                break
            self._set_state(BleConnectionState.DISCONNECTED)
            await asyncio.sleep(self.config.reconnect_delay)

        self._set_state(BleConnectionState.STOPPED)

    async def _resolve_device(self, scanner: Any) -> Any:
        device = self.config.device.strip()
        if WINDOWS_ADDRESS_RE.match(device):
            return device
        found = await scanner.find_device_by_name(device, timeout=self.config.scan_timeout)
        if found is None:
            raise ConnectionError("未找到 BLE 设备: %s" % device)
        return found

    def _on_disconnected(self, _client: Any = None) -> None:
        self.connected = False
        if self._loop and self._disconnect_event:
            self._loop.call_soon_threadsafe(self._disconnect_event.set)

    def _notification_callback(self, _sender: Any, data: bytearray) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._handle_notification, bytes(data))

    def _handle_notification(self, payload: bytes) -> None:
        try:
            message = decode_message(payload, DOWNLINK_TYPES)
        except ProtocolError as exc:
            self._emit("error", "%s: %s" % (exc.code, exc))
            return
        if not self._incoming_sequences.accept(message.seq, message.ts):
            self.logger.debug("discard duplicate/out-of-order BLE message seq=%s", message.seq)
            return
        if message.type == "heartbeat":
            self._last_heartbeat = time.monotonic()
        self._emit("message", message)
        if message.type == "error":
            data = message.data
            self._emit("error", data.get("message", "UNO Q returned an error"))
            if data.get("fatal"):
                self._on_disconnected(self.client)

    async def _cleanup_connection(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        self.connected = False
        if self.client is not None:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception:
                self.logger.debug("BLE cleanup disconnect failed", exc_info=True)
        self.client = None

    async def _heartbeat_loop(self) -> None:
        while self.connected and self._stop_event and not self._stop_event.is_set():
            await self.send_heartbeat()
            await asyncio.sleep(self.config.heartbeat_interval)
            if time.monotonic() - self._last_heartbeat > self.config.heartbeat_timeout:
                self._emit("error", "BLE 心跳超时，正在重连")
                self._on_disconnected(self.client)
                return

    async def send_message(self, msg_type: str, data: Dict[str, Any]) -> bool:
        """Send one validated message. Returns False when not connected."""

        if not self.connected or self.client is None or not self.client.is_connected:
            return False
        payload = encode_message(msg_type, data, self.next_seq())
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            if not self.connected or self.client is None:
                return False
            try:
                await self.client.write_gatt_char(
                    RX_CHARACTERISTIC_UUID, payload,
                    response=self.config.write_with_response,
                )
                return True
            except Exception as exc:
                self._emit("error", "BLE 发送失败: %s" % exc)
                self._on_disconnected(self.client)
                return False

    async def send_eye_data(self, yaw: float, pitch: float, is_focused: int,
                            state_duration: float, confidence: float) -> bool:
        return await self.send_message("eye_data", {
            "yaw": round(yaw, 2), "pitch": round(pitch, 2),
            "is_focused": int(is_focused), "state_duration": round(state_duration, 2),
            "confidence": round(confidence, 2),
        })

    async def send_screen_data(self, state: str, confidence: float,
                               app: Optional[str] = None,
                               category: Optional[str] = None) -> bool:
        data: Dict[str, Any] = {"state": state, "confidence": round(confidence, 2)}
        if app is not None:
            data["app"] = app
        if category is not None:
            data["category"] = category
        return await self.send_message("screen_data", data)

    async def send_rest_command(self, action: str, duration: Optional[int] = None,
                                reason: Optional[str] = "manual") -> bool:
        data: Dict[str, Any] = {"action": action}
        if duration is not None:
            data["duration"] = duration
        if reason is not None:
            data["reason"] = reason
        return await self.send_message("rest_command", data)

    async def send_heartbeat(self, uptime: Optional[int] = None) -> bool:
        if uptime is None:
            uptime = int(time.monotonic())
        return await self.send_message("heartbeat", {"uptime": uptime})

    async def send_sync_request(self, fields: Optional[List[str]] = None) -> bool:
        data: Dict[str, Any] = {}
        if fields is not None:
            data["fields"] = fields
        return await self.send_message("sync_request", data)
