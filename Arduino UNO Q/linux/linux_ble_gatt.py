"""Low-level BlueZ GATT server for the FocusFlow UNO Q BLE link.

The Linux side is the GATT Server; the Windows side is the GATT Client.
BlueZ exposes the GATT Server API through the system D-Bus:

* ``org.bluez.GattManager1.RegisterApplication`` registers an entire
  application tree (services, characteristics, descriptors) under a
  single root object path.
* ``org.bluez.GattService1`` describes a service.
* ``org.bluez.GattCharacteristic1`` describes a characteristic; the
  ``WriteValue`` method receives client writes and the ``Notify`` signal
  pushes data to subscribed clients.
* ``org.freedesktop.DBus.ObjectManager.GetManagedObjects`` is the entry
  point BlueZ uses to enumerate our tree.

This module registers those interfaces against a FocusFlow service UUID
and converts GATT ``Write`` / ``Notify`` events into plain Python
callbacks, so the higher level server (:mod:`linux_ble_server`) can
focus on protocol parsing, state-machine updates and message dispatch
instead of the BlueZ D-Bus plumbing.
"""

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional

from dbus_fast import BusType, DBusError, NameFlag, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal


from dbus_fast import Message as _DBusMessage


from .linux_ble_protocol import (
    RX_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    TX_CHARACTERISTIC_UUID,
)

LOGGER = logging.getLogger(__name__)

# CCCD well-known UUID (Bluetooth SIG assigned numbers).
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# BlueZ GATT characteristic flag strings (see ``gatt-api.txt``).
FLAG_READ = "read"
FLAG_WRITE = "write"
FLAG_WRITE_WO_RESP = "write-without-response"
FLAG_NOTIFY = "notify"
FLAG_INDICATE = "indicate"

WriteHandler = Callable[[bytes], Awaitable[None]]
NotifyStateHandler = Callable[[bool], Awaitable[None]]


class _Application(ServiceInterface):
    """Root object that owns the ObjectManager interface.

    BlueZ calls :meth:`GetManagedObjects` to enumerate the application
    tree.  The dictionary returned here is rebuilt whenever the tree
    changes (e.g. when the TX Characteristic is added) so BlueZ always
    sees the latest state.
    """

    def __init__(self, owner: "BlueZGattServer") -> None:
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self._owner = owner

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        """Return the service/characteristic/descriptors tree."""
        return self._owner.build_managed_objects()

    @signal()
    def InterfacesAdded(self, object_path: "o", interfaces: "a{sa{sv}}") -> "oa{sa{sv}}":
        return object_path, interfaces

    @signal()
    def InterfacesRemoved(self, object_path: "o", interfaces: "as") -> "oas":
        return object_path, interfaces


class _FocusFlowService(ServiceInterface):
    """``org.bluez.GattService1`` implementation."""

    def __init__(self, path: str, uuid: str) -> None:
        super().__init__("org.bluez.GattService1")
        self._path = path
        self._uuid = uuid
        self._primary = True

    @property
    def path(self) -> str:
        return self._path

    def get_properties(self) -> Dict[str, Variant]:
        return {
            "UUID": Variant("s", self._uuid),
            "Primary": Variant("b", self._primary),
            "Characteristics": Variant(
                "ao",
                [
                    f"{self._path}/rx",
                    f"{self._path}/tx",
                ],
            ),
        }


class _RxCharacteristic(ServiceInterface):
    """RX Characteristic (Windows -> UNO Q, Write + WriteWithoutResponse)."""

    def __init__(
        self,
        path: str,
        uuid: str,
        handler: WriteHandler,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
    ) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self._path = path
        self._uuid = uuid
        self._handler = handler
        self._loop = loop
        self._logger = logger

    @property
    def path(self) -> str:
        return self._path

    def get_properties(self) -> Dict[str, Variant]:
        return {
            "UUID": Variant("s", self._uuid),
            "Service": Variant("o", self._path.rsplit("/", 1)[0]),
            "Value": Variant("ay", b""),
            "Notifying": Variant("b", False),
            "Flags": Variant("as", [FLAG_WRITE, FLAG_WRITE_WO_RESP]),
            "WriteAcquired": Variant("b", False),
            "NotifyAcquired": Variant("b", False),
        }

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}") -> None:
        # GATT writes arrive on the D-Bus thread.  Hand the bytes to the
        # asyncio loop so the user's callback runs in the configured
        # event loop without blocking the bus.
        if not isinstance(value, (bytes, bytearray, memoryview)):
            self._logger.warning("RX write with unexpected payload type: %s", type(value))
            return
        payload = bytes(value)
        self._logger.debug("RX WriteValue: %d bytes", len(payload))
        try:
            asyncio.run_coroutine_threadsafe(self._handler(payload), self._loop)
        except RuntimeError:
            # Loop is closed; nothing useful we can do here.
            self._logger.warning("RX write dropped because the asyncio loop is closed")

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        # RX is write-only; still satisfy BlueZ by returning an empty
        # buffer rather than raising (some clients probe with Read).
        return b""

    @method()
    def StartNotify(self) -> None:  # pragma: no cover - not on RX
        raise DBusError("org.bluez.Error.NotSupported",
                        "RX characteristic does not support Notify")

    @method()
    def StopNotify(self) -> None:  # pragma: no cover - not on RX
        raise DBusError("org.bluez.Error.NotSupported",
                        "RX characteristic does not support Notify")


class _TxCharacteristic(ServiceInterface):
    """TX Characteristic (UNO Q -> Windows, Notify + Read).

    BlueZ exposes the Client Characteristic Configuration descriptor
    (CCCD) at the well-known SIG UUID; the ``StartNotify`` / ``StopNotify``
    methods are invoked when the client writes the CCCD.  We forward
    those events to the high-level server so it can track whether the
    Windows side is currently subscribed.
    """

    def __init__(
        self,
        path: str,
        uuid: str,
        on_state: NotifyStateHandler,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
    ) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self._path = path
        self._uuid = uuid
        self._on_state = on_state
        self._loop = loop
        self._logger = logger
        self._notifying = False

    @property
    def path(self) -> str:
        return self._path

    def get_properties(self) -> Dict[str, Variant]:
        return {
            "UUID": Variant("s", self._uuid),
            "Service": Variant("o", self._path.rsplit("/", 1)[0]),
            "Value": Variant("ay", b""),
            "Notifying": Variant("b", self._notifying),
            "Flags": Variant("as", [FLAG_NOTIFY, FLAG_READ]),
            "WriteAcquired": Variant("b", False),
            "NotifyAcquired": Variant("b", True),
            "CCCD": Variant(
                "s",
                f"{self._path}/cccd",
            ),
        }

    @method()
    def StartNotify(self) -> None:
        if self._notifying:
            return
        self._notifying = True
        self._logger.debug("TX StartNotify")
        asyncio.run_coroutine_threadsafe(self._on_state(True), self._loop)

    @method()
    def StopNotify(self) -> None:
        if not self._notifying:
            return
        self._notifying = False
        self._logger.debug("TX StopNotify")
        asyncio.run_coroutine_threadsafe(self._on_state(False), self._loop)

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return b""

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}") -> None:  # pragma: no cover - not on TX
        raise DBusError("org.bluez.Error.NotSupported",
                        "TX characteristic does not support Write")

    @signal()
    def Notify(self, value: "ay") -> "ay":
        # ``Notify`` is a signal; the return type is the OUT signature
        # for dbus-fast.  The actual payload is supplied by ``emit``.
        return value

    @signal()
    def AcquireNotify(self) -> None:  # pragma: no cover - optional interface
        return None


class _CCCDDescriptor(ServiceInterface):
    """Minimal ``org.bluez.GattDescriptor1`` for the TX CCCD.

    Windows writes ``[0x01, 0x00]`` here to enable Notify and
    ``[0x00, 0x00]`` to disable.  BlueZ uses those writes to drive
    ``StartNotify`` / ``StopNotify`` on the characteristic, but it also
    requires the descriptor object to be exposed via ObjectManager.
    """

    def __init__(self, path: str, characteristic_path: str) -> None:
        super().__init__("org.bluez.GattDescriptor1")
        self._path = path
        self._characteristic_path = characteristic_path

    @property
    def path(self) -> str:
        return self._path

    def get_properties(self) -> Dict[str, Variant]:
        return {
            "UUID": Variant("s", CCCD_UUID),
            "Characteristic": Variant("o", self._characteristic_path),
            "Value": Variant("ay", b"\x00\x00"),
            "Flags": Variant("as", ["read", "write"]),
        }

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return b"\x00\x00"

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}") -> None:
        # BlueZ dispatches StartNotify/StopNotify separately, so we only
        # need to accept the write for the CCCD to satisfy introspection.
        return None


class _FocusFlowAdvertisement(ServiceInterface):
    """``org.bluez.LEAdvertisement1`` implementation.

    Registering a GATT application alone does not make the device
    discoverable — BlueZ only sends the GAP advertisement frames that
    ``LEAdvertisingManager1`` has registered.  This class exposes a
    peripheral-mode advertisement that includes both the local name
    (``UNO-Q-FF01``) and the FocusFlow service UUID, so passive scanners
    on Windows / phones can find us either by name or by service match.
    """

    def __init__(self, path: str, device_name: str, service_uuid: str) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self._path = path
        self._device_name = device_name
        self._service_uuid = service_uuid
        self._released = False

    @property
    def path(self) -> str:
        return self._path

    @property
    def released(self) -> bool:
        return self._released

    def get_properties(self) -> Dict[str, Variant]:
        # ``Type=peripheral`` keeps the device connectable.  We rely on
        # the patched ``GetManagedObjects`` to call this method (see
        # ``setup_dbus_fast.sh``), otherwise the standard dbus-fast
        # handler returns ``{}`` and BlueZ drops the advertisement with
        # "Failed to register advertisement".
        return {
            "Type": Variant("s", "peripheral"),
            "ServiceUUIDs": Variant("as", [self._service_uuid]),
            "LocalName": Variant("s", self._device_name),
            "IncludeTxPower": Variant("b", True),
            "Discoverable": Variant("b", True),
        }

    @method()
    def Release(self) -> None:
        self._released = True


class BlueZGattServer:
    """BlueZ GATT server wrapper for the FocusFlow service.

    The instance owns the asyncio event loop reference (because GATT
    callbacks arrive on the D-Bus thread) and exposes a small surface
    that :class:`linux.linux_ble_server.LinuxBLEServer` can drive:

    * ``set_rx_handler`` registers the coroutine invoked for every RX
      characteristic write.
    * ``set_notify_state_handler`` registers a coroutine invoked when a
      Windows client enables or disables TX Notify.
    * ``notify`` pushes a value over the TX characteristic.
    * ``start`` registers the application and starts advertising.
    * ``stop`` tears down advertising, unregisters the application and
      closes the D-Bus connection.
    """

    APP_ROOT = "/com/focusflow/app0"
    SERVICE_PATH = APP_ROOT + "/service0"
    RX_PATH = SERVICE_PATH + "/rx"
    TX_PATH = SERVICE_PATH + "/tx"
    TX_CCCD_PATH = TX_PATH + "/cccd"
    ADVERTISEMENT_PATH = APP_ROOT + "/adv0"
    ADAPTER_ROOT = "/org/bluez/hci0"

    def __init__(
        self,
        *,
        device_name: str = "UNO-Q-FF01",
        adapter: str = ADAPTER_ROOT,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.device_name = device_name
        self.adapter_path = adapter
        self.logger = logger or LOGGER

        self._bus: Optional[MessageBus] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._app = _Application(self)
        self._service = _FocusFlowService(self.SERVICE_PATH, SERVICE_UUID)
        self._rx: Optional[_RxCharacteristic] = None
        self._tx: Optional[_TxCharacteristic] = None
        self._cccd: Optional[_CCCDDescriptor] = None
        self._advertisement: Optional[_FocusFlowAdvertisement] = None
        self._registered = False
        self._advertising_registered = False

    # ---- handler wiring -------------------------------------------------
    def set_rx_handler(self, handler: WriteHandler) -> None:
        if self._rx is None:
            raise RuntimeError("Server has not started yet")
        self._rx._handler = handler  # noqa: SLF001 - intentional rebind

    def set_notify_state_handler(self, handler: NotifyStateHandler) -> None:
        if self._tx is None:
            raise RuntimeError("Server has not started yet")
        self._tx._on_state = handler  # noqa: SLF001 - intentional rebind

    # ---- introspection --------------------------------------------------
    def build_managed_objects(self) -> Dict[str, Dict[str, Dict[str, Variant]]]:
        """Return the service tree for ``GetManagedObjects``."""

        tree: Dict[str, Dict[str, Dict[str, Variant]]] = {
            self.APP_ROOT: {
                "org.freedesktop.DBus.ObjectManager": {},
            },
            self.SERVICE_PATH: {
                "org.bluez.GattService1": self._service.get_properties(),
            },
        }
        if self._rx is not None:
            tree[self._rx.path] = {
                "org.bluez.GattCharacteristic1": self._rx.get_properties(),
            }
        if self._tx is not None:
            tree[self._tx.path] = {
                "org.bluez.GattCharacteristic1": self._tx.get_properties(),
            }
        if self._cccd is not None:
            tree[self._cccd.path] = {
                "org.bluez.GattDescriptor1": self._cccd.get_properties(),
            }
        return tree

    # ---- advertising helpers -------------------------------------------
    async def _set_advertising(self, enable: bool) -> None:
        """Toggle LE advertisement + adapter state for the FocusFlow service.

        Registering the GATT application alone is *not* enough to make
        the device discoverable — BlueZ only transmits advertisement
        frames for entries that have been pushed through
        ``LEAdvertisingManager1``.  We therefore:

        1. Force ``Powered=True`` on the adapter.
        2. Set the adapter ``Alias`` to ``device_name`` so the generic
           discoverable advertisement already in flight carries the
           expected local name.
        3. Set ``Discoverable=True`` and ``Pairable=True`` so the
           adapter is willing to accept an incoming connection without
           the operator having to run ``bluetoothctl discoverable on``
           by hand.
        4. Register our own ``LEAdvertisement1`` carrying the FocusFlow
           service UUID, the local name and a connectable peripheral
           type.  Step 4 is what makes passive scanners on Windows see
           the device under ``UNO-Q-FF01``.
        """

        if self._bus is None:
            return
        # Force ``Powered=True`` and the right alias.  This needs the
        # adapter path (``/org/bluez/hciN``), not the root ``/``; the
        # previous version introspected ``/`` which only exposes the
        # ObjectManager + Introspectable interfaces and broke here.
        try:
            introspection = await self._bus.introspect("org.bluez", self.adapter_path)
            adapter_props = self._bus.get_proxy_object(
                "org.bluez", self.adapter_path, introspection,
            ).get_interface("org.freedesktop.DBus.Properties")
            powered = await adapter_props.call_get("org.bluez.Adapter1", "Powered")
            if not powered.value:
                await adapter_props.call_set(
                    "org.bluez.Adapter1", "Powered", Variant("b", True),
                )
            alias_prop = await adapter_props.call_get("org.bluez.Adapter1", "Alias")
            if alias_prop.value != self.device_name:
                await adapter_props.call_set(
                    "org.bluez.Adapter1", "Alias", Variant("s", self.device_name),
                )
            # Make sure the controller is in a state that allows
            # incoming connections even if the operator forgot to run
            # ``bluetoothctl discoverable on`` themselves.
            for prop, target in (("Discoverable", True), ("Pairable", True)):
                current = await adapter_props.call_get("org.bluez.Adapter1", prop)
                if current.value != target:
                    try:
                        await adapter_props.call_set(
                            "org.bluez.Adapter1", prop, Variant("b", target),
                        )
                    except DBusError as exc:
                        # Discoverable/Pairable can be rejected when
                        # the agent isn't running; we still keep going
                        # because the LEAdvertisement below is the real
                        # signal for passive scanners.
                        self.logger.warning(
                            "Could not set %s=%s on %s: %s",
                            prop, target, self.adapter_path, exc,
                        )
            self.logger.debug(
                "Adapter ready: alias=%s powered=Discoverable=Pairable=true",
                self.device_name,
            )
        except DBusError as exc:
            self.logger.warning("Adapter setup failed (continuing): %s", exc)

        if not enable:
            await self._unregister_advertisement()
            return

        # Step 4: register our own LEAdvertisement so passive scanners
        # see ``UNO-Q-FF01`` + the FocusFlow service UUID.
        if self._advertisement is None:
            self._advertisement = _FocusFlowAdvertisement(
                self.ADVERTISEMENT_PATH, self.device_name, SERVICE_UUID,
            )
            self._bus.export(self.ADVERTISEMENT_PATH, self._advertisement)
        try:
            introspection = await self._bus.introspect("org.bluez", self.adapter_path)
            adv_manager = self._bus.get_proxy_object(
                "org.bluez", self.adapter_path, introspection,
            ).get_interface("org.bluez.LEAdvertisingManager1")
            await adv_manager.call_register_advertisement(
                self.ADVERTISEMENT_PATH, {},
            )
            self._advertising_registered = True
            self.logger.info(
                "LEAdvertisement registered on %s (LocalName=%r, ServiceUUIDs=[%s])",
                self.adapter_path, self.device_name, SERVICE_UUID,
            )
        except DBusError as exc:
            self._advertising_registered = False
            self.logger.error(
                "RegisterAdvertisement(%s) failed: %s.  The GATT service "
                "is registered but Windows clients will not see this "
                "device in passive scans until an advertisement is up. "
                "Common causes: (1) another process is already holding "
                "an advertisement on this adapter; (2) the controller "
                "is in BR/EDR-only mode (set ControllerMode = le in "
                "/etc/bluetooth/main.conf); (3) the dbus-fast patch "
                "from setup_dbus_fast.sh is not applied.",
                self.ADVERTISEMENT_PATH, exc,
            )

    async def _unregister_advertisement(self) -> None:
        if not self._advertising_registered or self._bus is None:
            return
        try:
            introspection = await self._bus.introspect("org.bluez", self.adapter_path)
            adv_manager = self._bus.get_proxy_object(
                "org.bluez", self.adapter_path, introspection,
            ).get_interface("org.bluez.LEAdvertisingManager1")
            await adv_manager.call_unregister_advertisement(self.ADVERTISEMENT_PATH)
            self.logger.debug("LEAdvertisement unregistered")
        except DBusError as exc:
            self.logger.debug("UnregisterAdvertisement failed (ignored): %s", exc)
        finally:
            self._advertising_registered = False
        if self._advertisement is not None:
            try:
                self._bus.unexport(self.ADVERTISEMENT_PATH)
            except Exception:  # pragma: no cover - already unexported
                pass
            self._advertisement = None

    # ---- lifecycle ------------------------------------------------------
    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Connect to the system bus, export the GATT tree and register.

        All D-Bus errors are re-raised unchanged so ``LinuxBLEServer.run``
        can show them verbatim.  The most common failures are:

        * ``OSError: [Errno 1] Operation not permitted`` —
          ``bluetoothd`` not running, or the current user lacks
          ``CAP_NET_ADMIN`` to talk to it.
        * ``org.bluez.Error.NotReady`` — adapter exists but isn't
          powered / discoverable.
        * ``org.bluez.Error.InvalidArguments`` / ``org.freedesktop.DBus.Error.InvalidArgs`` —
          the application tree is malformed; check that
          ``adapter_path`` points to a real adapter.
        * ``org.bluez.Error.Failed`` — ``RegisterApplication`` was
          rejected, often because BlueZ already has another application
          bound to the same UUID set on the same adapter.
        """

        if self._bus is not None:
            raise RuntimeError("Server already started")
        self._loop = loop
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:
            self.logger.error(
                "Failed to connect to system D-Bus: %s: %s.  bluetoothd "
                "is reachable via the system bus; make sure it is "
                "running (sudo systemctl status bluetooth) and the "
                "current user can talk to it (typically needs "
                "CAP_NET_ADMIN, e.g. via sudo).",
                type(exc).__name__, exc,
            )
            raise

        # We deliberately do NOT probe the adapter here.  BlueZ
        # manages ``Powered`` / ``Discoverable`` / ``Alias`` itself
        # and the user is expected to have configured those before
        # running this script (``bluetoothctl power on`` etc.).  A
        # previous version of this code introspected ``/`` and tried to
        # use ``org.freedesktop.DBus.Properties`` on the adapter path,
        # which fails because (a) the introspection returned for ``/``
        # does not include the adapter's interfaces, and (b)
        # ``Properties`` is an auto-interface added by dbus-daemon and
        # not exposed via introspection.  If the adapter is missing or
        # not powered the next ``RegisterApplication`` call will fail
        # with a clear BlueZ error.

        # Acquire the well-known bus name so BlueZ (and any other
        # system-bus client) can resolve ``com.focusflow`` to *our*
        # connection.  Without this, ``bus.export()`` only adds the
        # paths to an internal map on our private connection, but the
        # system bus daemon has no way to route inbound calls because
        # no client owns the name.  RegisterApplication would then
        # fail with "No valid service object found".  This requires a
        # matching policy in ``/etc/dbus-1/system.d/com.focusflow.conf``
        # granting the current user / group permission to own
        # ``com.focusflow`` -- see the dev_session policy install
        # command in the README.
        try:
            name_reply = await self._bus.request_name(
                "com.focusflow", NameFlag.DO_NOT_QUEUE,
            )
            self.logger.info(
                "Acquired system bus name 'com.focusflow' (reply=%d)",
                name_reply,
            )
        except DBusError as exc:
            self.logger.error(
                "Cannot own bus name 'com.focusflow': %s.  "
                "Install /etc/dbus-1/system.d/com.focusflow.conf "
                "and reload dbus (systemctl reload dbus).",
                exc,
            )
            raise

        # The characteristic / service / descriptor objects can only
        # be created after we know the bus is up.
        self._rx = _RxCharacteristic(
            self.RX_PATH, RX_CHARACTERISTIC_UUID,
            handler=self._default_rx_handler, loop=loop, logger=self.logger,
        )
        self._tx = _TxCharacteristic(
            self.TX_PATH, TX_CHARACTERISTIC_UUID,
            on_state=self._default_notify_state, loop=loop, logger=self.logger,
        )
        self._cccd = _CCCDDescriptor(self.TX_CCCD_PATH, self.TX_PATH)

        self._bus.export(self.APP_ROOT, self._app)
        self._bus.export(self.SERVICE_PATH, self._service)
        self._bus.export(self.RX_PATH, self._rx)
        self._bus.export(self.TX_PATH, self._tx)
        self._bus.export(self.TX_CCCD_PATH, self._cccd)

        # Register the application via org.bluez.GattManager1.  In
        # BlueZ 5.x this interface lives on the ADAPTER object (e.g.
        # /org/bluez/hci0), not on /org/bluez.  Earlier versions of
        # this code introspected ``/`` which only exposes
        # Introspectable + ObjectManager, so ``get_interface("org.bluez.GattManager1")``
        # raised ``InterfaceNotFoundError`` and the GATT application
        # never got registered.  Run
        # ``dbus-send --system --dest=org.bluez --print-reply /
        #   org.freedesktop.DBus.ObjectManager.GetManagedObjects``
        # to confirm the adapter path on this system.
        try:
            adapter_introspection = await self._bus.introspect(
                "org.bluez", self.adapter_path,
            )
        except DBusError as exc:
            self.logger.error(
                "Cannot introspect adapter %s: %s.  Is bluetoothd running "
                "and is the adapter path correct?  Use "
                "--adapter=/org/bluez/hciN or pass --scan-only to list.",
                self.adapter_path, exc,
            )
            raise
        manager = self._bus.get_proxy_object(
            "org.bluez", self.adapter_path, adapter_introspection
        ).get_interface("org.bluez.GattManager1")
        options: Dict[str, Variant] = {}
        try:
            await manager.call_register_application(self.APP_ROOT, options)
            self._registered = True
            self.logger.info(
                "Registered FocusFlow GATT application at %s on %s",
                self.APP_ROOT, self.adapter_path,
            )
        except DBusError as exc:
            self.logger.error(
                "RegisterApplication(%s) failed: %s.  Common causes: "
                "(1) BlueZ already has another GATT application bound to "
                "the FocusFlow UUIDs - stop the other process and retry; "
                "(2) the controller is not advertising-capable; "
                "(3) the user lacks CAP_NET_ADMIN.",
                self.APP_ROOT, exc,
            )
            raise

        await self._set_advertising(True)

    async def stop(self) -> None:
        if self._bus is None:
            return
        if self._registered:
            try:
                introspection = await self._bus.introspect("org.bluez", "/")
                manager = self._bus.get_proxy_object(
                    "org.bluez", "/org/bluez", introspection
                ).get_interface("org.bluez.GattManager1")
                await manager.call_unregister_application(self.APP_ROOT)
            except DBusError as exc:
                self.logger.debug("Unregister failed (ignored): %s", exc)
            self._registered = False

        await self._set_advertising(False)
        for path in (self.TX_CCCD_PATH, self.TX_PATH, self.RX_PATH,
                     self.SERVICE_PATH, self.APP_ROOT):
            try:
                self._bus.unexport(path)
            except Exception:  # pragma: no cover - already unexported
                pass
        self._bus.disconnect()
        self._bus = None

    # ---- TX notify ------------------------------------------------------
    async def notify(self, value: bytes) -> bool:
        """Push a TX notification to subscribed clients.

        Returns False when no client is currently subscribed (the Windows
        side has not yet enabled Notify).  Calling code may still call
        this — the high-level server logs and skips — but knowing the
        subscription state is useful for tests.
        """

        if self._tx is None or self._bus is None:
            return False
        if not self._tx._notifying:  # noqa: SLF001
            return False
        try:
            self._tx.Notify(bytes(value))
            return True
        except Exception as exc:
            self.logger.warning("TX Notify emit failed: %s", exc)
            return False

    # ---- default callbacks (overridden by set_*_handler) ----------------
    async def _default_rx_handler(self, payload: bytes) -> None:  # pragma: no cover - safety net
        self.logger.debug(
            "RX bytes arrived before the high-level handler was installed: %r",
            payload,
        )

    async def _default_notify_state(self, enabled: bool) -> None:  # pragma: no cover - safety net
        self.logger.debug("TX Notify state -> %s (no handler installed)", enabled)
