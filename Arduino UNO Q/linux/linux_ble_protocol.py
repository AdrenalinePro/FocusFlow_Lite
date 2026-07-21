"""FocusFlow BLE wire-format helpers for the UNO Q Linux side.

The protocol is symmetric: both directions use the same envelope, the
same field validation, and the same 240-byte JSON size cap.  This module
re-exports the helpers from :mod:`windows.windows_ble_protocol` so the
Linux side keeps a single source of truth with the Windows client.  The
GATT UUIDs and protocol constants are also re-exported so the rest of
the Linux package only needs to import from ``linux.linux_ble_protocol``.
"""

from __future__ import annotations

import os
import sys

# Allow ``from windows.windows_ble_protocol import ...`` to resolve when
# the package is used as ``python -m linux.linux_ble_test`` or directly
# with ``python linux_ble_test.py``.  The repository root is the parent
# of both the ``windows/`` and ``linux/`` folders.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from windows.windows_ble_protocol import (  # noqa: E402
    BLEMessage,
    DOWNLINK_TYPES,
    ERROR_CODES,
    FEEDBACK_TYPES,
    MAX_JSON_BYTES,
    REST_ACTIONS,
    REST_PHASES,
    REST_REASONS,
    RX_CHARACTERISTIC_UUID,
    SCREEN_CATEGORIES,
    SCREEN_STATES,
    SERVICE_UUID,
    STATES,
    TFT_STATES,
    TX_CHARACTERISTIC_UUID,
    UINT32_MAX,
    UPLINK_TYPES,
    ProtocolError,
    decode_message,
    encode_downlink,
    encode_message,
    validate_data,
)

# ``encode_downlink`` is a thin wrapper around ``encode_message`` with
# ``allowed_types=DOWNLINK_TYPES``.  Windows 1.0.2+ exposes it directly
# (re-exported above) so the Linux server uses a single source of truth.
encode_downlink = encode_downlink

__all__ = [
    "BLEMessage",
    "DOWNLINK_TYPES",
    "ERROR_CODES",
    "FEEDBACK_TYPES",
    "MAX_JSON_BYTES",
    "REST_ACTIONS",
    "REST_PHASES",
    "REST_REASONS",
    "RX_CHARACTERISTIC_UUID",
    "SCREEN_CATEGORIES",
    "SCREEN_STATES",
    "SERVICE_UUID",
    "STATES",
    "TFT_STATES",
    "TX_CHARACTERISTIC_UUID",
    "UINT32_MAX",
    "UPLINK_TYPES",
    "ProtocolError",
    "decode_message",
    "encode_downlink",
    "encode_message",
    "validate_data",
]
