"""FocusFlow BLE Linux server package.

The Linux side acts as the BLE GATT Server. It exposes the FocusFlow
service defined in ``FocusFlow_BLE_Protocol.md`` and exchanges JSON-over-
GATT messages with the Windows client. Import :class:`LinuxBLEServer` to
embed the server in an asyncio application, or run :mod:`linux_ble_test`
to validate the link from the command line.
"""

from .linux_ble_protocol import (
    DOWNLINK_TYPES,
    MAX_JSON_BYTES,
    RX_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    TX_CHARACTERISTIC_UUID,
    UPLINK_TYPES,
    BLEMessage,
    ProtocolError,
    decode_message,
    encode_message,
)
from .linux_ble_server import (
    BleServerConfig,
    BleServerState,
    LinuxBLEServer,
)

__all__ = [
    "DOWNLINK_TYPES",
    "MAX_JSON_BYTES",
    "RX_CHARACTERISTIC_UUID",
    "SERVICE_UUID",
    "TX_CHARACTERISTIC_UUID",
    "UPLINK_TYPES",
    "BLEMessage",
    "ProtocolError",
    "decode_message",
    "encode_message",
    "BleServerConfig",
    "BleServerState",
    "LinuxBLEServer",
]
