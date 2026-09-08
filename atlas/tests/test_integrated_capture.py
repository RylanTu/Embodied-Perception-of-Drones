import csv
from pathlib import Path
import tempfile
import unittest

from plot_generator import _smooth_pressure, generate_batch_plot, generate_condition_plot
from service import AppState, CSV_COLUMNS, validate_batch


class IntegratedCaptureTests(unittest.TestCase):
    def test_experiment_preset_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            parameters = {
                "count": 20, "start": 0, "end": 2000, "speed": 1500,
                "accel": 500, "decel": 600, "baseline_wait": 2000,
                "end_dwell": 300, "return_speed": 40, "return_accel": 700,
                "return_decel": 800, "between_wait": 1000, "soft_min": 0,
                "soft_max": 2000, "pulses_per_mm": 80, "max_speed": 2000,
                "label": 1, "obstacle_distance_mm": 1000, "batch_id": "b1",
                "condition": "same", "note": "test",
            }
            AppState(config_path, root / "data").save_experiment_preset("高速有障碍", parameters)
            restored = AppState(config_path, root / "data")
            presets = restored.public_experiment_presets()
            self.assertEqual(presets[0]["name"], "高速有障碍")
            self.assertEqual(presets[0]["parameters"]["speed"], 1500)
            restored.delete_experiment_preset("高速有障碍")
            self.assertEqual(restored.public_experiment_presets(), [])

    def test_plot_filter_preserves_source_and_reduces_impulse_noise(self):
        source = [0.0] * 10 + [8.0] + [0.0] * 10
        filtered = _smooth_pressure(source, {
            "enabled": True, "median_window": 5, "ema_alpha": 0.35,
        })
        self.assertEqual(source[10], 8.0)
        self.assertLess(max(filtered), 0.01)

    def test_plot_filter_can_be_disabled(self):
        source = [0.0, 1.0, 0.0, 2.0]
        self.assertEqual(_smooth_pressure(source, {"enabled": False}), source)

    def test_lidar_and_pressure_need_time_and_position_agreement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = AppState(root / "config.json", root / "data")
            state.config["lidar"]["enabled"] = True
            state.lidar_status["connected"] = True
            state.latest_pressure_event = {"time": 10.0, "position_mm": 800.0}
            state.latest_lidar_event = {"time": 10.5, "position_mm": 900.0,
                                        "confidence": .9}
            result = state._fusion_locked(10.6)
            self.assertEqual(result["state"], "confirmed")
            self.assertTrue(result["obstacle"])
            state.latest_lidar_event["position_mm"] = 1500.0
            result = state._fusion_locked(10.6)
            self.assertEqual(result["state"], "lidar_suspected")
            self.assertFalse(result["obstacle"])

    def test_batch_settings_include_independent_ramps(self):
        settings = validate_batch({
            "ramp_unit": "mm",
            "count": 20, "start": 0, "end": 2000, "speed": 20,
            "accel": 500, "decel": 600, "baseline_wait": 2000, "end_dwell": 300,
            "return_speed": 40, "return_accel": 700, "return_decel": 800,
            "between_wait": 1000, "soft_min": 0, "soft_max": 2000,
            "pulses_per_mm": 80, "max_speed": 200, "label": 1,
            "obstacle_distance_mm": 1000, "batch_id": "b1", "condition": "same", "note": "",
        })
        self.assertEqual(settings["accel"], 500)
        self.assertEqual(settings["return_decel"], 800)

    def test_condition_plot_is_generated_without_extra_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "batch.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for index in range(3):
                    row = {column: 0 for column in CSV_COLUMNS}
                    row.update({"trial_id": "b1_0001", "phase": "measure"})
                    for channel in range(1, 6): row[f"pressure_pa_{channel}"] = index + channel
                    writer.writerow(row)
            output = generate_condition_plot(root)
            self.assertTrue(output.is_file())
            self.assertIn("<svg", output.read_text(encoding="utf-8"))
            batch_output = generate_batch_plot(path)
            self.assertTrue(batch_output.is_file())
            svg = batch_output.read_text(encoding="utf-8")
            self.assertIn("实线=无障碍", svg)
            self.assertIn("zero-phase EMA", svg)


if __name__ == "__main__":
    unittest.main()
