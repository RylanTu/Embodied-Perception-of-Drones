import copy
from collections import deque
from pathlib import Path
import threading
import unittest

from detector import PressureSpikeDetector
from model_detector import HybridDetector
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

    def test_model_requires_configured_consecutive_hits(self):
        class FakeSession:
            def infer(self, feeds, mode):
                return [[[[10.0]]]]

        config = self.config()
        detector = HybridDetector(config, Path("."))
        detector.enabled = True
        detector.session = FakeSession()
        detector.session_thread_id = threading.get_ident()
        detector.reload_pending = False
        detector.window_samples = 2
        detector.samples = deque(maxlen=2)
        detector.threshold = 0.5
        detector.consecutive_hits = 3
        valid = [True] * 5
        self.assertFalse(detector.process([0.0] * 5, valid)["obstacle"])
        self.assertFalse(detector.process([0.0] * 5, valid)["obstacle"])
        self.assertFalse(detector.process([0.0] * 5, valid)["obstacle"])
        self.assertTrue(detector.process([0.0] * 5, valid)["obstacle"])

    def test_model_uses_selected_sensor_order(self):
        class FakeSession:
            feed = None

            def infer(self, feeds, mode):
                self.feed = feeds[0]
                return [[[[0.0]]]]

        config = self.config()
        config["model"]["channels"] = [2, 3, 4]
        config["model"]["input_channels"] = 3
        detector = HybridDetector(config, Path("."))
        session = FakeSession()
        detector.enabled = True
        detector.session = session
        detector.session_thread_id = threading.get_ident()
        detector.reload_pending = False
        detector.window_samples = 1
        detector.samples = deque(maxlen=1)
        detector.process([11.0, 22.0, 33.0, 44.0, 55.0], [True] * 5)
        self.assertEqual(tuple(session.feed.shape), (1, 3, 1, 1))
        self.assertEqual(session.feed.reshape(-1).tolist(), [22.0, 33.0, 44.0])


if __name__ == "__main__":
    unittest.main()
