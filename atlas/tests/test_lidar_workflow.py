import math
import unittest

from lidar_workflow import (AcquisitionConfig, LidarDecisionWorkflow, RoiOccupancyDecision,
                            RPLIDAR_START_SCAN, RPLIDAR_STOP, RplidarNodeParser,
                            StableAngleGate, WorkflowState, to_world)


def node(angle, distance=1000, start=False, quality=20):
    aq = round(angle * 64)
    dq = round(distance * 4)
    return bytes([(quality << 2) | int(start) | (int(not start) << 1),
                  ((aq & 0x7f) << 1) | 1, (aq >> 7) & 0xff,
                  dq & 0xff, (dq >> 8) & 0xff])


def revolution(start_angle=0, distance=1000):
    return b''.join(node(angle, distance, start=(index == 0))
                    for index, angle in enumerate(range(start_angle, 360, 10)))


class LidarWorkflowTests(unittest.TestCase):
    def test_fragmentation_noise_and_check_bit(self):
        parser = RplidarNodeParser()
        good = node(90, 1234, True)
        following = node(91, 1200, False)
        broken = bytearray(node(30)); broken[1] &= 0xfe
        self.assertEqual(parser.feed(b'junk' + bytes(broken) + good[:2]), [])
        result = parser.feed(good[2:] + following)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0].new_scan)
        self.assertAlmostEqual(result[0].angle_deg, 90)
        self.assertAlmostEqual(result[0].distance_mm, 1234)
        self.assertGreater(parser.discarded_bytes, 0)

    def test_reference_45_degree_transform(self):
        point = to_world(RplidarNodeParser.parse_node(node(0, 1000, True)), 2.0, .5, 45)
        self.assertAlmostEqual(point.x_m, .5 + math.sqrt(.5), places=6)
        self.assertAlmostEqual(point.y_m, 0, places=6)
        self.assertAlmostEqual(point.z_m, math.sqrt(.5), places=6)

    def test_warmup_sampling_decision_and_frozen_snapshot(self):
        config = AcquisitionConfig(warmup_revolutions=2, sample_revolutions=3,
            minimum_points_per_revolution=30, minimum_angle_coverage_deg=300)
        decision = RoiOccupancyDecision(.65, .76, .2, .65, .76,
            minimum_hits_per_revolution=1, minimum_occupied_revolutions=3,
            minimum_total_hit_ratio=.02)
        flow = LidarDecisionWorkflow(config, decision)
        self.assertEqual(flow.start(0), RPLIDAR_START_SCAN)
        # Six start markers finish five revolutions: two warmup + three samples.
        stream = b''.join(revolution() for _ in range(6))
        for offset in range(0, len(stream), 17):
            flow.feed(stream[offset:offset+17], 1 + offset / len(stream))
        self.assertEqual(flow.state, WorkflowState.COMPLETE)
        self.assertEqual(len(flow.scans), 3)
        self.assertEqual(flow.result.verdict, 'obstacle')
        frozen_count = sum(map(len, flow.scans))
        flow.feed(revolution(), 3)
        self.assertEqual(sum(map(len, flow.scans)), frozen_count)

    def test_bad_revolution_is_rejected_not_counted(self):
        flow = LidarDecisionWorkflow(AcquisitionConfig(warmup_revolutions=0,
            sample_revolutions=1, minimum_points_per_revolution=20,
            minimum_angle_coverage_deg=270))
        flow.start(0)
        short_scan = b''.join(node(angle, start=(angle in (0, 110)))
                              for angle in range(0, 120, 10))
        flow.feed(short_scan, .1)
        self.assertEqual(flow.state, WorkflowState.SAMPLING)
        self.assertEqual(flow.public_status()['rejected_revolutions'], 1)

    def test_timeout_stop_and_restart(self):
        flow = LidarDecisionWorkflow(AcquisitionConfig(data_timeout_s=1,
            session_timeout_s=5))
        flow.start(0)
        self.assertEqual(flow.poll(1.01), WorkflowState.FAULT)
        self.assertIn('timeout', flow.error)
        self.assertEqual(flow.start(2), RPLIDAR_START_SCAN)
        self.assertEqual(flow.stop(), RPLIDAR_STOP)
        self.assertEqual(flow.state, WorkflowState.STOPPED)
        self.assertEqual(flow.start(3), RPLIDAR_START_SCAN)

    def test_angle_gate_requires_continuous_stability_and_is_one_shot(self):
        gate = StableAngleGate(45, 2, .5)
        self.assertFalse(gate.update(44, 0))
        self.assertFalse(gate.update(48, .4))
        self.assertFalse(gate.update(46, 1))
        self.assertTrue(gate.update(45, 1.5))
        self.assertFalse(gate.update(45, 2))
        gate.reset()
        self.assertFalse(gate.update(45, 3))
        wrapped = StableAngleGate(359, 2, 0)
        self.assertTrue(wrapped.update(1, 4))

    def test_no_detector_returns_undetermined_not_fake_clear(self):
        flow = LidarDecisionWorkflow(AcquisitionConfig(warmup_revolutions=0,
            sample_revolutions=1, minimum_points_per_revolution=30,
            minimum_angle_coverage_deg=300))
        flow.start(0)
        flow.feed(revolution() + node(0, start=True), .5)
        self.assertEqual(flow.result.verdict, 'undetermined')


if __name__ == '__main__': unittest.main()
