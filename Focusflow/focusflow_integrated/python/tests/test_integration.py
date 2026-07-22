"""Integration tests for the FocusFlow BLE server subclass."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, call

# ── Path setup BEFORE importing modules under test ────────────────────
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.dirname(THIS_DIR)
REPO_DIR = os.path.dirname(os.path.dirname(PYTHON_DIR))
SOURCE_CODE = os.path.join(REPO_DIR, "source_code")
WINDOWS_PROTO_ROOT = "/home/arduino/focusble"

for path in (SOURCE_CODE, WINDOWS_PROTO_ROOT, PYTHON_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

# The Arduino App framework is only present on the UNO Q.  Provide a
# stub so ``from arduino.app_utils import ...`` does not blow up.
if "arduino" not in sys.modules:
    import types
    _arduino_pkg = types.ModuleType("arduino")
    _app_utils = types.ModuleType("arduino.app_utils")
    _app_utils.Bridge = MagicMock()
    _app_utils.App = MagicMock()
    _app_utils.Logger = MagicMock()
    sys.modules["arduino"] = _arduino_pkg
    sys.modules["arduino.app_utils"] = _app_utils

from linux.linux_ble_protocol import BLEMessage
from linux.linux_ble_server import BleServerConfig

import focusflow_server as ffs


def _make_server(
    wristband_subscribed: bool = True,
) -> "ffs.FocusFlowBLEServer":
    """Build a FocusFlowBLEServer with fully mocked peripherals."""

    wristband = MagicMock()
    wristband.is_subscribed.return_value = wristband_subscribed
    wristband.send_vibration.return_value = True
    wristband.stop_vibration.return_value = True

    tft = MagicMock()
    tft.last_status.return_value = "running"

    config = BleServerConfig()
    server = ffs.FocusFlowBLEServer(
        config=config,
        wristband=wristband,
        tft=tft,
        vibration_intensity=ffs.DEFAULT_VIBRATION_INTENSITY,
        logger=MagicMock(),
    )
    # Replace the async network calls with AsyncMocks so we can
    # assert on them without a real asyncio loop.
    server.send_state_update = AsyncMock()
    server.send_vibration_feedback = AsyncMock()

    # Sequence tracker must accept all seqs for these tests.
    server._incoming_sequences = MagicMock()
    server._incoming_sequences.accept.return_value = True

    return server, wristband, tft


def _du(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _run(coro):
    """Run a coroutine to completion on a fresh loop and close it."""

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────────────
#  A. decision_update parsing (the bypass decoder)
# ─────────────────────────────────────────────────────────────────────
class TestDecisionUpdateParsing(unittest.TestCase):
    """Cover _try_parse_decision_update: valid + invalid inputs."""

    def setUp(self):
        self.server, _, _ = _make_server()

    def test_valid_full_payload(self):
        payload = _du({
            "type": "decision_update",
            "seq": 12,
            "ts": 1784600000,
            "data": {
                "state": "focused",
                "score": 82,
                "duration": 15.0,
                "signal_ok": True,
                "app": "VS Code",
            },
        })
        msg = self.server._try_parse_decision_update(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.type, "decision_update")
        self.assertEqual(msg.seq, 12)
        self.assertEqual(msg.ts, 1784600000)
        self.assertEqual(msg.data["state"], "focused")
        self.assertEqual(msg.data["score"], 82)
        self.assertEqual(msg.data["app"], "VS Code")
        self.assertTrue(msg.data["signal_ok"])

    def test_minimal_payload_uses_default_signal_ok(self):
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "focused"},
        })
        msg = self.server._try_parse_decision_update(payload)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.data["signal_ok"])
        self.assertNotIn("score", msg.data)

    def test_null_score_kept(self):
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "focused", "score": None},
        })
        msg = self.server._try_parse_decision_update(payload)
        self.assertIsNotNone(msg)
        self.assertNotIn("score", msg.data)  # null stripped

    def test_app_truncated_to_24_chars(self):
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "focused", "app": "x" * 50},
        })
        msg = self.server._try_parse_decision_update(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(len(msg.data["app"]), 24)

    def test_unknown_state_rejected(self):
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "daydreaming"},
        })
        self.assertIsNone(self.server._try_parse_decision_update(payload))

    def test_waiting_state_accepted(self):
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "waiting", "signal_ok": False},
        })
        msg = self.server._try_parse_decision_update(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.data["state"], "waiting")

    def test_score_out_of_range_rejected(self):
        for bad in (101, -1, 150, "abc"):
            payload = _du({
                "type": "decision_update", "seq": 1, "ts": 1,
                "data": {"state": "focused", "score": bad},
            })
            self.assertIsNone(
                self.server._try_parse_decision_update(payload),
                f"score={bad!r} should be rejected",
            )

    def test_non_decision_update_returns_none(self):
        payload = _du({"type": "eye_data", "seq": 1, "ts": 1, "data": {}})
        self.assertIsNone(self.server._try_parse_decision_update(payload))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(
            self.server._try_parse_decision_update(b"not json"),
        )
        self.assertIsNone(
            self.server._try_parse_decision_update(b""),
        )

    def test_unknown_fields_are_dropped(self):
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {
                "state": "focused",
                "score": 90,
                "mystery_field": "should be dropped",
                "another": 42,
            },
        })
        msg = self.server._try_parse_decision_update(payload)
        self.assertIsNotNone(msg)
        self.assertNotIn("mystery_field", msg.data)
        self.assertNotIn("another", msg.data)


# ─────────────────────────────────────────────────────────────────────
#  B. State transition → vibration policy mapping
# ─────────────────────────────────────────────────────────────────────
class TestVibrationPolicy(unittest.TestCase):

    def _transition_to(self, state, **extra):
        server, wb, tft = _make_server()
        msg = BLEMessage(
            type="decision_update",
            seq=1, ts=1,
            data={"state": state, "score": 80, "app": "VS", **extra},
        )
        _run(server._handle_decision_update(msg))
        return server, wb, tft

    def test_distracted_triggers_3x_vibration(self):
        server, wb, tft = self._transition_to("distracted")
        wb.send_vibration.assert_called_once_with(
            ffs.DEFAULT_VIBRATION_INTENSITY,
            ffs.VIBRATION_REPEATS_DISTRACTED,
        )
        tft.show_alert.assert_called_once()

    def test_procrastinating_triggers_3x_vibration(self):
        server, wb, tft = self._transition_to("procrastinating")
        wb.send_vibration.assert_called_once_with(
            ffs.DEFAULT_VIBRATION_INTENSITY,
            ffs.VIBRATION_REPEATS_PROCRASTINATING,
        )
        tft.show_alert.assert_called_once()

    def test_waiting_does_not_vibrate(self):
        # Per BLE supplement: "waiting 不应触发惩罚性反馈"
        server, wb, tft = self._transition_to("waiting")
        wb.send_vibration.assert_not_called()
        wb.stop_vibration.assert_not_called()
        # And the TFT should NOT flip to alert (no punitive UI)
        tft.show_alert.assert_not_called()
        tft.show_focus.assert_not_called()

    def test_resting_calls_stop_vibration(self):
        # Per BLE supplement: "resting 必须立即停止屏幕以外的设备输出"
        server, wb, tft = self._transition_to("resting")
        wb.stop_vibration.assert_called_once()
        wb.send_vibration.assert_not_called()

    def test_focused_no_vibration_no_alert(self):
        server, wb, tft = self._transition_to("focused")
        wb.send_vibration.assert_not_called()
        wb.stop_vibration.assert_not_called()
        tft.show_alert.assert_not_called()
        # Initial driver state is "focused", so no transition occurs
        # and show_focus is not called either.  (Subsequent transitions
        # into focused DO call show_focus — see below.)

    def test_distracted_then_focused_shows_focus_screen(self):
        server, wb, tft = _make_server()
        # focused → distracted (initial state is focused)
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=1, ts=1,
            data={"state": "distracted", "score": 20, "app": "B站"},
        )))
        tft.show_alert.assert_called_once()
        # distracted → focused
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=2, ts=2,
            data={"state": "focused", "score": 80, "app": "VS Code"},
        )))
        tft.show_focus.assert_called_once()
        kwargs = tft.show_focus.call_args.kwargs
        self.assertEqual(kwargs["pct"], 80)
        self.assertEqual(kwargs["screen"], "VS Code")

    def test_score_null_keeps_previous(self):
        server, _, _ = _make_server()
        # Set the driver score first via a normal decision_update
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=1, ts=1,
            data={"state": "distracted", "score": 25},
        )))
        # After this, the driver may have changed state; reset driver
        # state and re-issue with null score
        server._driver.current_state = "focused"
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=2, ts=2,
            data={"state": "focused", "score": None},
        )))
        # Driver focus_score should still be 25 (the last numeric one)
        self.assertEqual(server._driver.focus_score, 25)


# ─────────────────────────────────────────────────────────────────────
#  C. state_update to Windows — feedback field per state
# ─────────────────────────────────────────────────────────────────────
class TestStateUpdateFeedback(unittest.TestCase):
    """Verify triggered_feedback matches the BLE supplement spec."""

    def test_feedback_mapping_table(self):
        # The mapping is the critical fix in this iteration: resting
        # used to be "vibrate_continuous" — must now be "none".
        self.assertEqual(ffs.FEEDBACK_FOR_STATE["focused"], "none")
        self.assertEqual(ffs.FEEDBACK_FOR_STATE["waiting"], "none")
        self.assertEqual(ffs.FEEDBACK_FOR_STATE["resting"], "none")
        self.assertEqual(ffs.FEEDBACK_FOR_STATE["distracted"], "vibrate_short")
        self.assertEqual(ffs.FEEDBACK_FOR_STATE["procrastinating"],
                         "vibrate_double")

    def test_state_update_sent_on_distracted(self):
        server, _, _ = _make_server()
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=1, ts=1,
            data={"state": "distracted", "score": 30, "app": "B站"},
        )))
        server.send_state_update.assert_awaited_once()
        kwargs = server.send_state_update.call_args.kwargs
        self.assertEqual(kwargs["state"], "distracted")
        self.assertEqual(kwargs["triggered_feedback"], "vibrate_short")
        self.assertEqual(kwargs["focus_score"], 30)
        self.assertEqual(kwargs["prev_state"], "focused")

    def test_state_update_resting_uses_none_feedback(self):
        server, _, _ = _make_server()
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=1, ts=1,
            data={"state": "distracted", "score": 20},
        )))
        server.send_state_update.reset_mock()
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=2, ts=2,
            data={"state": "resting", "score": 0},
        )))
        kwargs = server.send_state_update.call_args.kwargs
        self.assertEqual(kwargs["state"], "resting")
        self.assertEqual(kwargs["triggered_feedback"], "none")


# ─────────────────────────────────────────────────────────────────────
#  D. Wristband not subscribed → vibration is skipped, feedback
#     reports success=False
# ─────────────────────────────────────────────────────────────────────
class TestWristbandOffline(unittest.TestCase):

    def test_no_vibration_when_wristband_offline(self):
        server, wb, tft = _make_server(wristband_subscribed=False)
        wb.send_vibration.return_value = False
        _run(server._handle_decision_update(BLEMessage(
            type="decision_update", seq=1, ts=1,
            data={"state": "distracted", "score": 20, "app": "B站"},
        )))
        # send_vibration is never called when not subscribed
        wb.send_vibration.assert_not_called()
        # show_alert still happens (TFT is independent of BLE wristband)
        tft.show_alert.assert_called_once()
        # Vibration feedback reports failure
        server.send_vibration_feedback.assert_awaited_once()
        kwargs = server.send_vibration_feedback.call_args.kwargs
        self.assertFalse(kwargs["success"])


# ─────────────────────────────────────────────────────────────────────
#  E. device_status override — real wristband + TFT health
# ─────────────────────────────────────────────────────────────────────
class TestDeviceStatus(unittest.TestCase):

    def test_includes_real_wristband_and_tft_state(self):
        server, wb, tft = _make_server(wristband_subscribed=True)
        tft.last_status.return_value = "running"
        snap = server._snapshot_device_status()
        self.assertTrue(snap["wristband_connected"])
        self.assertEqual(snap["tft_display"], "running")
        # EEG fields stay at placeholder values (not implemented)
        self.assertFalse(snap["eeg_connected"])
        self.assertEqual(snap["eeg_battery"], -1)
        self.assertEqual(snap["wristband_battery"], -1)  # not implemented

    def test_wristband_disconnected_reflected(self):
        server, _, _ = _make_server(wristband_subscribed=False)
        snap = server._snapshot_device_status()
        self.assertFalse(snap["wristband_connected"])


# ─────────────────────────────────────────────────────────────────────
#  F. _handle_rx bypass — decision_update does not go through the
#     strict upstream decoder (which would reject it).
# ─────────────────────────────────────────────────────────────────────
class TestHandleRxBypass(unittest.TestCase):

    def test_decision_update_bypasses_strict_decoder(self):
        server, wb, tft = _make_server()
        # The strict decoder in source_code/linux/ would raise
        # INVALID_MSG_TYPE for decision_update.  Verify we never call
        # it for this path.
        server._decode_message = MagicMock(
            side_effect=AssertionError("should not be called for decision_update"),
        )
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "distracted", "score": 20, "app": "B站"},
        })
        _run(server._handle_rx(payload))
        wb.send_vibration.assert_called_once_with(40, 3)
        tft.show_alert.assert_called_once()

    def test_duplicate_seq_is_dropped(self):
        server, wb, _ = _make_server()
        server._incoming_sequences.accept.return_value = False
        payload = _du({
            "type": "decision_update", "seq": 1, "ts": 1,
            "data": {"state": "distracted", "score": 20},
        })
        _run(server._handle_rx(payload))
        wb.send_vibration.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
#  G. rest_command still works (backward compat with original req #3)
# ─────────────────────────────────────────────────────────────────────
class TestRestCommandCompat(unittest.TestCase):
    """Existing rest_command flow must still produce 1× / 2× vibrations."""

    def _make_rest_handler_state(self, in_rest: bool):
        """Pretend we are / are not in rest to test the override's edges."""

        server, wb, tft = _make_server()
        # Fake the upstream bookkeeping so the override sees the
        # appropriate transitions.
        if in_rest:
            server._rest_started_at = 100.0
            server._rest_duration = 300
            server._driver.current_state = "resting"
        else:
            server._rest_started_at = None
            server._rest_duration = 0
            server._driver.current_state = "focused"
        return server, wb, tft

    def test_rest_start_vibrates_1x(self):
        server, wb, tft = self._make_rest_handler_state(in_rest=False)
        _run(server._handle_rest_command({"action": "start", "duration": 300}))
        wb.send_vibration.assert_called_once_with(40, 1)
        tft.show_break.assert_called_once()

    def test_rest_stop_vibrates_2x(self):
        server, wb, tft = self._make_rest_handler_state(in_rest=True)
        _run(server._handle_rest_command({"action": "stop"}))
        wb.send_vibration.assert_called_once_with(40, 2)
        tft.show_focus.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
#  H. End-to-end scenario walkthrough
# ─────────────────────────────────────────────────────────────────────
class TestEndToEndScenario(unittest.TestCase):

    def test_full_study_session_flow(self):
        """Walk through a realistic 4-state sequence."""

        server, wb, tft = _make_server()
        msgs = [
            # Initial: focused (initial driver state)
            {"type": "decision_update", "seq": 1, "ts": 1,
             "data": {"state": "distracted", "score": 25, "app": "B站"}},
            # Slacking on phone
            {"type": "decision_update", "seq": 2, "ts": 2,
             "data": {"state": "procrastinating", "score": 15, "app": "B站"}},
            # Back to focus
            {"type": "decision_update", "seq": 3, "ts": 3,
             "data": {"state": "focused", "score": 80, "app": "VS Code"}},
            # Out of view
            {"type": "decision_update", "seq": 4, "ts": 4,
             "data": {"state": "waiting", "signal_ok": False}},
            # Enter rest via rest_command (the laptop's older path)
        ]

        for raw in msgs:
            payload = _du(raw)
            _run(server._handle_rx(payload))

        # Vibrations: 2 × distraction transitions, 0 × waiting
        self.assertEqual(wb.send_vibration.call_count, 2)
        wb.send_vibration.assert_has_calls([
            call(40, ffs.VIBRATION_REPEATS_DISTRACTED),
            call(40, ffs.VIBRATION_REPEATS_PROCRASTINATING),
        ])

        # Alerts: distracted + procrastinating (waiting is non-punitive)
        self.assertEqual(tft.show_alert.call_count, 2)

        # Focus screen called once for the focused transition
        tft.show_focus.assert_called_once()

        # Three state_update calls: distracted, procrastinating, focused
        # (waiting is a state transition but feedback is "none" — it
        # still gets sent.)
        self.assertGreaterEqual(server.send_state_update.await_count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
