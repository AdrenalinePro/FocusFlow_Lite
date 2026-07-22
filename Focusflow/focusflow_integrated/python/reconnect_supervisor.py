"""Periodic reconnection supervisor for the FocusFlow integration.

The supervisor wakes up every ``interval_seconds`` and verifies two
long-lived BLE subsystems:

* the Linux-side GATT server that talks to the Windows laptop
  (``LinuxBLEServer``);
* the wristband GATT server that talks to the ESP32-C3 wristband
  (``WristbandController``).

If a subsystem has dropped out of its expected steady state, the
supervisor restarts it.  This satisfies the project requirement that
"all Bluetooth connections should be retried at intervals after
program start".

Restart policy
--------------

Laptop (LinuxBLEServer)
    Restart when the server's high-level state is one of
    ``STOPPED``, ``STARTING`` or ``ERROR``, or when it has been stuck in
    ``ADVERTISING`` for ``advertising_stale_seconds`` without reaching
    ``CONNECTED`` / ``NOTIFY_READY`` (i.e. Windows is not connecting).

Wristband (WristbandController)
    Restart when the GATT application is not running, or when the
    wristband has been unsubscribed for longer than
    ``subscription_stale_seconds`` (i.e. it disconnected and never
    came back).

Every restart is logged.  The supervisor uses exponential backoff so a
permanently-broken subsystem does not flood the log; once a restart
succeeds the backoff resets.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from linux.linux_ble_server import BleServerState, LinuxBLEServer
from wristband_controller import WristbandController

LOGGER = logging.getLogger("focusflow.supervisor")


RestartFn = Callable[[], Awaitable[bool]]


@dataclass
class SupervisorConfig:
    """Knobs for the supervisor.

    The defaults are conservative on purpose: a *working* BLE link is
    the steady state, and the supervisor should not gratuitously tear
    it down.  The previous defaults of 60 s ``subscription_stale_seconds``
    caused the supervisor to restart the wristband GATT app every 75 s
    while it was idle, which on a QCA-based UNO Q would also break the
    laptop-side BLE link whenever the two shared the same adapter
    (the BlueZ daemon would tear down unrelated centrals during
    ``UnregisterApplication``).

    * ``subscription_stale_seconds=300`` — wait five minutes of
      *continuous* un-subscribed time before considering the wristband
      GATT server broken.  Short blips from a flaky ESP32 are tolerated.
    * ``max_backoff_seconds=300`` — backoff caps at five minutes too;
      a hard failure still recovers, just less aggressively.
    """

    interval_seconds: float = 15.0
    advertising_stale_seconds: float = 120.0
    subscription_stale_seconds: float = 300.0
    # Cap backoff so a permanent failure does eventually retry quickly.
    max_backoff_seconds: float = 300.0


class ReconnectSupervisor:
    """Periodically verifies and restarts the BLE subsystems."""

    def __init__(
        self,
        *,
        server: LinuxBLEServer,
        wristband: WristbandController,
        on_laptop_restart: Optional[RestartFn] = None,
        on_wristband_restart: Optional[RestartFn] = None,
        config: Optional[SupervisorConfig] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._server = server
        self._wristband = wristband
        self._on_laptop_restart = on_laptop_restart
        self._on_wristband_restart = on_wristband_restart
        self._config = config or SupervisorConfig()
        self._logger = logger or LOGGER

        self._stop_event: Optional[asyncio.Event] = None
        self._laptop_backoff = 0.0
        self._wristband_backoff = 0.0

    # ── lifecycle ──────────────────────────────────────────────────
    async def run(self) -> None:
        """Run the supervisor until :meth:`stop` is called."""

        self._stop_event = asyncio.Event()
        self._logger.info(
            "supervisor starting (interval=%.1fs, ad-stale=%.1fs, sub-stale=%.1fs)",
            self._config.interval_seconds,
            self._config.advertising_stale_seconds,
            self._config.subscription_stale_seconds,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self._check_once()
                except Exception:  # pragma: no cover - defensive
                    self._logger.exception("supervisor check raised")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._logger.info("supervisor stopped")

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    # ── per-tick checks ────────────────────────────────────────────
    async def _check_once(self) -> None:
        await self._check_laptop()
        await self._check_wristband()

    async def _check_laptop(self) -> None:
        state = self._server.state
        # Healthy states: ADVERTISING, CONNECTED, NOTIFY_READY.
        if state in (BleServerState.ADVERTISING,
                     BleServerState.CONNECTED,
                     BleServerState.NOTIFY_READY):
            self._laptop_backoff = 0.0
            return

        # Dead states: STOPPED, STARTING (still warming up), ERROR.
        if state == BleServerState.STARTING and self._laptop_backoff < 5.0:
            # Still warming up — give it a few seconds before we panic.
            return

        self._laptop_backoff = min(
            self._config.max_backoff_seconds,
            self._laptop_backoff * 2.0 + self._config.interval_seconds,
        )
        self._logger.warning(
            "laptop BLE server unhealthy (state=%s); restarting in %.1fs",
            state.value if hasattr(state, "value") else state,
            self._laptop_backoff,
        )
        await asyncio.sleep(self._laptop_backoff)
        if self._stop_event is not None and self._stop_event.is_set():
            return
        # Re-check the live state after the sleep.  The early-return
        # above only catches a healthy state at the *start* of the
        # tick — a Windows client that connected during the backoff
        # would otherwise be killed by the restart we are about to
        # perform.  ADVERTISING / CONNECTED / NOTIFY_READY are all
        # "good" — the state will move to CONNECTED / NOTIFY_READY as
        # soon as a Windows client subscribes.
        if self._server.state in (
            BleServerState.ADVERTISING,
            BleServerState.CONNECTED,
            BleServerState.NOTIFY_READY,
        ):
            self._logger.info(
                "laptop BLE server recovered during backoff "
                "(state=%s); skipping restart",
                self._server.state.value
                if hasattr(self._server.state, "value")
                else self._server.state,
            )
            self._laptop_backoff = 0.0
            return
        try:
            if self._on_laptop_restart is not None:
                ok = await self._on_laptop_restart()
            else:
                ok = await self._default_laptop_restart()
            if ok:
                self._laptop_backoff = 0.0
                self._logger.info("laptop BLE server restarted successfully")
            else:
                self._logger.warning("laptop BLE server restart returned False")
        except Exception as exc:
            self._logger.error("laptop BLE restart failed: %s", exc)

    async def _check_wristband(self) -> None:
        if self._wristband.is_running() and self._wristband.is_subscribed():
            self._wristband_backoff = 0.0
            return

        if self._wristband.is_running() and not self._wristband.is_subscribed():
            # Server up but no one subscribed.  Only restart if it has
            # been un-subscribed for longer than the threshold; this
            # tolerates normal "phone paired, no app open" gaps.
            since = self._wristband.seconds_since_last_subscription()
            if since is not None and since < self._config.subscription_stale_seconds:
                return
            reason = "no subscription for %.1fs" % (since or 0.0)
        elif not self._wristband.is_running():
            reason = "GATT application not registered"
        else:
            return

        self._wristband_backoff = min(
            self._config.max_backoff_seconds,
            self._wristband_backoff * 2.0 + self._config.interval_seconds,
        )
        self._logger.warning(
            "wristband BLE unhealthy (%s); restarting in %.1fs",
            reason, self._wristband_backoff,
        )
        await asyncio.sleep(self._wristband_backoff)
        if self._stop_event is not None and self._stop_event.is_set():
            return
        # IMPORTANT: re-check the live state *after* the sleep.  Without
        # this, a client (ESP32 wristband or Windows laptop) that
        # connected while the supervisor was waiting would be killed
        # by the very restart we are about to perform — the original
        # early-return above only fires at the *start* of the tick.
        # This was the root cause of the "Windows shows reconnecting"
        # UI and the wristband dropping out every ~75 s.
        if not self._wristband.is_running():
            reason = "GATT application not registered (re-check)"
        elif self._wristband.is_subscribed():
            self._logger.info(
                "wristband BLE recovered during backoff "
                "(subscribed=%s); skipping restart",
                self._wristband.is_subscribed(),
            )
            self._wristband_backoff = 0.0
            return
        else:
            since = self._wristband.seconds_since_last_subscription()
            self._logger.info(
                "wristband still unhealthy after backoff "
                "(no subscription for %.1fs); proceeding with restart",
                since or 0.0,
            )
        try:
            if self._on_wristband_restart is not None:
                ok = await self._on_wristband_restart()
            else:
                ok = await self._default_wristband_restart()
            if ok:
                self._wristband_backoff = 0.0
                self._logger.info("wristband BLE restarted successfully")
            else:
                self._logger.warning("wristband BLE restart returned False")
        except Exception as exc:
            self._logger.error("wristband BLE restart failed: %s", exc)

    # ── default restart callbacks ──────────────────────────────────
    async def _default_laptop_restart(self) -> bool:
        """Stop the existing server and schedule a fresh run.

        LinuxBLEServer.run() blocks until stop() is called, so the
        supervisor cannot await it; we have to re-schedule it as a task
        after stop() completes.
        """

        try:
            await self._server.stop()
        except Exception as exc:
            self._logger.debug("laptop stop raised: %s", exc)
        # Let the run() coroutine drain to completion.
        await asyncio.sleep(1.5)
        try:
            new_task = asyncio.create_task(self._server.run())
            new_task.set_name("focusflow-linux-ble-restart")
        except Exception as exc:
            self._logger.error("laptop start failed: %s", exc)
            return False
        # Give the new server a moment to register.
        await asyncio.sleep(2.0)
        return self._server.state not in (
            BleServerState.STOPPED, BleServerState.ERROR,
        )

    async def _default_wristband_restart(self) -> bool:
        return await self._wristband.restart_async()
