import unittest

from lidar_runtime import FullRouteCandidateDetector, PositionTimeline
from lidar_workflow import WorldPoint


def point(x_m, y_m=0.0, z_m=0.5):
    return WorldPoint(angle_deg=0.0, distance_mm=1000.0, timestamp_s=1.0,
                      rail_position_m=0.0, x_m=x_m, y_m=y_m, z_m=z_m,
                      quality=20)


def detector_config():
    return {
        "route_min_mm": 0.0, "route_max_mm": 2000.0,
        "corridor_half_width_mm": 300.0,
        "height_min_mm": 0.0, "height_max_mm": 1000.0,
        "cluster_bin_mm": 50.0, "cluster_min_points": 3,
        "persistence_revolutions": 3, "history_revolutions": 4,
    }


class LidarRuntimeTests(unittest.TestCase):
    def test_position_timeline_interpolates_esp_measurements(self):
        timeline = PositionTimeline()
        timeline.add(10.0, 100.0)
        timeline.add(12.0, 500.0)
        self.assertAlmostEqual(timeline.position_m_at(11.0), 0.3)
        self.assertAlmostEqual(timeline.position_m_at(9.0), 0.1)
        self.assertAlmostEqual(timeline.position_m_at(13.0), 0.5)

    def test_position_timeline_requires_real_esp_sample(self):
        with self.assertRaisesRegex(RuntimeError, "ESP32"):
            PositionTimeline().position_m_at(1.0)

    def test_candidate_can_be_found_anywhere_on_route(self):
        detector = FullRouteCandidateDetector(detector_config())
        near_start = tuple(point(0.125 + offset * .001) for offset in range(4))
        for _ in range(3):
            result = detector.process(near_start)
        self.assertTrue(result["obstacle"])
        self.assertLess(result["candidate_position_mm"], 300)

        detector.reset()
        near_end = tuple(point(1.825 + offset * .001) for offset in range(4))
        for _ in range(3):
            result = detector.process(near_end)
        self.assertTrue(result["obstacle"])
        self.assertGreater(result["candidate_position_mm"], 1700)

    def test_outside_corridor_is_not_a_candidate(self):
        detector = FullRouteCandidateDetector(detector_config())
        outside = tuple(point(1.0, y_m=.8) for _ in range(10))
        for _ in range(4):
            result = detector.process(outside)
        self.assertFalse(result["obstacle"])

    def test_candidate_requires_multiple_revolutions(self):
        detector = FullRouteCandidateDetector(detector_config())
        scan = tuple(point(.8) for _ in range(4))
        self.assertFalse(detector.process(scan)["obstacle"])
        self.assertFalse(detector.process(scan)["obstacle"])
        self.assertTrue(detector.process(scan)["obstacle"])


if __name__ == "__main__":
    unittest.main()
