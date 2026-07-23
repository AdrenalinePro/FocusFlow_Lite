"""Serial-line FocusFlow client for Windows (replaces WindowsBLEClient).

Same ``send_*`` API as :class:`ble.windows_ble_client.WindowsBLEClient`
so ``uno_q_bridge.py`` and the test scripts can be ported by swapping
the import.  Transport is a newline-delimited JSON stream over a USB
CDC ACM serial port instead of BLE GATT.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Make ``Laptop/`` importable so ``ble.windows_ble_protocol`` resolves
# regardless of how this file is launched.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ble.windows_ble_protocol import (  # noqa: E402
    DOWNLINK_TYPES,
    MAX_JSON_BYTES,
    ProtocolError,
    decode_message,
    encode_message,
)
from serial_protocol import SerialTransport, encode_frame, auto_resolve_port  # noqa: E402

LOGGER = logging.getLogger(__name__)
Handler = Callable[..., Any]


class SerialFocusFlowClient:
    """Async serial client with the same public surface as ``WindowsBLEClient``.

    Usage::

        client = SerialFocusFlowClient(port="COM3")
        client.add_message_handler(lambda msg: print(msg.type, msg.data))
        await client.run_forever()
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self.logger = logger or LOGGER

        self._transport: Optional[SerialTransport] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._seq = 0
        self._read_task: Optional[asyncio.Task] = None
        self.connected = False

        self._handlers: Dict[str, List[Handler]] = {
            "message": [],
            "state": [],
            "error": [],
        }

    # ---- handler wiring -------------------------------------------------

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
                self.logger.exception("serial %s handler failed", kind)

    # ---- lifecycle ------------------------------------------------------

    async def run_forever(self) -> None:
        """Open the serial port, start the read loop, and block until stop()."""

        resolved = await auto_resolve_port(self._port)
        self._transport = SerialTransport(port=resolved, baudrate=self._baudrate)
        self.logger.info("串口已打开: %s @ %d baud", resolved, self._baudrate)

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.connected = True
        self._emit("state", "connected")

        self._read_task = asyncio.create_task(self._read_loop())
        try:
            await self._stop_event.wait()
        finally:
            self.connected = False
            if self._read_task is not None:
                self._read_task.cancel()
                try:
                    await self._read_task
                except asyncio.CancelledError:
                    pass
            if self._transport is not None:
                self._transport.close()
            self._emit("state", "stopped")

    async def stop(self) -> None:
        """Signal the read loop to exit."""
        if self._stop_event is not None:
            self._stop_event.set()

    # ---- read loop ------------------------------------------------------

    async def _read_loop(self) -> None:
        while self._transport is not None and not (self._stop_event and self._stop_event.is_set()):
            try:
                raw = await self._transport.read_message()
            except EOFError:
                self.connected = False
                self._emit("error", "串口已断开（USB 线拔出？）")
                self._emit("state", "error")
                if self._stop_event is not None:
                    self._stop_event.set()
                return
            if not raw:
                continue
            if len(raw) > MAX_JSON_BYTES:
                self.logger.debug("串口帧超过 240 字节上限 (%d)，已忽略", len(raw))
                continue
            try:
                message = decode_message(raw, DOWNLINK_TYPES)
            except ProtocolError as exc:
                self.logger.debug("非法 JSON 帧已忽略: %s", exc)
                continue
            self._emit("message", message)

    # ---- sequence helper ------------------------------------------------

    def next_seq(self) -> int:
        value = self._seq
        self._seq = (self._seq + 1) % 2**32
        return value

    # ---- send helpers ---------------------------------------------------

    async def send_message(self, msg_type: str, data: Dict[str, Any]) -> bool:
        """Encode, frame, and write one uplink message."""
        if self._transport is None or not self._transport.is_open:
            return False
        payload = encode_message(msg_type, data, self.next_seq())
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            if self._transport is None or not self._transport.is_open:
                return False
            try:
                await self._transport.write(encode_frame(payload))
                return True
            except Exception as exc:
                self._emit("error", "串口写入失败: %s" % exc)
                return False

    async def send_eye_data(
        self, yaw: float, pitch: float, is_focused: int,
        state_duration: float, confidence: float,
    ) -> bool:
        return await self.send_message("eye_data", {
            "yaw": round(yaw, 2), "pitch": round(pitch, 2),
            "is_focused": int(is_focused),
            "state_duration": round(state_duration, 2),
            "confidence": round(confidence, 2),
        })

    async def send_screen_data(
        self, state: str, confidence: float,
        app: Optional[str] = None, category: Optional[str] = None,
    ) -> bool:
        data: Dict[str, Any] = {"state": state, "confidence": round(confidence, 2)}
        if app is not None:
            data["app"] = app
        if category is not None:
            data["category"] = category
        return await self.send_message("screen_data", data)

    async def send_rest_command(
        self, action: str, duration: Optional[int] = None,
        reason: Optional[str] = "manual",
    ) -> bool:
        data: Dict[str, Any] = {"action": action}
        if duration is not None:
            data["duration"] = duration
        if reason is not None:
            data["reason"] = reason
        return await self.send_message("rest_command", data)

    async def send_heartbeat(self, uptime: Optional[int] = None) -> bool:
        import time
        return await self.send_message("heartbeat", {
            "uptime": uptime if uptime is not None else int(time.monotonic()),
        })

    async def send_sync_request(self, fields: Optional[List[str]] = None) -> bool:
        data: Dict[str, Any] = {}
        if fields is not None:
            data["fields"] = fields
        return await self.send_message("sync_request", data)
