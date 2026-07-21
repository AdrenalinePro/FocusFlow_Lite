import unittest

from flowtime_packet_decoder import decode_eeg_packet, sequence_distance


class FlowtimePacketDecoderTests(unittest.TestCase):
    def test_captured_first_packet(self):
        packet = [
            0, 0,
            23, 158, 41, 23, 158, 41,
            23, 87, 21, 23, 87, 21,
            23, 4, 84, 23, 4, 84,
        ]
        result = decode_eeg_packet(packet)
        self.assertEqual(result.sequence, 0)
        self.assertEqual(result.left, (1547817, 1529621, 1508436))
        self.assertEqual(result.right, result.left)

    def test_signed_24_bit_value(self):
        packet = [0, 1] + [255, 255, 255, 0, 0, 1] * 3
        result = decode_eeg_packet(packet)
        self.assertEqual(result.left, (-1, -1, -1))
        self.assertEqual(result.right, (1, 1, 1))

    def test_sequence_wrap(self):
        self.assertEqual(sequence_distance(65535, 0), 1)

    def test_invalid_length(self):
        with self.assertRaises(ValueError):
            decode_eeg_packet([0] * 19)


if __name__ == "__main__":
    unittest.main()
