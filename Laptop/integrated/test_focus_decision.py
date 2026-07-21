import unittest

from focus_decision import FocusDecisionEngine


class FocusDecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FocusDecisionEngine()

    def test_screen_non_learning_has_highest_priority(self):
        self.engine.update_screen({"state": "slacking", "is_learning": False}, now=1)
        self.engine.update_camera({"face_detected": True, "is_focused": True}, now=1)
        self.engine.update_eeg(99, valid=True, source="test", now=1)
        self.assertEqual(self.engine.evaluate(now=2)["state"], "slacking")

    def test_learning_but_camera_away_is_distracted(self):
        self.engine.update_screen({"state": "focused", "is_learning": True}, now=1)
        self.engine.update_camera(
            {"face_detected": False, "is_focused": False, "state_duration": 4},
            now=1,
        )
        self.engine.update_eeg(90, valid=True, source="test", now=1)
        self.assertEqual(self.engine.evaluate(now=2)["state"], "distracted")

    def test_short_camera_deviation_is_debounced(self):
        self.engine.update_screen({"is_learning": True}, now=1)
        self.engine.update_camera(
            {"face_detected": True, "is_focused": False, "state_duration": 1.5},
            now=1,
        )
        self.assertEqual(self.engine.evaluate(now=2)["state"], "checking_camera")

    def test_eeg_percentage_only_appears_after_both_gates(self):
        self.engine.update_screen({"is_learning": True}, now=1)
        self.engine.update_camera(
            {"face_detected": True, "looking_at_screen": True, "state_duration": 8},
            now=1,
        )
        self.engine.update_eeg(73.26, valid=True, source="personal", now=1)
        result = self.engine.evaluate(now=2)
        self.assertEqual(result["state"], "focused")
        self.assertEqual(result["label"], "专注：73.3%")

    def test_invalid_eeg_is_not_reported_as_zero_focus(self):
        self.engine.update_screen({"is_learning": True}, now=1)
        self.engine.update_camera(
            {"face_detected": True, "is_focused": True, "state_duration": 8},
            now=1,
        )
        self.engine.update_eeg(None, valid=False, source="personal", reason="poor quality", now=1)
        self.assertEqual(self.engine.evaluate(now=2)["state"], "eeg_invalid")

    def test_group_camera_chinese_state_is_supported(self):
        self.engine.update_screen({"state": "专注工作"}, now=1)
        self.engine.update_camera(
            {"face_detected": True, "state": "专注", "state_duration": 8},
            now=1,
        )
        self.engine.update_eeg(66, valid=True, source="personal", now=1)
        self.assertEqual(self.engine.evaluate(now=2)["label"], "专注：66.0%")

    def test_reset_discards_pre_rest_inputs(self):
        self.engine.update_screen({"is_learning": True}, now=1)
        self.engine.update_camera(
            {"face_detected": True, "looking_at_screen": True}, now=1
        )
        self.engine.update_eeg(80, valid=True, source="test", now=1)
        self.engine.reset()
        result = self.engine.evaluate(now=1, ts=1)
        self.assertEqual(result["state"], "waiting_screen")


if __name__ == "__main__":
    unittest.main()
