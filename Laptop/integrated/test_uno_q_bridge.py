import unittest
from types import SimpleNamespace

from uno_q_bridge import decision_payload, install_decision_protocol


class DecisionPayloadTests(unittest.TestCase):
    def test_maps_final_states(self):
        cases = {
            "focused": "focused",
            "distracted": "distracted",
            "slacking": "procrastinating",
            "waiting_eeg": "waiting",
            "eeg_invalid": "waiting",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                payload = decision_payload(
                    {"state": source, "focus_percent": 82.4},
                    {"app": "VSCode"},
                    resting=False,
                )
                self.assertEqual(payload["state"], expected)
                self.assertEqual(payload["score"], 82)

    def test_rest_overrides_and_app_is_bounded(self):
        payload = decision_payload(
            {"state": "slacking", "focus_percent": None},
            {"app": "x" * 100},
            resting=True,
        )
        self.assertEqual(payload["state"], "resting")
        self.assertIsNone(payload["score"])
        self.assertEqual(len(payload["app"]), 24)

    def test_protocol_extension_accepts_decision_update(self):
        class ProtocolError(ValueError):
            def __init__(self, message, code):
                super().__init__(message)
                self.code = code

        protocol = SimpleNamespace(
            UPLINK_TYPES={"heartbeat"},
            ProtocolError=ProtocolError,
            validate_data=lambda msg_type, data: None,
        )
        install_decision_protocol(protocol)
        payload = decision_payload(
            {"state": "focused", "focus_percent": 87},
            {"app": "VSCode"},
            resting=False,
        )
        protocol.validate_data("decision_update", payload)
        self.assertIn("decision_update", protocol.UPLINK_TYPES)


if __name__ == "__main__":
    unittest.main()
