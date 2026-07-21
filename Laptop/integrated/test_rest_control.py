import unittest

from rest_control import RestController


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class RestControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.controller = RestController(self.clock)

    def test_start_counts_down_and_ends_once(self):
        state = self.controller.start(60, "manual")
        self.assertTrue(state["active"])
        self.assertEqual(state["remaining_seconds"], 60)

        self.clock.now += 31
        state, ended = self.controller.poll()
        self.assertFalse(ended)
        self.assertEqual(state["remaining_seconds"], 29)
        self.assertEqual(state["phase"], "ending")

        self.clock.now += 29
        state, ended = self.controller.poll()
        self.assertTrue(ended)
        self.assertFalse(state["active"])
        self.assertEqual(state["phase"], "ended")
        self.assertFalse(self.controller.poll()[1])

    def test_extend_and_manual_stop(self):
        self.controller.start(10)
        self.clock.now += 5
        state = self.controller.extend(20)
        self.assertEqual(state["remaining_seconds"], 25)
        self.assertEqual(state["duration_seconds"], 30)
        state = self.controller.stop()
        self.assertFalse(state["active"])
        self.assertFalse(state["outputs_paused"])

    def test_invalid_commands_are_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.start(0)
        with self.assertRaises(ValueError):
            self.controller.extend(10)


if __name__ == "__main__":
    unittest.main()
