"""High-level asyncio BLE server for the UNO Q Linux side.

This module is what a FocusFlow main program imports.  It wires together:

* the low-level :class:`linux.linux_ble_gatt.BlueZGattServer` (BlueZ GATT
  plumbing);
* the protocol helpers from :mod:`linux.linux_ble_protocol`
  (encode/decode/validate, see also ``windows/windows_ble_protocol.py``);
* a :class:`linux.linux_ble_state_machine.StateDriver` that fuses the
  latest eye / screen inputs and produces focus updates;
* the rest timer, device-status timer, heartbeat echo and sync_request
  replies required by the protocol.

The public surface is intentionally small and async-friendly so a Qt-free
main program (or a unit-test harness) can run the server from a normal
``asyncio.run(main())`` entry point:

* :meth:`LinuxBLEServer.run` blocks until :meth:`stop` is called;
* :meth:`add_message_handler` / :meth:`add_connection_handler` /
  :meth:`add_error_handler` register callbacks that fire on every
  validated incoming message and on every Notify subscription change;
* the ``send_*`` helpers push one downlink message to the Windows client.

The same instance is also used by ``linux_ble_test.py`` to validate the
BLE link end-to-end, so all of the protocol guarantees (compact JSON,
sequence de-duplication, heartbeat echo with ``echo_seq`` etc.) are
exercised by the unit tests.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union

from .linux_ble_gatt import BlueZGattServer
from .linux_ble_protocol import (
    BLEMessage,
    DOWNLINK_TYPES,
    MAX_JSON_BYTES,
    ProtocolError,
    STATES,
    UPLINK_TYPES,
    decode_message,
    encode_downlink,
    encode_message,
)
from .linux_ble_state_machine import StateDriver

LOGGER = logging.getLogger(__name__)


class BleServerState(str, Enum):
    """High-level server state machine."""

    STOPPED = "stopped"
    STARTING = "starting"
    ADVERTISING = "advertising"
    CONNECTED = "connected"
    NOTIFY_READY = "notify_ready"
    ERROR = "error"


@dataclass
class BleServerConfig:
    """Runtime settings.  The defaults match ``FocusFlow_BLE_Protocol.md``."""

    device_name: str = "UNO-Q-FF01"
    adapter: str = BlueZGattServer.ADAPTER_ROOT
    advertised_service_uuids: Optional[Sequence[str]] = None
    focus_score_interval: float = 1.0
    device_status_interval: float = 30.0
    auto_rest_countdown: bool = True
    rest_countdown_interval: float = 10.0
    # Optional initial state to publish on start.  ``None`` keeps the
    # state machine's own default (``focused``).
    initial_state: Optional[str] = None
    # Override the driver when the caller wants to plug in an ONNX model.
    driver: Any = None
    logger: Optional[logging.Logger] = None
    # When False the server does not automatically send ``focus_score``
    # / ``device_status`` / ``rest_countdown`` timers.  Tests and
    # interactive demos use this to take manual control.
    background_loops: bool = True
    # ``BleServerConfig.emit_ready_pattern`` is True by default; when the
    # Windows client subscribes to TX Notify, the server immediately
    # pushes a state_update + focus_score + device_status trio so the
    # Windows test script can correlate the "client ready" event with a
    # known round-trip sequence number.  Set to False for production
    # main programs that don't want the unsolicited burst.
    emit_ready_pattern: bool = True


Handler = Callable[..., Any]


class LinuxBLEServer:
    """An asyncio-friendly BLE GATT server for the FocusFlow protocol."""

    def __init__(
        self,
        config: Optional[BleServerConfig] = None,
        driver: Optional[Any] = None,
        gatt: Optional[BlueZGattServer] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config or BleServerConfig()
        self.logger = self.config.logger or logger or LOGGER
        if driver is not None:
            self.config.driver = driver
        self._driver: Any = self.config.driver or StateDriver()
        self._gatt: BlueZGattServer = gatt or BlueZGattServer(
            device_name=self.config.device_name,
            adapter=self.config.adapter,
            advertised_service_uuids=self.config.advertised_service_uuids,
            logger=self.logger,
        )

        self.state = BleServerState.STOPPED
        self.notifying = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._send_lock: Optional[asyncio.Lock] = None
        self._seq = 0
        self._incoming_sequences = _IncomingSequenceTracker()
        self._last_heartbeat = 0.0
        self._last_device_status_at = 0.0
        self._rest_started_at: Optional[float] = None
        self._rest_duration: int = 0
        self._focus_score_task: Optional[asyncio.Task] = None
        self._rest_timer_task: Optional[asyncio.Task] = None
        self._device_status_task: Optional[asyncio.Task] = None

        self._handlers: Dict[str, List[Handler]] = {
            "message": [],
            "connection": [],
            "error": [],
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
                    # Schedule but don't block on slow handlers.
                    asyncio.create_task(result)
            except Exception:
                self.logger.exception("BLE %s handler failed", kind)

    def _set_state(self, state: BleServerState) -> None:
        if self.state != state:
            self.state = state
            self.logger.debug("server state -> %s", state.value)
            self._emit("connection", state)

    # ---- driver access -------------------------------------------------
    @property
    def driver(self) -> Any:
        """Return the current state-machine driver (may be a custom one)."""

        return self._driver

    @property
    def gatt(self) -> BlueZGattServer:
        """Return the underlying BlueZ GATT wrapper (mainly for tests)."""

        return self._gatt

    # ---- lifecycle -----------------------------------------------------
    async def run(self) -> None:
        """Start the GATT server and block until :meth:`stop` is called.

        The RX / Notify handlers are installed **after** :meth:`start`
        succeeds because ``BlueZGattServer`` only creates the underlying
        D-Bus characteristic objects during start().  Calling
        ``set_rx_handler`` before start() used to raise
        ``RuntimeError("Server has not started yet")`` and silently
        aborted the server task.
        """

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._stop_requested = False
        self._stop_event.clear()
        self._set_state(BleServerState.STARTING)

        try:
            await self._gatt.start(self._loop)
        except Exception as exc:
            self._set_state(BleServerState.ERROR)
            # Surface the underlying D-Bus / RegisterApplication error
            # verbatim so operators can tell ``Permission denied`` from
            # ``No such adapter`` from a malformed GATT tree without
            # having to enable dbus-fast debug logging.
            self._emit("error", "启动 GATT 服务器失败: %s: %s" % (
                type(exc).__name__, exc,
            ))
            self.logger.error("GATT server start failed", exc_info=True)
            raise

        # Now that ``_gatt._rx`` / ``_gatt._tx`` exist, install the
        # dispatch callbacks.  Until we reach this point the low-level
        # GATT layer uses its default handlers (which just log) so a
        # premature write / StartNotify never crashes the server.
        self._gatt.set_rx_handler(self._handle_rx)
        self._gatt.set_notify_state_handler(self._handle_notify_state)

        self._set_state(BleServerState.ADVERTISING)
        self.logger.info(
            "FocusFlow BLE server is advertising as %r.  Pairing is not "
            "required - just connect from Windows.",
            self.config.device_name,
        )

        if self.config.background_loops:
            self._focus_score_task = asyncio.create_task(self._focus_score_loop())
            self._rest_timer_task = asyncio.create_task(self._rest_timer_loop())
            self._device_status_task = asyncio.create_task(self._device_status_loop())

        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Request a clean shutdown.  Safe to call from any task."""

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
        try:
            await self._gatt.stop()
        except Exception:
            self.logger.debug("GATT stop raised", exc_info=True)
        self._set_state(BleServerState.STOPPED)

    # ---- TX notify state ----------------------------------------------
    async def _handle_notify_state(self, enabled: bool) -> None:
        prev = self.notifying
        self.notifying = enabled
        self.logger.info("TX Notify subscription changed: %s -> %s", prev, enabled)
        if enabled:
            self._set_state(BleServerState.NOTIFY_READY)
            # The protocol expects Linux to reply with a heartbeat as soon
            # as Notify is up so the Windows side knows we are alive.
            await self.send_heartbeat(echo_seq=None)
            # Then emit a deterministic burst so the Windows test script
            # can correlate its first batch of ``RX`` lines against a
            # known sequence of ``TX`` lines on this side.
            await self._emit_ready_pattern()
        else:
            self._set_state(BleServerState.CONNECTED)

    # ---- RX message dispatch ------------------------------------------
    async def _handle_rx(self, payload: bytes) -> None:
        try:
            message = decode_message(payload, UPLINK_TYPES)  # uplink: Windows -> UNO Q
        except ProtocolError as exc:
            self._emit("error", "%s: %s" % (exc.code, exc))
            return
        if not self._incoming_sequences.accept(message.seq, message.ts):
            self.logger.debug("discard duplicate/out-of-order seq=%s", message.seq)
            return
        await self._dispatch(message)

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

    async def _handle_heartbeat(self, message: BLEMessage) -> None:
        uptime = int(time.time() - self._start_time())
        await self.send_heartbeat(echo_seq=message.seq, uptime=uptime)

    def _start_time(self) -> float:
        if not hasattr(self, "_started_at"):
            self._started_at = time.time()
        return self._started_at

    async def _handle_rest_command(self, data: Dict[str, Any]) -> None:
        action = data.get("action")
        if action == "start":
            duration = int(data.get("duration", 300))
            self._rest_started_at = time.time()
            self._rest_duration = duration
            self._set_in_rest(True)
            await self._trigger_feedback("vibrate_short")
            await self.send_state_update(
                state="resting", focus_score=0,
                prev_state=self._driver.current_state,
                duration_in_state=0,
                triggered_feedback="vibrate_short",
            )
        elif action == "stop":
            if not self._rest_started_at:
                # Protocol §11 TC-04: send STATE_CONFLICT when stop arrives
                # outside of resting.
                self._emit("error", "STATE_CONFLICT: rest_command(stop) received while not resting")
                await self.send_error("STATE_CONFLICT", "rest_command(stop) received while not resting")
                return
            duration_in_state = time.time() - (self._rest_started_at or time.time())
            self._rest_started_at = None
            self._set_in_rest(False)
            await self._trigger_feedback("vibrate_short")
            await self.send_state_update(
                state="focused", focus_score=self._driver.focus_score,
                prev_state="resting",
                duration_in_state=duration_in_state,
                triggered_feedback="vibrate_short",
            )
        elif action == "extend":
            if not self._rest_started_at:
                self._emit("error", "STATE_CONFLICT: rest_command(extend) received while not resting")
                await self.send_error("STATE_CONFLICT", "rest_command(extend) received while not resting")
                return
            extra = int(data.get("duration", 60))
            self._rest_duration += extra
        elif action == "query":
            if self._rest_started_at:
                remaining = max(0, int(self._rest_started_at + self._rest_duration - time.time()))
                await self.send_rest_countdown(
                    remaining=remaining,
                    total=self._rest_duration,
                )
            else:
                await self.send_rest_countdown(remaining=0, total=0)
        else:
            await self.send_error("INVALID_JSON", "unknown rest action %r" % action)

    def _set_in_rest(self, value: bool) -> None:
        # The driver never returns "resting" itself; the server flips the
        # state directly so the decision loop keeps returning "resting".
        if value:
            self._driver.current_state = "resting"
        elif self._driver.current_state == "resting":
            self._driver.current_state = "focused"

    async def _trigger_feedback(self, mode: str) -> None:
        await self.send_vibration_feedback(mode=mode, trigger="state_transition", success=True)

    async def _handle_sync_request(self, data: Dict[str, Any]) -> None:
        """Respond to ``sync_request`` with the current server snapshot.

        Windows 1.0.2+ accepts a compact ``device_status`` (only the
        fields the server actually knows about) and an optional
        ``rest_countdown`` of ``None``.  Together with a compact
        ``prev_state`` we keep the response well under 240 bytes.
        """

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
        """Return a ``device_status`` that omits fields still at their
        sentinel value (``false`` / ``-1``).  Windows accepts the
        compact form for ``sync_response`` (see
        ``windows.windows_ble_protocol._validate_device_status(required=False)``).
        """

        snapshot = self._snapshot_device_status()
        return {k: v for k, v in snapshot.items()
                if not (k.endswith("_battery") and v == -1)
                and not (k.endswith("_connected") and v is False)}

    async def _emit_ready_pattern(self) -> None:
        """Send a deterministic burst of downlink messages right after
        the Windows client subscribes to TX Notify.  The Windows test
        script logs each ``RX`` line; matching its ``seq`` numbers
        against this side's ``TX`` log gives an immediate answer to
        ``did Windows receive my burst?``.
        """

        if not self.config.emit_ready_pattern:
            return
        self.logger.info(
            "Client subscribed - emitting ready pattern (state_update, "
            "focus_score, device_status, display_content)"
        )
        await self.send_state_update(
            state=self._driver.current_state,
            focus_score=int(self._driver.focus_score),
            prev_state=self._driver.prev_state,
            duration_in_state=0.0,
            triggered_feedback="none",
        )
        await self.send_focus_score(
            int(self._driver.focus_score), self._driver.current_state,
        )
        await self.send_device_status(**self._snapshot_device_status())
        await self.send_display_content(
            line1="FocusFlow BLE",
            line2="Server ready",
            line3=self._driver.current_state,
            line4="score=%d" % int(self._driver.focus_score),
        )

    def _snapshot_device_status(self) -> Dict[str, Any]:
        # A real implementation would read EEG / wristband / TFT status
        # from the peripheral drivers.  The defaults below match the
        # example payload in §5.3.5 / §5.3.8 so the protocol fields are
        # always present.
        return {
            "eeg_connected": False,
            "eeg_battery": -1,
            "wristband_connected": False,
            "wristband_battery": -1,
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
                    await self._trigger_feedback("vibrate_continuous")
                    await self.send_state_update(
                        state="focused",
                        focus_score=self._driver.focus_score,
                        prev_state="resting",
                        duration_in_state=self._rest_duration,
                        triggered_feedback="vibrate_continuous",
                    )
                else:
                    total = self._rest_duration
                    if remaining < 30:
                        phase = "ending"
                    elif remaining > total * 0.8:
                        phase = "start"
                    else:
                        phase = "middle"
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
        """Validate, encode and notify one downlink message."""

        if msg_type not in DOWNLINK_TYPES:
            self._emit("error", "INVALID_MSG_TYPE: cannot send %r (not in downlink set)" % msg_type)
            return False
        try:
            # encode_downlink validates against DOWNLINK_TYPES; encode_message
            # from windows.windows_ble_protocol hardcodes UPLINK_TYPES so it
            # cannot be used for state_update, vibration_feedback, etc.
            payload = encode_downlink(msg_type, data, self.next_seq())
        except ProtocolError as exc:
            self._emit("error", "%s: %s" % (exc.code, exc))
            return False
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            sent = await self._gatt.notify(payload)
            if not sent:
                # No client is currently subscribed; this is normal at
                # boot.  Surface as a soft warning so logs make sense.
                self.logger.debug("TX Notify dropped (no subscriber): %s", msg_type)
            return sent

    async def send_state_update(
        self,
        state: str,
        focus_score: int,
        prev_state: str,
        duration_in_state: float,
        triggered_feedback: str,
    ) -> bool:
        return await self.send_message("state_update", {
            "state": state,
            "focus_score": int(focus_score),
            "prev_state": prev_state,
            "duration_in_state": round(float(duration_in_state), 2),
            "triggered_feedback": triggered_feedback,
        })

    async def send_focus_score(self, score: int, state: str) -> bool:
        return await self.send_message("focus_score", {
            "score": int(score),
            "state": state,
        })

    async def send_rest_countdown(
        self,
        remaining: int,
        total: int,
        phase: str = "middle",
    ) -> bool:
        if remaining <= 0:
            # When the rest is over we don't push a countdown frame.
            return False
        return await self.send_message("rest_countdown", {
            "remaining": int(remaining),
            "total": max(1, int(total)),
            "state": "resting",
            "phase": phase,
        })

    async def send_display_content(
        self,
        line1: Optional[str] = None,
        line2: Optional[str] = None,
        line3: Optional[str] = None,
        line4: Optional[str] = None,
    ) -> bool:
        data: Dict[str, Any] = {}
        for key, value in (("line1", line1), ("line2", line2),
                           ("line3", line3), ("line4", line4)):
            if value is not None:
                data[key] = value
        return await self.send_message("display_content", data)

    async def send_device_status(
        self,
        eeg_connected: bool,
        eeg_battery: int,
        wristband_connected: bool,
        wristband_battery: int,
        tft_display: str,
    ) -> bool:
        return await self.send_message("device_status", {
            "eeg_connected": bool(eeg_connected),
            "eeg_battery": int(eeg_battery),
            "wristband_connected": bool(wristband_connected),
            "wristband_battery": int(wristband_battery),
            "tft_display": tft_display,
        })

    async def send_vibration_feedback(self, mode: str, trigger: str, success: bool) -> bool:
        return await self.send_message("vibration_feedback", {
            "mode": mode,
            "trigger": trigger,
            "success": bool(success),
        })

    async def send_heartbeat(
        self,
        echo_seq: Optional[int] = None,
        uptime: Optional[int] = None,
    ) -> bool:
        data: Dict[str, Any] = {}
        if echo_seq is not None:
            data["echo_seq"] = int(echo_seq)
        if uptime is None:
            uptime = int(time.time() - self._start_time())
        data["uptime"] = int(uptime)
        return await self.send_message("heartbeat", data)

    async def send_sync_response(
        self,
        state: str,
        focus_score: int,
        prev_state: str,
        rest_countdown: Optional[Dict[str, Any]],
        device_status: Dict[str, Any],
    ) -> bool:
        return await self.send_message("sync_response", {
            "state": state,
            "focus_score": int(focus_score),
            "prev_state": prev_state,
            "rest_countdown": rest_countdown,
            "device_status": device_status,
        })

    async def send_error(self, code: str, message: str, fatal: bool = False) -> bool:
        if code not in {"INVALID_JSON", "INVALID_MSG_TYPE", "MISSING_FIELD",
                        "OUT_OF_RANGE", "STATE_CONFLICT", "DEVICE_BUSY",
                        "INTERNAL_ERROR"}:
            self.logger.warning("unknown error code %r; sending anyway", code)
        return await self.send_message("error", {
            "code": code,
            "message": message,
            "fatal": bool(fatal),
        })


class _IncomingSequenceTracker:
    """Drop duplicate / out-of-order uint32 sequence numbers with wraparound.

    Mirrors the same helper used by the Windows client (see
    ``windows/windows_ble_client.SequenceTracker``) so the protocol behaves
    identically in both directions.
    """

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
