import copy
import unittest

from detector import PressureSpikeDetector
from service import DEFAULT_CONFIG, validate_config


class DetectorTests(unittest.TestCase):
    def config(self):
        value = copy.deepcopy(DEFAULT_CONFIG)
        value["prefer_npu"] = False
        return value

    def test_two_channel_pressure_peak_detects_obstacle(self):
        detector = PressureSpikeDetector(self.config())
        detector.process([0.0] * 5, [True] * 5)
        result = detector.process([3.0, -3.0, 0.0, 0.0, 0.0], [True] * 5)
        self.assertEqual(result["spikes"][:2], [1, 1])
        self.assertTrue(result["obstacle"])

    def test_one_channel_does_not_meet_default_vote(self):
        detector = PressureSpikeDetector(self.config())
        detector.process([0.0] * 5, [True] * 5)
        result = detector.process([3.0, 0.0, 0.0, 0.0, 0.0], [True] * 5)
        self.assertFalse(result["obstacle"])

    def test_model_probability_threshold_is_validated(self):
        config = self.config()
        config["model"]["probability_threshold"] = 1.5
        with self.assertRaises(ValueError):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
