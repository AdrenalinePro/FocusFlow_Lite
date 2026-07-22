"""FocusFlow BLE Windows client package.

The package deliberately keeps the BLE implementation independent from the
application GUI.  Import :class:`WindowsBLEClient` for asyncio applications,
or :class:`WindowsBLEClientThread` for PyQt5 applications.
"""

from .windows_ble_protocol import (
    DISCOVERY_SERVICE_UUID,
    MAX_JSON_BYTES,
    RX_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    TX_CHARACTERISTIC_UUID,
    ProtocolError,
    decode_message,
    encode_downlink,
    encode_message,
)
from .windows_ble_client import (
    BleClientConfig,
    BleConnectionState,
    NotConnectedError,
    WindowsBLEClient,
)

__all__ = [
    "MAX_JSON_BYTES",
    "DISCOVERY_SERVICE_UUID",
    "RX_CHARACTERISTIC_UUID",
    "SERVICE_UUID",
    "TX_CHARACTERISTIC_UUID",
    "ProtocolError",
    "decode_message",
    "encode_downlink",
    "encode_message",
    "BleClientConfig",
    "BleConnectionState",
    "NotConnectedError",
    "WindowsBLEClient",
]

# PyQt5 is optional for command-line/asyncio users.
try:
    from .windows_ble_qt import WindowsBLEClientThread
except ImportError:  # pragma: no cover - depends on the local GUI install
    WindowsBLEClientThread = None
else:
    __all__.append("WindowsBLEClientThread")
