"""Wristband BLE GATT server lifecycle wrapper.

This module wraps :class:`ble_server.HandGattServer` (the BLE peripheral
that talks to the ESP32-C3 wristband) so the rest of the FocusFlow
integration can treat it as a plain object with start / stop / send /
query-state methods.  All async work is scheduled on the asyncio loop
that :mod:`reconnect_supervisor` owns; the public API here is sync so
the rest of the code does not need to know about asyncio at all.

Why a wrapper instead of using ``HandGattServer`` directly?

* ``HandGattServer.send_command`` must be called from the asyncio loop
  that owns the D-Bus bus connection (it ends in a synchronous
  ``emit_properties_changed`` on the dbus-next ``ServiceInterface``).
  ``send_command_threadsafe`` already schedules the call on the right
  loop, but it raises if the server is not running — callers from the
  rest of the integration should never have to remember that.

* The reconnect supervisor needs ``is_running`` / ``is_subscribed``
  signals that survive a restart without leaking a stale connection.
  The wrapper holds a single ``HandGattServer`` instance and recreates
  it on each ``restart_async``.

* The wrapper exposes a single ``on_subscription_change`` callback that
  fires whenever the wristband (un)subscribes.  ``LinuxBLEServer``
  listens on this callback to push an updated ``device_status`` to the
  Windows client.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

# The wristband server is a top-level module under ``source_code/``;
# main.py is responsible for putting that directory on ``sys.path``
# before importing this module.
import ble_server

LOGGER = logging.getLogger("focusflow.wristband")

DEFAULT_DEVICE_NAME = "Hand-Control-Board"
DEFAULT_ADAPTER = "hci0"


SubscriptionCallback = Callable[[bool], None]


class WristbandController:
    """Thread-safe wrapper around :class:`ble_server.HandGattServer`."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        adapter: str = DEFAULT_ADAPTER,
        device_name: str = DEFAULT_DEVICE_NAME,
        advertise: bool = True,
        on_subscription_change: Optional[SubscriptionCallback] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._loop = loop
        self._adapter = adapter
        self._device_name = device_name
        self._advertise = advertise
        self._on_sub = on_subscription_change
        self._logger = logger or LOGGER

        # ``self._lock`` guards ``self._server`` against concurrent
        # restart calls coming from the reconnect supervisor and the
        # main thread.
        self._lock = threading.Lock()
        self._aio_lock: Optional[asyncio.Lock] = None

        self._server: Optional["ble_server.HandGattServer"] = None

        # ``self._subscribed`` mirrors ``HandGattServer.notifying`` but
        # can be read from any thread without a D-Bus round-trip.
        self._subscribed = False
        self._subscribed_at: Optional[float] = None

    # ── public, sync API ─────────────────────────────────────────────
    def is_running(self) -> bool:
        """Return True when the GATT application is registered with BlueZ."""

        server = self._server
        return bool(server and server.running)

    def is_subscribed(self) -> bool:
        """Return True when the wristband has called ``StartNotify``."""

        return self._subscribed

    def seconds_since_last_subscription(self) -> Optional[float]:
        """Time elapsed since the most recent subscription event.

        ``None`` when no subscription event has happened yet.
        """

        if self._subscribed_at is None:
            return None
        return self._loop.time() - self._subscribed_at

    def send_vibration(self, intensity: int, repeat_count: int) -> bool:
        """Send a vibration command (thread-safe, returns False if not subscribed).

        ``intensity`` is 0..100, ``repeat_count`` is 0..65535.  Returns
        ``False`` when the wristband is not currently subscribed — the
        caller can decide whether to queue or drop the command.
        """

        server = self._server
        if server is None or not server.running:
            return False
        try:
            future = server.send_command_threadsafe(intensity, repeat_count)
        except RuntimeError as exc:
            self._logger.debug("wristband send rejected: %s", exc)
            return False
        try:
            return bool(future.result(timeout=1.0))
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.warning("wristband send raised: %s", exc)
            return False

    def stop_vibration(self) -> bool:
        """Shortcut for ``send_vibration(0, 0)`` (immediate stop)."""

        return self.send_vibration(0, 0)

    # ── async API (called from the supervisor / startup loop) ────────
    async def start_async(self) -> bool:
        """Create a fresh ``HandGattServer`` and register it with BlueZ.

        Returns True on success.  If a server was already running it is
        stopped first so we never leak a duplicate registration.
        """

        async with self._async_lock():
            if self._server is not None and self._server.running:
                await self._stop_locked()
            # Pass the subscription callback into the constructor —
            # ``HandGattServer`` does not expose a public setter for it.
            server = ble_server.HandGattServer(
                adapter=self._adapter,
                device_name=self._device_name,
                on_subscription_changed=self._handle_subscription_change,
                advertise=self._advertise,
            )
            try:
                await server.start()
            except Exception as exc:
                self._logger.error("wristband GATT start failed: %s", exc)
                self._server = None
                self._set_subscribed(False)
                return False
            self._server = server
            self._set_subscribed(server.notifying)
            self._logger.info(
                "wristband GATT server running (adapter=%s, name=%s, advertise=%s)",
                self._adapter, self._device_name, self._advertise,
            )
            return True

    async def stop_async(self) -> None:
        """Unregister the GATT application and disconnect from D-Bus."""

        async with self._async_lock():
            await self._stop_locked()

    async def restart_async(self) -> bool:
        """Tear down + bring back up.  Used by the reconnect supervisor."""

        await self.stop_async()
        return await self.start_async()

    # ── internals ─────────────────────────────────────────────────────
    async def _stop_locked(self) -> None:
        server = self._server
        self._server = None
        self._set_subscribed(False)
        if server is None:
            return
        try:
            await server.stop()
        except Exception as exc:
            self._logger.debug("wristband stop raised: %s", exc)

    def _async_lock(self):
        # ``asyncio.Lock`` must be created inside the running loop.
        if self._aio_lock is None:
            self._aio_lock = asyncio.Lock()
        return self._aio_lock

    def _handle_subscription_change(self, notifying: bool) -> None:
        # Invoked on the asyncio loop that owns the D-Bus bus.
        self._set_subscribed(notifying)

    def _set_subscribed(self, value: bool) -> None:
        prev = self._subscribed
        self._subscribed = value
        self._subscribed_at = self._loop.time()
        if prev != value and self._on_sub is not None:
            try:
                self._on_sub(value)
            except Exception:  # pragma: no cover - callback errors are fatal
                self._logger.exception("on_subscription_change callback failed")
