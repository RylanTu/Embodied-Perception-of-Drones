from pathlib import Path
import tempfile
import unittest
from service import AppState
from lidar_workflow import WorldPoint


class LidarMapTests(unittest.TestCase):
    def test_map_keeps_bounded_snapshot_and_clear_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AppState(Path(directory) / 'config.json', Path(directory) / 'data')
            point = WorldPoint(90, 1000, 1, .1, .1, 1, .2, 20)
            points = (point,) * 1000
            for _ in range(101):
                state.on_lidar_scan(points, {'obstacle': False})
            snapshot = state.public_lidar_map()
            self.assertEqual(len(snapshot['points']), 100000)
            self.assertEqual(snapshot['preview_limit'], 100000)
            self.assertEqual(len(snapshot['scan']), 1000)
            self.assertEqual(snapshot['points'][0], [.1, 1, .2])
            state.clear_lidar_map()
            self.assertEqual(state.public_lidar_map()['points'], [])
            self.assertIsNone(state.public_lidar_map()['age_s'])
            self.assertEqual(len(snapshot['points']), 100000)
            self.assertEqual(len(points), 1000)
            self.assertEqual(state.public_lidar_map()['archive']['saved_points'], 101000)
