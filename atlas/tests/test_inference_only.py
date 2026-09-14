from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from service import AppState, SerialController


class IntegratedControllerTests(unittest.TestCase):
    def test_default_policy_requires_three_model_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AppState(Path(directory) / "config.json")
            self.assertEqual(state.config["model"]["consecutive_hits"], 3)

    def test_telemetry_is_inferred_when_no_batch_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = AppState(root / "config.json")
            state.on_telemetry({
                "pressure_pa": [0.0] * 5,
                "temperature_c": [20.0] * 5,
                "valid": [True] * 5,
                "uptime_ms": 1,
                "sample_sequence": 1,
            })
            self.assertIn("detection", state.latest)
            self.assertEqual(list(root.glob("*.csv")), [])
            self.assertEqual(state.public_status()["role"], "integrated_controller")

    def test_confirmed_model_obstacle_requests_one_shot_stop(self):
        class BatchStub:
            active = True
            recording = True
            s = {"start": 10.0, "accel": 225.0}

            def __init__(self):
                self.samples = []
                self.stops = []

            def on_sample(self, sample):
                self.samples.append(sample)

            def on_auto_stop(self, position):
                self.stops.append(position)

            def public_status(self):
                return {"active": self.active}

        with tempfile.TemporaryDirectory() as directory:
            state = AppState(Path(directory) / "config.json")
            state.config["model"]["stop_on_obstacle"] = True
            batch = BatchStub()
            state.batch = batch
            state.detector = SimpleNamespace(process=lambda *args: {
                "obstacle": True, "backend": "ascend_om", "model_probability": 0.99,
            })
            sample = {
                "pressure_pa": [0.0] * 5, "temperature_c": [20.0] * 5,
                "valid": [True] * 5, "uptime_ms": 1, "sample_sequence": 1,
                "position_mm": 800.0, "moving": True, "enabled": True,
                "stopped": False, "sensor_test_mode": False,
            }
            self.assertTrue(state.on_telemetry(sample))
            writes = []
            link = SerialController(state)
            link.transport = "json"
            link.serial = SimpleNamespace(is_open=True, write=lambda value: writes.append(value))
            link._maybe_send_auto_stop(True)
            link._maybe_send_auto_stop(True)
            self.assertEqual(writes, [b"STOP\n"])
            self.assertEqual(batch.stops, [800.0])
            self.assertTrue(state.public_status()["auto_stop"]["latched"])

    def test_simulated_sensor_data_does_not_request_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AppState(Path(directory) / "config.json")
            state.config["model"]["stop_on_obstacle"] = True
            state.batch = SimpleNamespace(active=True, recording=True,
                                          s={"start": 10.0, "accel": 225.0},
                                          on_sample=lambda sample: None)
            state.detector = SimpleNamespace(process=lambda *args: {
                "obstacle": True, "backend": "ascend_om", "model_probability": 0.99,
            })
            requested = state.on_telemetry({
                "pressure_pa": [0.0] * 5, "temperature_c": [20.0] * 5,
                "valid": [True] * 5, "uptime_ms": 1, "sample_sequence": 1,
                "position_mm": 800.0, "moving": True, "enabled": True,
                "stopped": False, "sensor_test_mode": True,
            })
            self.assertFalse(requested)

    def test_acceleration_distance_is_ignored_then_fresh_hits_are_required(self):
        class DetectorStub:
            resets = 0

            def process(self, *args):
                return {"obstacle": True, "backend": "ascend_om", "model_probability": 0.99}

            def reset_decision(self):
                self.resets += 1

        with tempfile.TemporaryDirectory() as directory:
            state = AppState(Path(directory) / "config.json")
            state.config["model"]["stop_on_obstacle"] = True
            state.detector = DetectorStub()
            state.batch = SimpleNamespace(active=True, recording=True,
                                          s={"start": 10.0, "accel": 225.0},
                                          on_sample=lambda sample: None)
            sample = {
                "pressure_pa": [0.0] * 5, "temperature_c": [20.0] * 5,
                "valid": [True] * 5, "uptime_ms": 1, "sample_sequence": 1,
                "position_mm": 100.0, "moving": True, "enabled": True,
                "stopped": False, "sensor_test_mode": False,
            }
            self.assertFalse(state.on_telemetry(sample))
            self.assertTrue(state.latest["detection"]["start_ignored"])
            self.assertFalse(state.latest["detection"]["pressure_obstacle"])
            self.assertEqual(state.detector.resets, 1)
            sample = {**sample, "sample_sequence": 2, "position_mm": 400.0}
            self.assertTrue(state.on_telemetry(sample))
            self.assertFalse(state.latest["detection"]["start_ignored"])


if __name__ == "__main__":
    unittest.main()
