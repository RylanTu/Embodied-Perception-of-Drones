import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from export_lidar_ply import export_ply
from lidar_archive import LidarArchive
from lidar_workflow import WorldPoint


class LidarArchiveTests(unittest.TestCase):
    def test_chunk_restart_config_change_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = LidarArchive(root, chunk_points=3)
            points = (WorldPoint(90, 1000, 1.25, .1, .1, 1, .2, 20),) * 4
            archive.append(points, {"pitch_deg": 45})
            first_session = archive.session
            archive.append(points, {"pitch_deg": 45})
            files = sorted(first_session.glob("points_*.csv"))
            self.assertEqual(len(files), 2)
            with files[0].open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual(float(rows[0]['rail_position_mm']), 100)
            self.assertEqual(float(rows[0]['host_monotonic_s']), 1.25)
            output = root / "cloud.ply"
            self.assertEqual(export_ply(first_session, output), 8)
            self.assertIn("element vertex 8\n", output.read_text())
            with self.assertRaises(FileExistsError):
                export_ply(first_session, output)
            archive.append(points, {"pitch_deg": 30})
            self.assertNotEqual(archive.session, first_session)
            self.assertEqual(json.loads((archive.session / 'metadata.json').read_text())['lidar_config']['pitch_deg'], 30)
            restarted = LidarArchive(root)
            restarted.append(points, {"pitch_deg": 30})
            self.assertNotEqual(restarted.session, archive.session)
            self.assertTrue(files[0].is_file())
            self.assertEqual(archive.snapshot()['saved_points'], 12)

    def test_storage_error_visible_and_latched(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = LidarArchive(Path(directory))
            points = [WorldPoint(90, 1000, 1, .1, .1, 1, .2, 20)]
            with patch.object(Path, 'mkdir', side_effect=OSError('disk full')):
                archive.append(points, {})
            archive.append(points, {})
            self.assertIn('disk full', archive.snapshot()['error'])
            self.assertEqual(archive.snapshot()['saved_points'], 0)

    def test_empty_scan_does_not_create_session(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = LidarArchive(Path(directory))
            archive.append([], {})
            self.assertFalse(archive.root.exists())
