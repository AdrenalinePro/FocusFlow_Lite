#!/usr/bin/env python3
"""Linux/BlueZ BLE GATT server for the ESP32-C3 wristband.

The board is a BLE peripheral and the wristband is a BLE central.  After the
wristband subscribes to the notify-only control characteristic, application
code can call ``HandGattServer.send_vibration()`` to send a command.

Run this file in a terminal for an interactive console, or import
``HandGattServer`` from another Python application.
"""

import argparse
import asyncio
import concurrent.futures
import logging
import signal
import sys
from typing import Callable, Optional

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, PropertyAccess
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, dbus_property, method


SERVICE_UUID = "7b3a0001-6a4f-4d91-9c10-123456789000"
CHARACTERISTIC_UUID = "7b3a0002-6a4f-4d91-9c10-123456789000"
DEFAULT_DEVICE_NAME = "Hand-Control-Board"

BLUEZ_SERVICE = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"

APP_PATH = "/com/example/hand"
SERVICE_PATH = f"{APP_PATH}/service0"
CHARACTERISTIC_PATH = f"{SERVICE_PATH}/control"
ADVERTISEMENT_PATH = f"{APP_PATH}/advertisement0"

LOGGER = logging.getLogger("hand_ble")
SubscriptionCallback = Callable[[bool], None]


def pack_command(intensity: int, repeat_count: int) -> bytes:
    """Encode a wristband command as exactly three bytes.

    ``intensity`` is 0..100. ``repeat_count`` is an unsigned 16-bit value and
    is encoded little-endian.  A zero in either field tells the wristband to
    stop its current action.
    """

    if not isinstance(intensity, int) or isinstance(intensity, bool):
        raise TypeError("intensity must be an integer")
    if not isinstance(repeat_count, int) or isinstance(repeat_count, bool):
        raise TypeError("repeat_count must be an integer")
    if not 0 <= intensity <= 100:
        raise ValueError("intensity must be in the range 0..100")
    if not 0 <= repeat_count <= 0xFFFF:
        raise ValueError("repeat_count must be in the range 0..65535")

    return bytes(
        (
            intensity,
            repeat_count & 0xFF,
            (repeat_count >> 8) & 0xFF,
        )
    )


class Application(ServiceInterface):
    """D-Bus ObjectManager used by BlueZ to discover the local GATT tree."""

    def __init__(
        self, service: "GattService", characteristic: "ControlCharacteristic"
    ) -> None:
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self._service = service
        self._characteristic = characteristic

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        return {
            SERVICE_PATH: {
                "org.bluez.GattService1": {
                    "UUID": Variant("s", self._service.UUID),
                    "Primary": Variant("b", self._service.Primary),
                }
            },
            CHARACTERISTIC_PATH: {
                "org.bluez.GattCharacteristic1": {
                    "UUID": Variant("s", self._characteristic.UUID),
                    "Service": Variant("o", self._characteristic.Service),
                    "Flags": Variant("as", self._characteristic.Flags),
                    "Value": Variant("ay", self._characteristic.Value),
                    "Notifying": Variant("b", self._characteristic.Notifying),
                }
            },
        }


class GattService(ServiceInterface):
    """Primary control service exported to BlueZ."""

    def __init__(self) -> None:
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return SERVICE_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True


class ControlCharacteristic(ServiceInterface):
    """Notify-only characteristic that carries three-byte motion commands."""

    def __init__(
        self, on_notify_state_changed: Optional[SubscriptionCallback] = None
    ) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self._value = b""
        self._notifying = False
        self._on_notify_state_changed = on_notify_state_changed

    @property
    def notifying(self) -> bool:
        """Whether at least one remote client has enabled notifications."""

        return self._notifying

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return CHARACTERISTIC_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["notify"]

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self._notifying

    def _set_notifying(self, value: bool, *, emit: bool) -> None:
        if self._notifying == value:
            return

        self._notifying = value
        if emit:
            self.emit_properties_changed({"Notifying": value})

        if self._on_notify_state_changed is not None:
            try:
                self._on_notify_state_changed(value)
            except Exception:
                # A business callback must not turn a valid D-Bus request into
                # an error response to BlueZ.
                LOGGER.exception("subscription callback failed")

    @method()
    def StartNotify(self) -> None:
        self._set_notifying(True, emit=True)
        LOGGER.info("wristband subscribed to control notifications")

    @method()
    def StopNotify(self) -> None:
        self._set_notifying(False, emit=True)
        LOGGER.info("wristband unsubscribed from control notifications")

    def update_value(self, packet: bytes) -> None:
        """Emit ``Value`` as a notification through BlueZ."""

        self._value = packet
        self.emit_properties_changed({"Value": packet})

    def reset_notify_state(self) -> None:
        """Clear local state while the D-Bus application is shutting down."""

        self._set_notifying(False, emit=False)


class Advertisement(ServiceInterface):
    """Connectable LE advertisement containing the control service UUID."""

    def __init__(self, local_name: str) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self._local_name = local_name
        self._tx_power = 0

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self._local_name

    @dbus_property(access=PropertyAccess.READWRITE)
    def TxPower(self) -> "n":
        # BlueZ 5.82 reads this optional property and writes back the power
        # selected by the controller when CanSetTxPower is supported.  The
        # setter keeps compatibility with that behavior.
        return self._tx_power

    @TxPower.setter
    def TxPower(self, value: "n") -> None:
        self._tx_power = value
        LOGGER.debug("BlueZ selected advertisement TX power: %d dBm", value)

    @method()
    def Release(self) -> None:
        LOGGER.info("BlueZ released the advertisement")


class HandGattServer:
    """Reusable BLE server API for board-side application code.

    The public business interfaces are:

    * :meth:`start` / :meth:`stop` for the server lifecycle;
    * :meth:`wait_until_subscribed` to wait for the wristband;
    * :meth:`send_vibration` (or :meth:`send_command`) to send a command;
    * :meth:`stop_vibration` to send the immediate-stop packet;
    * :meth:`send_command_threadsafe` for callbacks running in another thread.

    ``send_command`` returns ``False`` if the wristband has not subscribed and
    returns ``True`` after the notification has been emitted to BlueZ.
    """

    def __init__(
        self,
        adapter: str = "hci0",
        device_name: str = DEFAULT_DEVICE_NAME,
        on_subscription_changed: Optional[SubscriptionCallback] = None,
    ) -> None:
        self.adapter_path = self._adapter_to_path(adapter)
        self.device_name = self._validate_device_name(device_name)
        self._subscription_callback = on_subscription_changed

        self.bus: Optional[MessageBus] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._gatt_manager = None
        self._advertising_manager = None
        self._application_registered = False
        self._advertisement_registered = False
        self._subscriber_event = asyncio.Event()

        self.service = GattService()
        self.characteristic = ControlCharacteristic(self._notify_state_changed)
        self.application = Application(self.service, self.characteristic)
        self.advertisement = Advertisement(self.device_name)

    @staticmethod
    def _adapter_to_path(adapter: str) -> str:
        if not isinstance(adapter, str):
            raise TypeError("adapter must be a string")
        adapter = adapter.strip()
        if adapter.startswith("/"):
            if not adapter.startswith("/org/bluez/"):
                raise ValueError("adapter object path must be below /org/bluez")
            return adapter.rstrip("/")
        if not adapter or "/" in adapter:
            raise ValueError("adapter must be hciN or a BlueZ adapter object path")
        return f"/org/bluez/{adapter}"

    @staticmethod
    def _validate_device_name(device_name: str) -> str:
        if not isinstance(device_name, str):
            raise TypeError("device_name must be a string")
        device_name = device_name.strip()
        if not device_name:
            raise ValueError("device_name must not be empty")
        if "\x00" in device_name:
            raise ValueError("device_name must not contain NUL")
        return device_name

    @property
    def running(self) -> bool:
        return self.bus is not None and self._application_registered

    @property
    def notifying(self) -> bool:
        return self.characteristic.notifying

    def _notify_state_changed(self, notifying: bool) -> None:
        if notifying:
            self._subscriber_event.set()
        else:
            self._subscriber_event.clear()

        if self._subscription_callback is not None:
            try:
                self._subscription_callback(notifying)
            except Exception:
                LOGGER.exception("application subscription callback failed")

    async def start(self) -> None:
        """Export and register the GATT service and LE advertisement."""

        if self.bus is not None:
            return

        self._loop = asyncio.get_running_loop()
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.export(APP_PATH, self.application)
        self.bus.export(SERVICE_PATH, self.service)
        self.bus.export(CHARACTERISTIC_PATH, self.characteristic)
        self.bus.export(ADVERTISEMENT_PATH, self.advertisement)

        try:
            introspection = await self.bus.introspect(
                BLUEZ_SERVICE, self.adapter_path
            )
            adapter_object = self.bus.get_proxy_object(
                BLUEZ_SERVICE, self.adapter_path, introspection
            )
            self._gatt_manager = adapter_object.get_interface(GATT_MANAGER_IFACE)
            self._advertising_manager = adapter_object.get_interface(
                ADVERTISING_MANAGER_IFACE
            )

            await self._gatt_manager.call_register_application(APP_PATH, {})
            self._application_registered = True
            await self._advertising_manager.call_register_advertisement(
                ADVERTISEMENT_PATH, {}
            )
            self._advertisement_registered = True
        except Exception:
            await self.stop()
            raise

        LOGGER.info("GATT server registered on %s", self.adapter_path)
        LOGGER.info("advertising as %s", self.device_name)
        LOGGER.info("service UUID: %s", SERVICE_UUID)
        LOGGER.info("control characteristic UUID: %s", CHARACTERISTIC_UUID)

    async def wait_until_subscribed(self, timeout: Optional[float] = None) -> bool:
        """Wait for ``StartNotify``; return ``False`` if timeout expires."""

        if self.notifying:
            return True
        if not self.running:
            raise RuntimeError("the GATT server is not running")

        try:
            if timeout is None:
                await self._subscriber_event.wait()
            else:
                await asyncio.wait_for(self._subscriber_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.notifying

    def send_command(self, intensity: int, repeat_count: int) -> bool:
        """Send one command from the server's asyncio event-loop thread."""

        packet = pack_command(intensity, repeat_count)
        if not self.running or not self.notifying:
            return False

        self.characteristic.update_value(packet)
        LOGGER.info(
            "command sent: intensity=%d repeats=%d packet=%s",
            intensity,
            repeat_count,
            packet.hex(" "),
        )
        return True

    def send_vibration(self, intensity: int, repeat_count: int) -> bool:
        """Business-friendly alias of :meth:`send_command`."""

        return self.send_command(intensity, repeat_count)

    def stop_vibration(self) -> bool:
        """Immediately stop the current wristband action."""

        return self.send_command(0, 0)

    async def send_when_subscribed(
        self,
        intensity: int,
        repeat_count: int,
        timeout: Optional[float] = None,
    ) -> bool:
        """Wait for a subscription and then send, useful during startup."""

        # Validate immediately instead of waiting before reporting bad input.
        pack_command(intensity, repeat_count)
        if not await self.wait_until_subscribed(timeout):
            return False
        return self.send_command(intensity, repeat_count)

    def send_command_threadsafe(
        self, intensity: int, repeat_count: int
    ) -> concurrent.futures.Future[bool]:
        """Schedule a command safely from a non-asyncio worker thread."""

        loop = self._loop
        if loop is None or loop.is_closed() or not self.running:
            raise RuntimeError("the GATT server is not running")

        result: concurrent.futures.Future[bool] = concurrent.futures.Future()

        def send() -> None:
            if result.cancelled():
                return
            try:
                result.set_result(self.send_command(intensity, repeat_count))
            except Exception as exc:
                result.set_exception(exc)

        loop.call_soon_threadsafe(send)
        return result

    async def stop(self) -> None:
        """Unregister all BlueZ objects and disconnect from system D-Bus."""

        bus = self.bus
        if bus is None:
            self._loop = None
            return

        try:
            if self._advertisement_registered and self._advertising_manager:
                await self._advertising_manager.call_unregister_advertisement(
                    ADVERTISEMENT_PATH
                )
        except Exception:
            LOGGER.debug("could not unregister advertisement", exc_info=True)
        finally:
            self._advertisement_registered = False

        try:
            if self._application_registered and self._gatt_manager:
                await self._gatt_manager.call_unregister_application(APP_PATH)
        except Exception:
            LOGGER.debug("could not unregister GATT application", exc_info=True)
        finally:
            self._application_registered = False

        self.characteristic.reset_notify_state()
        for path in (
            ADVERTISEMENT_PATH,
            CHARACTERISTIC_PATH,
            SERVICE_PATH,
            APP_PATH,
        ):
            try:
                bus.unexport(path)
            except Exception:
                LOGGER.debug("could not unexport %s", path, exc_info=True)

        bus.disconnect()
        self.bus = None
        self._loop = None
        self._gatt_manager = None
        self._advertising_manager = None
        LOGGER.info("GATT server stopped")


CONSOLE_HELP = """Commands:
  send <intensity> <repeats>  send a vibration command, e.g. send 80 3
  <intensity> <repeats>       shorthand for send
  stop                        send the immediate-stop command
  status                      show server/subscription status
  help                        show this help
  quit                        stop the server
"""


def _handle_console_line(
    line: str, server: HandGattServer, stopped: asyncio.Event
) -> None:
    """Parse one interactive command without blocking the event loop."""

    fields = line.strip().split()
    if not fields:
        return

    command = fields[0].lower()
    if command in {"quit", "exit"}:
        stopped.set()
        return
    if command in {"help", "?"}:
        print(CONSOLE_HELP, end="")
        return
    if command == "status":
        print(
            f"running={server.running}, wristband_subscribed={server.notifying}"
        )
        return
    if command == "stop":
        if server.stop_vibration():
            print("stop command sent")
        else:
            print("not sent: the wristband has not subscribed yet")
        return

    if command == "send":
        fields = fields[1:]
    if len(fields) != 2:
        print("invalid command; enter 'help' for usage")
        return

    try:
        intensity = int(fields[0], 10)
        repeat_count = int(fields[1], 10)
        sent = server.send_vibration(intensity, repeat_count)
    except (TypeError, ValueError) as exc:
        print(f"invalid command: {exc}")
        return

    if sent:
        print(f"sent: intensity={intensity}, repeats={repeat_count}")
    else:
        print("not sent: the wristband has not subscribed yet")


async def run_server(
    adapter: str,
    device_name: str,
    *,
    interactive: bool,
) -> None:
    """Run until SIGINT/SIGTERM or the interactive ``quit`` command."""

    server = HandGattServer(adapter=adapter, device_name=device_name)
    await server.start()

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
        except NotImplementedError:
            pass

    console_installed = False
    if interactive:
        print(CONSOLE_HELP, end="")
        print("Waiting for the wristband to connect and subscribe...")

        def on_stdin_ready() -> None:
            line = sys.stdin.readline()
            if line == "":
                stopped.set()
                return
            _handle_console_line(line, server, stopped)

        try:
            loop.add_reader(sys.stdin.fileno(), on_stdin_ready)
            console_installed = True
        except (AttributeError, NotImplementedError, OSError):
            LOGGER.warning("interactive stdin is unavailable; running as a daemon")

    LOGGER.info("server is running; press Ctrl+C to stop")
    try:
        await stopped.wait()
    finally:
        if console_installed:
            loop.remove_reader(sys.stdin.fileno())
        await server.stop()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adapter",
        default="hci0",
        help="BlueZ adapter name or object path (default: hci0)",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_DEVICE_NAME,
        dest="device_name",
        help=f"advertised BLE name (default: {DEFAULT_DEVICE_NAME})",
    )
    parser.add_argument(
        "--no-console",
        action="store_true",
        help="disable the interactive console (for systemd/embedded use)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="enable debug logging"
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    interactive = not args.no_console and sys.stdin.isatty()
    try:
        asyncio.run(
            run_server(
                args.adapter,
                args.device_name,
                interactive=interactive,
            )
        )
    except KeyboardInterrupt:
        return 130
    except (DBusError, OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("could not run BLE server: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
