#!/usr/bin/env python3
"""Bridge the integrated laptop decision stream to the UNO Q via USB serial."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable, Optional


LOGGER = logging.getLogger("uno_q_bridge")
Publisher = Callable[[dict], Awaitable[None]]


def install_decision_protocol(protocol: Any) -> None:
    """Extend the teammate protocol module without modifying their checkout."""
    protocol.UPLINK_TYPES.add("decision_update")
    if getattr(protocol, "_focusflow_decision_update_installed", False):
        return
    original_validate_data = protocol.validate_data

    def validate_data(msg_type: str, data: Any) -> None:
        if msg_type != "decision_update":
            original_validate_data(msg_type, data)
            return
        if not isinstance(data, dict):
            raise protocol.ProtocolError("data must be an object", "INVALID_FIELD")
        allowed_states = {"focused", "distracted", "procrastinating", "waiting", "resting"}
        if data.get("state") not in allowed_states:
            raise protocol.ProtocolError("invalid decision state", "INVALID_FIELD")
        score = data.get("score")
        if score is not None and (
            not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100
        ):
            raise protocol.ProtocolError("score must be null or 0..100", "OUT_OF_RANGE")
        if not isinstance(data.get("signal_ok"), bool):
            raise protocol.ProtocolError("signal_ok must be boolean", "INVALID_FIELD")
        duration = data.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            raise protocol.ProtocolError("duration must be non-negative", "OUT_OF_RANGE")
        app = data.get("app")
        if app is not None and (not isinstance(app, str) or len(app) > 24):
            raise protocol.ProtocolError("app must be at most 24 characters", "OUT_OF_RANGE")

    protocol.validate_data = validate_data
    protocol._focusflow_decision_update_installed = True


def decision_payload(decision: Optional[dict], screen: Optional[dict], *, resting: bool) -> dict:
    """Convert the laptop decision vocabulary into the UNO Q wire vocabulary."""
    decision = decision or {}
    screen = screen or {}
    state = str(decision.get("state", "waiting"))
    if resting:
        wire_state = "resting"
    elif state == "focused":
        wire_state = "focused"
    elif state == "slacking":
        wire_state = "procrastinating"
    elif state == "distracted":
        wire_state = "distracted"
    else:
        wire_state = "waiting"

    score = decision.get("focus_percent")
    score_value = None if score is None else max(0, min(100, int(round(float(score)))))
    app = str(screen.get("app") or "")[:24]
    payload = {
        "state": wire_state,
        "score": score_value,
        "duration": round(float(screen.get("state_duration", 0) or 0), 1),
        "signal_ok": state not in {"eeg_invalid", "waiting_eeg"},
    }
    if app:
        payload["app"] = app
    return payload


class UnoQBridge:
    """Own the laptop-to-UNO-Q serial connection and one-hertz forwarder."""

    def __init__(self, camera_dir: Path, *, device: Optional[str], publisher: Publisher) -> None:
        self.camera_dir = Path(camera_dir)
        self.device = device  # serial port path or None for auto-detect
        self.publisher = publisher
        self.client: Any = None
        self._client_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    @property
    def resolved_device(self) -> Optional[str]:
        """The resolved serial port path (for display / logging)."""
        return self.device

    async def _publish_status(self, *, state: str, error: Optional[str] = None) -> None:
        message = {
            "type": "uno_q_status",
            "state": state,
            "connected": state == "connected",
        }
        if error:
            message["error"] = error
        await self.publisher(message)

    async def _on_state(self, state: Any) -> None:
        value = getattr(state, "value", str(state))
        await self._publish_status(state=value)

    async def _on_error(self, error: Any) -> None:
        await self._publish_status(state="error", error=str(error))

    async def _on_message(self, message: Any) -> None:
        await self.publisher({
            "type": "uno_q_message",
            "message_type": message.type,
            "seq": message.seq,
            "data": message.data,
        })

    async def pre_discover(
        self,
        *,
        timeout: float = 12.0,
        scanner_cls: Any = None,
        service_uuid: Optional[str] = None,
    ) -> bool:
        """Resolve the serial port before the headband connection.

        With serial transport there is no BLE advertisement scanning — we
        simply verify that the requested port (or an auto-detected Arduino
        device) can be opened.  The actual communication starts in ``start()``.

        The *scanner_cls* and *service_uuid* parameters are accepted for API
        compatibility with the BLE-based caller but are ignored.
        """
        if str(self.camera_dir) not in sys.path:
            sys.path.insert(0, str(self.camera_dir))
        from serial_protocol import auto_resolve_port

        await self._publish_status(state="resolving")
        try:
            resolved = await auto_resolve_port(self.device)
        except Exception as exc:
            await self._publish_status(state="error", error=str(exc))
            LOGGER.warning("UNO Q serial port resolution failed: %s", exc)
            return False

        self.device = resolved
        LOGGER.info("UNO Q serial port resolved: %s", resolved)
        await self._publish_status(state="resolved")
        return True

    async def start(self) -> None:
        if str(self.camera_dir) not in sys.path:
            sys.path.insert(0, str(self.camera_dir))
        from serial_client import SerialFocusFlowClient
        from ble import windows_ble_protocol

        # Protocol v1.1 addition. Older teammate firmware must update both its
        # UPLINK_TYPES set and validate_data() before it can accept this type.
        install_decision_protocol(windows_ble_protocol)

        self.client = SerialFocusFlowClient(
            port=self.device,
            baudrate=115200,
        )
        self.client.add_state_handler(self._on_state)
        self.client.add_error_handler(self._on_error)
        self.client.add_message_handler(self._on_message)
        self._client_task = asyncio.create_task(self.client.run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self.client is not None:
            await self.client.stop()
        if self._client_task is not None:
            try:
                await asyncio.wait_for(self._client_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._client_task.cancel()
                await asyncio.gather(self._client_task, return_exceptions=True)

    async def send_decision(self, decision: Optional[dict], screen: Optional[dict], *, resting: bool) -> bool:
        if self.client is None:
            return False
        return await self.client.send_message(
            "decision_update",
            decision_payload(decision, screen, resting=resting),
        )

    async def forward_loop(self, eeg_reader: Any) -> None:
        last_rest_active = False
        previous_remaining = 0
        while not self._stop.is_set():
            rest = eeg_reader.rest_snapshot()
            active = bool(rest["active"])
            if self.client is not None and self.client.connected:
                if active and not last_rest_active:
                    await self.client.send_rest_command(
                        "start",
                        duration=max(1, int(rest["remaining_seconds"])),
                        reason=str(rest.get("reason") or "manual"),
                    )
                    print(f"[UNO Q] ▶ rest start  duration={max(1, int(rest['remaining_seconds']))}s")
                elif not active and last_rest_active and previous_remaining > 2:
                    # Manual early stop. Natural expiry is handled by the UNO Q's
                    # matching countdown, avoiding a late STATE_CONFLICT reply.
                    await self.client.send_rest_command("stop", reason=None)
                    print("[UNO Q] ▶ rest stop")

                payload = decision_payload(
                    eeg_reader.latest_focus_decision(),
                    eeg_reader.latest_screen_context(),
                    resting=active,
                )
                await self.send_decision(
                    eeg_reader.latest_focus_decision(),
                    eeg_reader.latest_screen_context(),
                    resting=active,
                )
                print(f"[UNO Q] ▶ decision_update  state={payload['state']}  "
                      f"score={payload['score']}  signal_ok={payload['signal_ok']}")
            last_rest_active = active
            previous_remaining = int(rest.get("remaining_seconds", 0) or 0)
            await asyncio.sleep(1.0)
