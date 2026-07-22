import unittest
from pathlib import Path
from types import SimpleNamespace

from uno_q_bridge import UnoQBridge, decision_payload, install_decision_protocol


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


class UnoQPreDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_caches_service_filtered_device_before_headband(self):
        published = []

        async def publish(message):
            published.append(message)

        device = SimpleNamespace(name="arduino-UNO", address="14:B5:CD:F1:F4:AF")
        advertisement = SimpleNamespace(
            local_name=None,
            service_uuids=["19B10000-E8F2-537E-4F6C-D104768A1214"],
        )

        class FakeScanner:
            @classmethod
            async def find_device_by_filter(cls, predicate, **kwargs):
                self.assertEqual(kwargs["service_uuids"], advertisement.service_uuids)
                return device if predicate(device, advertisement) else None

        bridge = UnoQBridge(Path(__file__).parent, device="UNO-Q-FF01", publisher=publish)
        found = await bridge.pre_discover(
            scanner_cls=FakeScanner,
            service_uuid=advertisement.service_uuids[0],
        )

        self.assertTrue(found)
        self.assertIs(bridge.resolved_device, device)
        self.assertEqual(published[0]["state"], "pre_scanning")
        self.assertEqual(published[-1]["state"], "cached")

    async def test_reports_not_found_without_starting_late_scan(self):
        published = []

        async def publish(message):
            published.append(message)

        class EmptyScanner:
            @classmethod
            async def find_device_by_filter(cls, predicate, **kwargs):
                return None

        bridge = UnoQBridge(Path(__file__).parent, device="UNO-Q-FF01", publisher=publish)
        found = await bridge.pre_discover(
            scanner_cls=EmptyScanner,
            service_uuid="19B10000-E8F2-537E-4F6C-D104768A1214",
        )

        self.assertFalse(found)
        self.assertIsNone(bridge.resolved_device)
        self.assertEqual(published[-1]["state"], "not_found")


if __name__ == "__main__":
    unittest.main()
