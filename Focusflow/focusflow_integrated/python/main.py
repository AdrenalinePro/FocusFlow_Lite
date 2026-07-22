"""FocusFlow Integration — Arduino App entry point.

This module is invoked by the App Lab framework at runtime.  It owns:

* the asyncio event loop that drives the BlueZ-based BLE subsystems;
* the bridge between that asyncio loop and the App framework's main
  thread (``Bridge.notify`` / ``Bridge.provide`` are sync and safe to
  call from any thread);
* the lifecycle of every long-running coroutine.

The module deliberately does not import from ``source_code/`` at module
top level — the path is wired up in ``_setup_source_path()`` so that
moving the focusflow_integrated/ folder around does not break anything.

The integration of three subsystems is described in the README; in
short:

    Windows laptop  ⇄  LinuxBLEServer  (source_code/linux/)
                         │ state changes
                         ▼
                    TFT JSON ──► Router Bridge ──► STM32 sketch (TFT)
                         │
                         └─► WristbandController ──► ESP32-C3 wristband
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Arduino App framework — only present at runtime on the UNO Q.
try:  # pragma: no cover - exercised on the UNO Q only
    from arduino.app_utils import App, Bridge, Logger  # type: ignore
except ImportError:  # pragma: no cover - developer workstation
    App = None  # type: ignore
    Bridge = None  # type: ignore
    class _StubLogger:  # noqa: D401 - tiny shim
        def __getattr__(self, name):
            return lambda *a, **kw: None
    Logger = _StubLogger()  # type: ignore

LOGGER = logging.getLogger("focusflow.main")

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent.parent  # /home/arduino/Focusflow on the host

# The Arduino App CLI only bind-mounts the app folder into the
# container at /app.  The host-side /home/arduino/Focusflow/source_code
# and /home/arduino/focusble/windows trees are NOT visible inside the
# container, so we ship vendored copies of the three BLE modules we
# need:
#   linux/             ← source_code/linux/  (FocusFlow LinuxBLE stack)
#   ble_server.py      ← source_code/ble_server.py (wristband GATT)
#   windows/windows_ble_protocol.py  ← /home/arduino/focusble/windows
# Vendored copies live next to this file; the code below adds them to
# sys.path the same way the upstream ``_setup_source_path`` helper did.

DEFAULT_LAPTOP_INTERVAL = 15.0
DEFAULT_WRISTBAND_ADAPTER = "hci0"
DEFAULT_LAPTOP_DEVICE_NAME = "UNO-Q-FF01"
DEFAULT_WRISTBAND_DEVICE_NAME = "Hand-Control-Board"

# Lazy globals — populated by main() once the asyncio loop is up.
_asyncio_thread: Optional[threading.Thread] = None
_asyncio_loop = None
_wristband = None
_tft = None
_server = None
_supervisor = None
_shutdown_lock = threading.Lock()
_shutdown_done = False


def _setup_source_path() -> None:
    """Make the vendored BLE modules importable.

    The Arduino App CLI binds the app directory to /app inside the
    container.  We vendor the linux/ package, ble_server.py and
    windows/windows_ble_protocol.py right next to main.py, so adding
    APP_DIR to sys.path is sufficient.  On a developer workstation
    (running main.py directly without the App framework) the same
    sys.path entry also works.
    """

    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))


def _setup_dbus_proxy_env() -> None:
    """Point the BLE modules at the host-side D-Bus proxy.

    The App Lab container does not mount ``/run/dbus/system_bus_socket``
    (and there is no user-facing way to add it).  The host-side script
    ``host/start_dbus_proxy.sh`` runs socat to expose the real socket
    through ``/app/host/host-dbus-proxy.sock`` (visible in the
    container because ``.cache/`` lives inside the bind-mounted app
    directory).  We default to that path; the user can override via
    the ``FOCUSFLOW_DBUS_ADDRESS`` env var before launching the app.
    """

    if "DBUS_SYSTEM_BUS_ADDRESS" in os.environ:
        return
    proxy = os.environ.get(
        "FOCUSFLOW_DBUS_ADDRESS",
        "unix:path=" + str(APP_DIR.parent / "host" / "host-dbus-proxy.sock"),
    )
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = proxy
    LOGGER.info("DBUS_SYSTEM_BUS_ADDRESS -> %s", proxy)


def _patch_dbus_fast_in_container() -> bool:
    """Apply the FocusFlow patches to dbus_fast inside the container.

    The Arduino App container builds a fresh virtualenv on every start,
    so we have to apply the message_bus.py patch at runtime.  The patch
    script (linux/patch_dbus_fast.py) auto-discovers the venv
    site-packages dir from ``sys.path`` and rewrites
    ``_default_get_managed_objects_handler`` so BlueZ's
    ``RegisterApplication`` actually finds our GATT objects.

    Returns True when the patch is already in place (no work needed) or
    was applied successfully this run.
    """

    try:
        sys.path.insert(0, str(APP_DIR / "linux"))
        import patch_dbus_fast  # noqa: WPS433
    except Exception as exc:
        LOGGER.warning("could not load patch_dbus_fast: %s", exc)
        return False

    # Detect an outdated patch (the marker exists but the patched
    # message_bus.py is from a previous, incompatible run).  The
    # discriminator is the presence of the new list-vs-dict shim:
    # older patches call ``.values()`` on what is actually a list in
    # dbus_fast >= 1.95 and break GATT registration.
    needs_repatch = False
    try:
        src = patch_dbus_fast.MESSAGE_BUS.read_text()
    except OSError as exc:
        LOGGER.warning("cannot read message_bus.py: %s", exc)
        return False
    has_new_shim = "isinstance(entries, dict)" in src
    if patch_dbus_fast.MARKER.exists() and not has_new_shim:
        LOGGER.warning(
            "dbus_fast patch marker exists but the patch is the old "            "incompatible version; re-patching",
        )
        needs_repatch = True
        try:
            patch_dbus_fast.MARKER.unlink()
        except OSError:
            pass
    elif patch_dbus_fast.MARKER.exists() and has_new_shim:
        LOGGER.info("dbus_fast patch already applied (current version)")
        return True

    try:
        rc = patch_dbus_fast.apply()
    except Exception as exc:
        LOGGER.error("dbus_fast patch failed: %s", exc)
        return False
    if rc != 0:
        LOGGER.error("dbus_fast patch returned %d", rc)
        return False
    LOGGER.info("dbus_fast patched inside the container")
    return True


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("FOCUSFLOW_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    # Tame dbus-fast / dbus-next / asyncio to WARNING; their DEBUG
    # output is too noisy for a long-running app.
    for noisy in ("dbus_fast", "dbus_next", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _start_asyncio_thread() -> "asyncio.AbstractEventLoop":
    """Spin up a dedicated event loop in a daemon thread.

    The Arduino App framework's main thread runs ``App.run`` which is
    synchronous, but the BlueZ D-Bus stack we depend on requires an
    asyncio loop.  Running our own loop in a daemon thread keeps the
    two worlds separated; ``Bridge.notify`` / ``Bridge.provide`` are
    documented as thread-safe so we can call them from either side.
    """

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(
        target=_runner, name="focusflow-asyncio", daemon=True,
    )
    thread.start()
    if not ready.wait(timeout=2.0):
        raise RuntimeError("background asyncio loop did not start")
    return loop


def _schedule(coro):
    """Schedule ``coro`` on the background loop and return a Future."""

    return asyncio.run_coroutine_threadsafe(coro, _asyncio_loop)


def _start_subsystems() -> None:
    """Bring up the wristband GATT server, the FocusFlow BLE server and the supervisor."""

    from ble_server import SERVICE_UUID as WRISTBAND_SERVICE_UUID
    from linux.linux_ble_server import BleServerConfig
    from focusflow_server import FocusFlowBLEServer
    from reconnect_supervisor import ReconnectSupervisor
    from tft_bridge import TFTBridge
    from wristband_controller import WristbandController

    global _wristband, _tft, _server, _supervisor

    _tft = TFTBridge(logger=logging.getLogger("focusflow.tft"))
    _wristband = WristbandController(
        loop=_asyncio_loop,
        adapter=os.environ.get("FOCUSFLOW_WRISTBAND_ADAPTER", DEFAULT_WRISTBAND_ADAPTER),
        device_name=DEFAULT_WRISTBAND_DEVICE_NAME,
        advertise=False,
        on_subscription_change=_on_wristband_subscription_change,
        logger=logging.getLogger("focusflow.wristband"),
    )

    config = BleServerConfig(
        device_name=DEFAULT_LAPTOP_DEVICE_NAME,
        # The controller owns one advertisement.  The ESP32 finds it by
        # this UUID; Windows connects to the same peripheral and discovers
        # the separately registered FocusFlow GATT service afterwards.
        advertised_service_uuids=(WRISTBAND_SERVICE_UUID,),
        focus_score_interval=1.0,
        device_status_interval=10.0,
        auto_rest_countdown=True,
        rest_countdown_interval=10.0,
        emit_ready_pattern=True,
    )

    _server = FocusFlowBLEServer(
        config=config,
        wristband=_wristband,
        tft=_tft,
        vibration_intensity=int(os.environ.get(
            "FOCUSFLOW_VIBRATION_INTENSITY", "40",
        )),
        logger=logging.getLogger("focusflow.server"),
    )

    _supervisor = ReconnectSupervisor(
        server=_server,
        wristband=_wristband,
        logger=logging.getLogger("focusflow.supervisor"),
    )

    async def _boot() -> None:
        LOGGER.info("starting wristband GATT server")
        ok = await _wristband.start_async()
        LOGGER.info("wristband GATT server start: %s", ok)

        LOGGER.info("starting laptop LinuxBLEServer")
        # LinuxBLEServer.run() blocks until stop() — schedule it as a
        # background task and give the GATT registration a moment to
        # complete before we ask for its state.
        _server_task = asyncio.create_task(_server.run())
        _server_task.set_name("focusflow-linux-ble")
        await asyncio.sleep(2.0)
        LOGGER.info("laptop BLE server state: %s", _server.state)

        # Initial TFT render so the screen is not blank.
        _tft.show_focus(pct=82, screen="等待 Windows 连接", status="待机")

        LOGGER.info("starting reconnect supervisor")
        asyncio.create_task(_supervisor.run())

    _schedule(_boot())


def _on_wristband_subscription_change(subscribed: bool) -> None:
    """Called from the asyncio loop when the wristband (un)subscribes.

    We just push a fresh ``device_status`` over BLE so the Windows
    client sees the change immediately instead of waiting for the
    next periodic tick.
    """

    if _server is None:
        return
    LOGGER.info("wristband subscription -> %s", subscribed)

    async def _push() -> None:
        try:
            await _server.send_device_status(
                **_server._snapshot_device_status(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("device_status push after subscription change failed: %s", exc)

    _schedule(_push())


def _shutdown() -> None:
    """Tear everything down.  Safe to call multiple times."""

    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True

    LOGGER.info("shutting down FocusFlow integration")
    if _supervisor is not None:
        _supervisor.stop()
    if _server is not None:
        try:
            _schedule(_server.stop())
        except Exception:
            pass
    if _wristband is not None:
        try:
            _schedule(_wristband.stop_async())
        except Exception:
            pass
    # Give the loop a moment to drain the shutdown coroutines.
    time.sleep(0.5)
    if _asyncio_loop is not None:
        try:
            _asyncio_loop.call_soon_threadsafe(_asyncio_loop.stop)
        except Exception:
            pass
    LOGGER.info("shutdown complete")


def _install_signal_handlers() -> None:
    """Trigger a graceful shutdown on SIGINT / SIGTERM."""

    def _handler(signum, _frame):
        LOGGER.info("received signal %s, shutting down", signum)
        _shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not on the main thread, or restricted environment — skip.
            pass


def _user_loop() -> None:
    """The function Arduino App Lab calls repeatedly.

    The actual work happens on the background asyncio loop, so this
    function just yields the CPU and lets the framework breathe.
    """

    time.sleep(1.0)


def main() -> None:
    _configure_logging()
    _setup_source_path()
    _setup_dbus_proxy_env()
    _patch_dbus_fast_in_container()
    _install_signal_handlers()

    LOGGER.info("FocusFlow integration starting")
    LOGGER.info("app dir: %s", APP_DIR)

    global _asyncio_loop
    _asyncio_loop = _start_asyncio_thread()
    _start_subsystems()

    if App is None:
        LOGGER.warning(
            "arduino.app_utils not importable; running in foreground "
            "(developer workstation mode, Ctrl+C to stop)"
        )
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            _shutdown()
        return

    # Bridge health: the MCU pushes tft_heartbeat every 5 s; the
    # tft_bridge already registers a callback in its __init__, so we
    # only need to log readiness here.
    try:
        Bridge.notify("integration_ready", "linux")
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("Bridge.notify(integration_ready) failed: %s", exc)

    App.run(user_loop=_user_loop)


if __name__ == "__main__":
    main()
