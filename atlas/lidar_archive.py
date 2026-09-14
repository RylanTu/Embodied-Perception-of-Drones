"""Append-only, bounded-file storage of accepted full-resolution world scans."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid


COLUMNS = ["scan_id", "received_utc", "host_monotonic_s", "rail_position_mm",
           "angle_deg", "distance_mm", "quality", "x_m", "y_m", "z_m"]


class LidarArchive:
    def __init__(self, data_dir: Path, chunk_points: int = 100000):
        self.root = data_dir / "lidar_archive"
        self.chunk_points = chunk_points
        self.lock = threading.Lock()
        self.session = None
        self.config_key = None
        self.path = None
        self.part = self.part_points = self.points = self.scans = 0
        self.error = ""

    def append(self, points, config):
        if not points:
            return
        with self.lock:
            # Latch storage failures: never silently resume a discontinuous archive.
            if self.error:
                return
            try:
                key = json.dumps(config, sort_keys=True)
                if self.session is None or key != self.config_key:
                    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
                    self.session = self.root / name
                    self.session.mkdir(parents=True, exist_ok=False)
                    metadata = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                                "lidar_config": config, "coordinates": "XYZ metres; pulse-derived slide position, not encoder feedback",
                                "scope": "Accepted complete scans only, before preview decimation; no automatic deletion",
                                "timestamp": "Host estimated monotonic seconds; not hardware acquisition timestamps",
                                "columns": COLUMNS}
                    with (self.session / "metadata.json").open("x", encoding="utf-8") as stream:
                        json.dump(metadata, stream, ensure_ascii=False, indent=2)
                    self.config_key = key
                    self.path = None
                    self.part = self.part_points = 0
                if self.path is None or self.part_points >= self.chunk_points:
                    self.part += 1
                    self.path = self.session / f"points_{self.part:06d}.csv"
                    with self.path.open("x", newline="", encoding="utf-8") as stream:
                        csv.writer(stream).writerow(COLUMNS)
                    self.part_points = 0
                received = datetime.now(timezone.utc).isoformat()
                # Close each scan: no application-buffered tail on normal shutdown.
                with self.path.open("a", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerows((self.scans + 1, received, p.timestamp_s, p.rail_position_m * 1000,
                                      p.angle_deg, p.distance_mm, p.quality, p.x_m, p.y_m, p.z_m)
                                     for p in points)
                self.scans += 1
                self.points += len(points)
                self.part_points += len(points)
            except (OSError, ValueError, TypeError) as exc:
                self.error = f"点云保存失败，记录已停止：{exc}；请检查磁盘后重启服务（失败文件可能不完整）"

    def snapshot(self):
        with self.lock:
            return {"saved_points": self.points, "saved_scans": self.scans, "error": self.error,
                    "current_file": str(self.path.relative_to(self.root.parent)).replace("\\", "/") if self.path else None}
