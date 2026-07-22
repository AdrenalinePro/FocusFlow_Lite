"""FocusFlow BLE v1.0 wire-format helpers.

The UNO Q service carries one compact UTF-8 JSON object per GATT write or
notification.  The protocol limits the JSON body to 240 bytes.  This keeps a
small margin inside the 244-byte ATT characteristic value available after a
247-byte ATT MTU negotiation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Set

SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214"
DISCOVERY_SERVICE_UUID = "7b3a0001-6a4f-4d91-9c10-123456789000"
RX_CHARACTERISTIC_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
TX_CHARACTERISTIC_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"
MAX_JSON_BYTES = 240
UINT32_MAX = 2**32 - 1

UPLINK_TYPES: Set[str] = {
    "eye_data",
    "screen_data",
    "rest_command",
    "heartbeat",
    "sync_request",
}
DOWNLINK_TYPES: Set[str] = {
    "state_update",
    "focus_score",
    "rest_countdown",
    "display_content",
    "device_status",
    "vibration_feedback",
    "heartbeat",
    "sync_response",
    "error",
}

STATES = {"focused", "distracted", "procrastinating", "resting"}
SCREEN_STATES = {"focused", "distracted", "procrastinating", "away"}
SCREEN_CATEGORIES = {"work", "study", "entertainment", "social", "game", "other"}
REST_ACTIONS = {"start", "stop", "extend", "query"}
REST_REASONS = {"manual", "auto_pomodoro", "auto_focus", "auto_long_session"}
FEEDBACK_TYPES = {
    "none",
    "vibrate_short",
    "vibrate_double",
    "vibrate_continuous",
    "notification",
    "tft_alert",
}
REST_PHASES = {"start", "middle", "ending"}
TFT_STATES = {"running", "error", "updating", "offline"}
ERROR_CODES = {
    "INVALID_JSON",
    "INVALID_MSG_TYPE",
    "MISSING_FIELD",
    "OUT_OF_RANGE",
    "STATE_CONFLICT",
    "DEVICE_BUSY",
    "INTERNAL_ERROR",
}


class ProtocolError(ValueError):
    """A malformed or semantically invalid protocol message."""

    def __init__(self, message: str, code: str = "INVALID_JSON") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BLEMessage:
    """Validated application message received from the UNO Q."""

    type: str
    seq: int
    ts: int
    data: Dict[str, Any]


def _require(condition: bool, message: str, code: str = "INVALID_JSON") -> None:
    if not condition:
        raise ProtocolError(message, code)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _number(data: Mapping[str, Any], key: str, minimum: Optional[float] = None,
            maximum: Optional[float] = None) -> None:
    _require(key in data, "missing data.%s" % key, "MISSING_FIELD")
    value = data[key]
    _require(_is_number(value), "data.%s must be a number" % key, "INVALID_JSON")
    if minimum is not None:
        _require(value >= minimum, "data.%s is below range" % key, "OUT_OF_RANGE")
    if maximum is not None:
        _require(value <= maximum, "data.%s is above range" % key, "OUT_OF_RANGE")


def _integer(data: Mapping[str, Any], key: str, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> None:
    _require(key in data, "missing data.%s" % key, "MISSING_FIELD")
    value = data[key]
    _require(isinstance(value, int) and not isinstance(value, bool),
             "data.%s must be an integer" % key, "INVALID_JSON")
    if minimum is not None:
        _require(value >= minimum, "data.%s is below range" % key, "OUT_OF_RANGE")
    if maximum is not None:
        _require(value <= maximum, "data.%s is above range" % key, "OUT_OF_RANGE")


def _string(data: Mapping[str, Any], key: str, allowed: Optional[Set[str]] = None,
            required: bool = True) -> None:
    if key not in data:
        _require(not required, "missing data.%s" % key, "MISSING_FIELD")
        return
    _require(isinstance(data[key], str), "data.%s must be a string" % key, "INVALID_JSON")
    if allowed is not None:
        _require(data[key] in allowed, "invalid data.%s: %s" % (key, data[key]), "OUT_OF_RANGE")


def _boolean(data: Mapping[str, Any], key: str, required: bool = True) -> None:
    if key not in data:
        _require(not required, "missing data.%s" % key, "MISSING_FIELD")
        return
    _require(isinstance(data[key], bool), "data.%s must be boolean" % key, "INVALID_JSON")


def _validate_device_status(data: Mapping[str, Any], required: bool = True) -> None:
    """Validate a full status or the compact status nested in sync_response."""

    known = {
        "eeg_connected", "eeg_battery", "wristband_connected",
        "wristband_battery", "tft_display",
    }
    if required:
        _boolean(data, "eeg_connected")
        _integer(data, "eeg_battery", -1, 100)
        _boolean(data, "wristband_connected")
        _integer(data, "wristband_battery", -1, 100)
        _string(data, "tft_display", TFT_STATES)
        return

    # sync_response is deliberately allowed to carry a compact snapshot.  A
    # complete device_status message remains strict; the next device_status
    # notification supplies battery/detail fields when they are needed.
    _require(any(key in data for key in known),
             "compact device_status must contain at least one known field", "MISSING_FIELD")
    if "eeg_connected" in data:
        _boolean(data, "eeg_connected")
    if "eeg_battery" in data:
        _integer(data, "eeg_battery", -1, 100)
    if "wristband_connected" in data:
        _boolean(data, "wristband_connected")
    if "wristband_battery" in data:
        _integer(data, "wristband_battery", -1, 100)
    if "tft_display" in data:
        _string(data, "tft_display", TFT_STATES)


def validate_data(msg_type: str, data: Mapping[str, Any]) -> None:
    """Validate fields defined by the protocol; unknown fields are ignored."""

    _require(isinstance(data, Mapping), "data must be an object", "INVALID_JSON")

    if msg_type == "eye_data":
        _number(data, "yaw", -180, 180)
        _number(data, "pitch", -90, 90)
        _integer(data, "is_focused", 0, 1)
        _number(data, "state_duration", 0)
        _number(data, "confidence", 0, 1)
    elif msg_type == "screen_data":
        _string(data, "state", SCREEN_STATES)
        _number(data, "confidence", 0, 1)
        _string(data, "app", required=False)
        _string(data, "category", SCREEN_CATEGORIES, required=False)
    elif msg_type == "rest_command":
        _string(data, "action", REST_ACTIONS)
        action = data["action"]
        if action in {"start", "extend"}:
            _integer(data, "duration", 1)
        _string(data, "reason", REST_REASONS, required=False)
    elif msg_type == "heartbeat":
        _integer(data, "uptime", 0)
        if "echo_seq" in data:
            _integer(data, "echo_seq", 0, UINT32_MAX)
    elif msg_type == "sync_request":
        if "fields" in data:
            fields = data["fields"]
            _require(isinstance(fields, list) and all(isinstance(item, str) for item in fields),
                     "data.fields must be an array of strings", "INVALID_JSON")
    elif msg_type == "state_update":
        _string(data, "state", STATES)
        _integer(data, "focus_score", 0, 100)
        _string(data, "prev_state", STATES)
        _number(data, "duration_in_state", 0)
        _string(data, "triggered_feedback", FEEDBACK_TYPES)
    elif msg_type == "focus_score":
        _integer(data, "score", 0, 100)
        _string(data, "state", STATES)
    elif msg_type == "rest_countdown":
        _integer(data, "remaining", 0)
        _integer(data, "total", 1)
        _string(data, "state", {"resting"})
        _string(data, "phase", REST_PHASES)
    elif msg_type == "display_content":
        for key in ("line1", "line2", "line3", "line4"):
            _string(data, key, required=False)
    elif msg_type == "device_status":
        _validate_device_status(data, required=True)
    elif msg_type == "vibration_feedback":
        _string(data, "mode")
        _string(data, "trigger")
        _boolean(data, "success")
    elif msg_type == "sync_response":
        _string(data, "state", STATES)
        _integer(data, "focus_score", 0, 100)
        _string(data, "prev_state", STATES)
        if "rest_countdown" in data:
            _require(data["rest_countdown"] is None or isinstance(data["rest_countdown"], Mapping),
                     "data.rest_countdown must be an object or null", "INVALID_JSON")
            if isinstance(data["rest_countdown"], Mapping):
                validate_data("rest_countdown", data["rest_countdown"])
        _require(isinstance(data.get("device_status"), Mapping),
                 "data.device_status must be an object", "INVALID_JSON")
        _validate_device_status(data["device_status"], required=False)
    elif msg_type == "error":
        _string(data, "code", ERROR_CODES)
        _string(data, "message")
        _boolean(data, "fatal")
    else:
        raise ProtocolError("unknown message type: %s" % msg_type, "INVALID_MSG_TYPE")


def _validate_envelope(message: Mapping[str, Any], allowed_types: Set[str]) -> None:
    _require(isinstance(message, Mapping), "message must be a JSON object")
    _require(isinstance(message.get("type"), str), "type must be a string", "MISSING_FIELD")
    msg_type = message["type"]
    _require(msg_type in allowed_types, "unknown message type: %s" % msg_type, "INVALID_MSG_TYPE")
    _require(isinstance(message.get("seq"), int) and not isinstance(message["seq"], bool),
             "seq must be uint32", "MISSING_FIELD")
    _require(0 <= message["seq"] <= UINT32_MAX, "seq is outside uint32", "OUT_OF_RANGE")
    _require(isinstance(message.get("ts"), int) and not isinstance(message["ts"], bool),
             "ts must be uint32", "MISSING_FIELD")
    _require(0 <= message["ts"] <= UINT32_MAX, "ts is outside uint32", "OUT_OF_RANGE")
    validate_data(msg_type, message.get("data", {}))


def encode_message(msg_type: str, data: Mapping[str, Any], seq: int, ts: Optional[int] = None,
                   allowed_types: Optional[Set[str]] = None) -> bytes:
    """Build and validate one JSON message.

    By default this keeps the Windows client's historical uplink behavior.
    A Linux GATT server can pass ``allowed_types=DOWNLINK_TYPES`` to encode
    notifications without duplicating this function.
    """

    if ts is None:
        import time
        ts = int(time.time())
    message = {"type": msg_type, "seq": seq, "ts": ts, "data": dict(data)}
    _validate_envelope(message, UPLINK_TYPES if allowed_types is None else allowed_types)
    try:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("data cannot be encoded as JSON: %s" % exc, "INVALID_JSON") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise ProtocolError("message is %d bytes; maximum is %d" % (len(payload), MAX_JSON_BYTES), "OUT_OF_RANGE")
    return payload


def encode_downlink(msg_type: str, data: Mapping[str, Any], seq: int,
                    ts: Optional[int] = None) -> bytes:
    """Convenience wrapper for UNO Q/Linux downlink notifications."""

    return encode_message(msg_type, data, seq, ts, allowed_types=DOWNLINK_TYPES)


def decode_message(payload: bytes, allowed_types: Optional[Set[str]] = None) -> BLEMessage:
    """Decode, size-check, and validate one inbound notification."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError("BLE payload must be bytes", "INVALID_JSON")
    raw = bytes(payload)
    if len(raw) > MAX_JSON_BYTES:
        raise ProtocolError("notification is too large: %d bytes" % len(raw), "OUT_OF_RANGE")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid UTF-8 JSON", "INVALID_JSON") from exc
    allowed = DOWNLINK_TYPES if allowed_types is None else allowed_types
    _validate_envelope(message, allowed)
    return BLEMessage(
        type=message["type"],
        seq=message["seq"],
        ts=message["ts"],
        data=dict(message.get("data", {})),
    )
