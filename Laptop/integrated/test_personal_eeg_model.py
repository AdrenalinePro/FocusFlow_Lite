import unittest
from pathlib import Path

import numpy as np

from personal_eeg_model import PersonalEEGModel


class PersonalEEGModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = PersonalEEGModel(
            Path(__file__).parent / "model_artifacts" / "eeg_baseline_v3"
        )

    def test_preprocessing_has_expected_shape_and_finite_values(self):
        result = self.model.preprocess(
            {
                "theta": 0.08,
                "alpha": 0.04,
                "beta": 0.30,
                "theta_beta": 0.267,
                "theta_alpha_beta": 0.40,
            }
        )
        self.assertEqual(result.shape, (1, 5))
        self.assertTrue(np.all(np.isfinite(result)))

    def test_prediction_is_a_valid_probability_and_state(self):
        prediction = self.model.predict(
            {
                "theta": 0.08,
                "alpha": 0.04,
                "beta": 0.30,
                "theta_beta": 0.267,
                "theta_alpha_beta": 0.40,
            }
        )
        self.assertGreaterEqual(prediction.distraction_probability, 0.0)
        self.assertLessEqual(prediction.distraction_probability, 1.0)
        self.assertAlmostEqual(
            prediction.focus_probability
            + prediction.distraction_probability,
            1.0,
            places=6,
        )
        self.assertIn(prediction.state, {"focus", "distraction"})

    def test_teacher_model_supports_metadata_without_log_features(self):
        teacher_model = PersonalEEGModel(
            Path(__file__).parent
            / "model_artifacts"
            / "eeg_attention_teacher_v4"
        )
        self.assertEqual(teacher_model.log1p_indices, ())
        prediction = teacher_model.predict(
            {
                "theta": 0.08,
                "alpha": 0.04,
                "beta": 0.30,
                "theta_beta": 0.267,
                "theta_alpha_beta": 0.40,
            }
        )
        self.assertGreaterEqual(prediction.focus_probability, 0.0)
        self.assertLessEqual(prediction.focus_probability, 1.0)


if __name__ == "__main__":
    unittest.main()
