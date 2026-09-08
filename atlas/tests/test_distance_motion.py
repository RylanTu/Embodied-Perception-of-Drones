import io
import csv
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from protocol import MOVE_DISTANCE, MOVE_ABSOLUTE, MOVE_PAYLOAD, move_distance_payload, move_payload
from service import BatchRunner, CSV_COLUMNS, validate_batch, wait_state_not_moving


def settings(**changes):
    value = dict(ramp_unit="mm", count=2, start=10, end=1900, speed=1500,
                 accel=225, decel=75, return_speed=1000, return_accel=150,
                 return_decel=150, baseline_wait=2000, end_dwell=300,
                 between_wait=1000, soft_min=0, soft_max=2000, pulses_per_mm=80,
                 max_speed=2000, label=1, obstacle_distance_mm=1000,
                 batch_id="test", condition="test", note="")
    value.update(changes)
    return value


class DistanceMotionTests(unittest.TestCase):
    def test_distance_payload_and_legacy_are_not_confused(self):
        self.assertNotEqual(MOVE_DISTANCE, MOVE_ABSOLUTE)
        self.assertEqual(MOVE_PAYLOAD.unpack(move_distance_payload(1900, 1500, 225.125, 75)),
                         (1900000, 1500000, 225125, 75000))
        self.assertEqual(MOVE_PAYLOAD.unpack(move_payload(1900, 1500, 300, 100)),
                         (1900000, 1500000, 300, 100))

    def test_reject_invalid_distances_and_overflow(self):
        for value in [-1, float("nan"), float("inf"), 4294968, 1e308]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                move_distance_payload(10, 1000, value, 150)
        for speed in [0, .0001, -1]:
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                move_distance_payload(10, speed, 150, 150)

    def test_distance_params_survive_without_integer_truncation(self):
        result = validate_batch(settings(accel=225.125))
        self.assertEqual(result["accel"], 225.125)
        self.assertEqual(result["decel"], 75)
        self.assertEqual(result["return_accel"], 150)
        self.assertEqual(result["ramp_unit"], "mm")

    def test_old_presets_convert_once(self):
        old = settings(accel=300, decel=100, return_accel=300, return_decel=300)
        del old["ramp_unit"]
        result = validate_batch(old)
        self.assertEqual(result["accel"], 225.375)
        self.assertEqual(result["decel"], 75.125)
        self.assertEqual(result["return_accel"], 150.375)
        self.assertEqual(validate_batch(result), result)
        self.assertEqual(old["accel"], 300)

    def test_invalid_batch_units_or_negative_distances(self):
        for change in [dict(accel=-1), dict(return_decel=-1), dict(ramp_unit="unknown")]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_batch(settings(**change))

    def make_runner(self, at_target=False):
        state = SimpleNamespace(lock=threading.RLock(), last_telemetry_time=100.,
            latest=dict(position_mm=10 if at_target else 0, moving=False, enabled=True, stopped=False))
        sent = []
        runner = BatchRunner(state, SimpleNamespace(command=lambda *args: sent.append(args)), settings())
        return runner, state, sent

    def exercise_arrival(self, at_target=False):
        runner, state, sent = self.make_runner(at_target)
        now = [100.]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
            state.last_telemetry_time = now[0]
            state.latest.update(position_mm=10, moving=False)
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
        with patch("service.time", clock): runner.move(10, 1000, 150, 150)
        self.assertEqual(len(sent), 1)  # No second press/command needed.
        self.assertEqual(sent[0][0], MOVE_DISTANCE)
        self.assertEqual(len(sleeps), 1)  # Must see a NEW frame even if already at target.

    def test_first_short_move_completes_without_any_moving_frame(self):
        self.exercise_arrival()

    def test_noop_target_does_not_accept_old_telemetry(self):
        self.exercise_arrival(at_target=True)

    def test_lost_telemetry_is_not_success(self):
        runner, state, _ = self.make_runner(at_target=True)
        now = [100.]
        def sleep(seconds): now[0] += seconds
        with patch("service.time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)):
            with self.assertRaisesRegex(RuntimeError, "数据中断"): runner.move(10, 1000, 150, 150)

    def test_stopped_or_disabled_at_target_is_not_success(self):
        for values in [dict(stopped=True), dict(enabled=False)]:
            runner, state, _ = self.make_runner(at_target=True)
            state.latest.update(values)
            with patch("service.time", SimpleNamespace(monotonic=lambda: 100.)):
                with self.assertRaisesRegex(RuntimeError, "停止锁定或失能"):
                    runner.wait_position(10, 1000, after=99)

    def test_batch_csv_marks_distance_units(self):
        runner, state, _ = self.make_runner()
        output = io.StringIO()
        runner.writer = csv.writer(output)
        runner.writer.writerow(CSV_COLUMNS)
        runner.recording = True
        sample = dict(state.latest, sample_sequence=1, uptime_ms=1, host_time="test",
                      pressure_pa=[1]*5, temperature_c=[20]*5, valid_mask=31, sensor_test_mode=False)
        runner.on_sample(sample)
        row = next(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(row["schema_version"], "6")
        self.assertEqual(row["accel_mm"], "225")
        self.assertNotIn("accel_ms", row)

    def test_stop_wait_ignores_old_stationary_frame(self):
        _, state, _ = self.make_runner(at_target=True)
        now = [100.]
        waits = []
        def sleep(seconds):
            now[0] += seconds
            waits.append(seconds)
            state.last_telemetry_time = now[0]
        with patch("service.time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)):
            wait_state_not_moving(state)
        self.assertEqual(len(waits), 1)


if __name__ == "__main__": unittest.main()
