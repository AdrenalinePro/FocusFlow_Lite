import unittest

import numpy as np

from local_eeg_features import FeatureConfig, StreamingFeatureExtractor, extract_features


class LocalEEGFeatureTests(unittest.TestCase):
    def setUp(self):
        self.config = FeatureConfig()
        self.t = np.arange(self.config.window_samples) / self.config.sample_rate

    def _two_channel_signal(self, frequency: float):
        left = 50.0 * np.sin(2.0 * np.pi * frequency * self.t)
        right = 45.0 * np.sin(2.0 * np.pi * frequency * self.t + 0.2)
        return left, right

    def test_theta_tone_is_theta_dominant(self):
        left, right = self._two_channel_signal(6.0)
        result = extract_features(left, right, config=self.config)
        self.assertTrue(result.valid)
        self.assertGreater(result.theta, 0.9)
        self.assertGreater(result.theta, result.alpha)
        self.assertGreater(result.theta, result.beta)

    def test_alpha_tone_is_alpha_dominant(self):
        left, right = self._two_channel_signal(10.0)
        result = extract_features(left, right, config=self.config)
        self.assertTrue(result.valid)
        self.assertGreater(result.alpha, 0.9)
        self.assertGreater(result.alpha, result.theta)
        self.assertGreater(result.alpha, result.beta)

    def test_beta_tone_is_beta_dominant(self):
        left, right = self._two_channel_signal(20.0)
        result = extract_features(left, right, config=self.config)
        self.assertTrue(result.valid)
        self.assertGreater(result.beta, 0.9)
        self.assertGreater(result.beta, result.theta)
        self.assertGreater(result.beta, result.alpha)

    def test_flat_line_is_rejected(self):
        zeros = np.zeros(self.config.window_samples)
        result = extract_features(zeros, zeros, config=self.config)
        self.assertFalse(result.valid)
        self.assertEqual(result.invalid_reason, "flat_line")

    def test_bad_device_quality_is_rejected(self):
        left, right = self._two_channel_signal(10.0)
        result = extract_features(left, right, quality=0, config=self.config)
        self.assertFalse(result.valid)
        self.assertEqual(result.invalid_reason, "poor_device_quality:0")

    def test_high_device_quality_is_accepted(self):
        left, right = self._two_channel_signal(10.0)
        result = extract_features(left, right, quality=4, config=self.config)
        self.assertTrue(result.valid)

    def test_streaming_bad_quality_emits_explicit_invalid_row(self):
        left, right = self._two_channel_signal(10.0)
        extractor = StreamingFeatureExtractor(self.config)
        result = extractor.add_block(left, right, quality=0)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].valid)
        self.assertEqual(result[0].invalid_reason, "poor_device_quality:0")

    def test_streaming_extractor_emits_once_per_step(self):
        left, right = self._two_channel_signal(10.0)
        extractor = StreamingFeatureExtractor(self.config)
        first = extractor.add_block(left, right)
        self.assertEqual(len(first), 1)
        extra_t = np.arange(self.config.step_samples) / self.config.sample_rate
        extra_left = 50.0 * np.sin(2.0 * np.pi * 10.0 * extra_t)
        extra_right = 45.0 * np.sin(2.0 * np.pi * 10.0 * extra_t + 0.2)
        second = extractor.add_block(extra_left, extra_right)
        self.assertEqual(len(second), 1)


if __name__ == "__main__":
    unittest.main()
