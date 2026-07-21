"""PyQt5 thread bridge for :mod:`windows_ble_client`.

All BLE asyncio work stays in the QThread's event loop.  Public send methods
are safe to call from the Qt GUI thread and never call ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .windows_ble_client import BleClientConfig, BleConnectionState, WindowsBLEClient
from .windows_ble_protocol import BLEMessage

try:
    from PyQt5.QtCore import QThread, pyqtSignal
except ImportError as exc:  # pragma: no cover - exercised only without PyQt5
    _PYQT_IMPORT_ERROR = exc
    QThread = None  # type: ignore
else:
    _PYQT_IMPORT_ERROR = None


if QThread is not None:

    class WindowsBLEClientThread(QThread):
        """Run :class:`WindowsBLEClient` without blocking the Qt GUI thread."""

        connection_state_signal = pyqtSignal(str)
        connected_signal = pyqtSignal(bool)
        message_signal = pyqtSignal(dict)
        state_update_signal = pyqtSignal(dict)
        focus_score_signal = pyqtSignal(int, str)
        rest_countdown_signal = pyqtSignal(int, int, str)
        display_content_signal = pyqtSignal(dict)
        device_status_signal = pyqtSignal(dict)
        vibration_feedback_signal = pyqtSignal(dict)
        heartbeat_signal = pyqtSignal(dict)
        sync_response_signal = pyqtSignal(dict)
        error_signal = pyqtSignal(str)

        def __init__(self, config: Optional[BleClientConfig] = None, parent: Any = None) -> None:
            super().__init__(parent)
            self.config = config or BleClientConfig()
            self._loop: Optional[asyncio.AbstractEventLoop] = None
            self._client: Optional[WindowsBLEClient] = None
            self._stop_requested = False

        def run(self) -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._client = WindowsBLEClient(self.config)
            self._client.add_state_handler(self._on_state)
            self._client.add_message_handler(self._on_message)
            self._client.add_error_handler(self.error_signal.emit)
            try:
                if self._stop_requested:
                    self._loop.create_task(self._client.stop())
                self._loop.run_until_complete(self._client.run_forever())
            finally:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()
                self._loop = None
                self._client = None

        def _on_state(self, state: BleConnectionState) -> None:
            value = state.value if isinstance(state, BleConnectionState) else str(state)
            self.connection_state_signal.emit(value)
            self.connected_signal.emit(value == BleConnectionState.CONNECTED.value)

        def _on_message(self, message: BLEMessage) -> None:
            data = dict(message.data)
            self.message_signal.emit({
                "type": message.type, "seq": message.seq, "ts": message.ts, "data": data
            })
            if message.type == "state_update":
                self.state_update_signal.emit(data)
            elif message.type == "focus_score":
                self.focus_score_signal.emit(data["score"], data["state"])
            elif message.type == "rest_countdown":
                self.rest_countdown_signal.emit(data["remaining"], data["total"], data["phase"])
            elif message.type == "display_content":
                self.display_content_signal.emit(data)
            elif message.type == "device_status":
                self.device_status_signal.emit(data)
            elif message.type == "vibration_feedback":
                self.vibration_feedback_signal.emit(data)
            elif message.type == "heartbeat":
                self.heartbeat_signal.emit(data)
            elif message.type == "sync_response":
                self.sync_response_signal.emit(data)
            elif message.type == "error":
                # Surface application-layer errors through the dedicated
                # ``error_signal`` so GUI handlers can show a notification
                # without having to filter ``message_signal`` themselves.
                self.error_signal.emit(data.get("message", "UNO Q returned an error"))

        def _submit(self, coroutine: Any) -> Any:
            if self._loop is None or self._client is None or not self.isRunning():
                return None
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

        def stop(self) -> None:
            """Request shutdown; call ``wait`` if the application is exiting."""
            self._stop_requested = True
            future = None
            if self._loop is not None and self._client is not None:
                future = asyncio.run_coroutine_threadsafe(self._client.stop(), self._loop)
            # 10 s gives the bleak disconnect + asyncio cleanup enough time to
            # finish on slow Windows stacks; the previous 5 s budget caused
            # ``QThread.wait`` to return before ``run_forever`` had unwound.
            if self.isRunning() and not self.wait(10000):
                import logging
                logging.getLogger(__name__).warning(
                    "BLE thread did not stop within 10s; forcing shutdown")
                if future is not None:
                    try:
                        future.result(timeout=2)
                    except Exception:
                        logging.getLogger(__name__).debug(
                            "BLE stop coroutine did not complete", exc_info=True)

        def send_eye_data(self, yaw: float, pitch: float, is_focused: int,
                          state_duration: float, confidence: float) -> Any:
            if self._client is None:
                return None
            return self._submit(self._client.send_eye_data(
                yaw, pitch, is_focused, state_duration, confidence
            ))

        def send_screen_data(self, state: str, confidence: float,
                             app: Optional[str] = None, category: Optional[str] = None) -> Any:
            if self._client is None:
                return None
            return self._submit(self._client.send_screen_data(state, confidence, app, category))

        def send_rest_command(self, action: str, duration: Optional[int] = None,
                              reason: Optional[str] = "manual") -> Any:
            if self._client is None:
                return None
            return self._submit(self._client.send_rest_command(action, duration, reason))

        def send_sync_request(self, fields: Optional[list] = None) -> Any:
            if self._client is None:
                return None
            return self._submit(self._client.send_sync_request(fields))

else:

    class WindowsBLEClientThread:  # pragma: no cover - simple optional-dependency guard
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError("PyQt5 is required for WindowsBLEClientThread") from _PYQT_IMPORT_ERROR
