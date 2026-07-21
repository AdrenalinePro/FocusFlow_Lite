import json

import pytest

from ble.windows_ble_protocol import ProtocolError, decode_message, encode_downlink, encode_message


def test_encode_compact_utf8_message():
    payload = encode_message(
        "eye_data",
        {"yaw": 5.2, "pitch": -3.1, "is_focused": 1, "state_duration": 2.5, "confidence": 0.95},
        0,
        1700000000,
    )
    assert len(payload) <= 240
    assert b" " not in payload
    assert decode_message(payload, {"eye_data"}).seq == 0


def test_decode_rejects_unknown_type():
    payload = json.dumps({"type": "unknown", "seq": 1, "ts": 1, "data": {}}).encode()
    with pytest.raises(ProtocolError) as exc:
        decode_message(payload)
    assert exc.value.code == "INVALID_MSG_TYPE"


def test_encode_rejects_out_of_range_eye_data():
    with pytest.raises(ProtocolError) as exc:
        encode_message("eye_data", {
            "yaw": 181, "pitch": 0, "is_focused": 1, "state_duration": 0, "confidence": 1,
        }, 1, 1)
    assert exc.value.code == "OUT_OF_RANGE"


def test_encode_rejects_oversized_json():
    with pytest.raises(ProtocolError) as exc:
        encode_message("screen_data", {
            "state": "focused", "confidence": 1, "app": "x" * 300,
        }, 1, 1)
    assert exc.value.code == "OUT_OF_RANGE"


def test_encode_downlink_accepts_linux_message_types():
    payload = encode_downlink("focus_score", {"score": 85, "state": "focused"}, 0, 1)
    message = decode_message(payload)
    assert message.type == "focus_score"
    assert message.data["score"] == 85


def test_compact_sync_response_fits_wire_limit():
    payload = encode_downlink("sync_response", {
        "state": "focused",
        "focus_score": 85,
        "prev_state": "focused",
        "device_status": {
            "eeg_connected": True,
            "wristband_connected": True,
            "tft_display": "running",
        },
    }, 0, 1)
    assert len(payload) <= 240
    assert "rest_countdown" not in decode_message(payload).data


def test_sync_response_allows_partial_nested_device_status():
    payload = encode_downlink("sync_response", {
        "state": "focused",
        "focus_score": 85,
        "prev_state": "focused",
        "rest_countdown": None,
        "device_status": {"eeg_connected": True},
    }, 0, 1)
    assert decode_message(payload).data["device_status"] == {"eeg_connected": True}
