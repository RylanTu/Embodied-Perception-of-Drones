from pathlib import Path
import tempfile
import unittest

from service import AppState


class IntegratedControllerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
