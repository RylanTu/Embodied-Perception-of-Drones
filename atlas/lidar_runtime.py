"""Continuous RPLIDAR reader and full-route obstacle candidate detector.

The serial protocol and coordinate convention intentionally match the supplied
45-degree demo.  This module adds only service concerns: monotonic timestamps,
measured rail-position interpolation, complete-revolution validation and
multi-revolution persistence.
"""
from __future__ import annotations

from collections import Counter, deque
import copy
import math
import threading
import time
from typing import Callable

from lidar_workflow import (RPLIDAR_START_SCAN, RPLIDAR_STOP, RplidarNodeParser,
                            WorldPoint, to_world)


class PositionTimeline:
    """Thread-safe interpolation of ESP32 rail positions in monotonic time."""

    def __init__(self, maximum_samples: int = 4096):
        self.samples: deque[tuple[float, float]] = deque(maxlen=maximum_samples)
        self.lock = threading.RLock()

    def add(self, timestamp_s: float, position_mm: float) -> None:
        if not math.isfinite(timestamp_s) or not math.isfinite(position_mm):
            return
        with self.lock:
            if self.samples and timestamp_s < self.samples[-1][0]:
                return
            self.samples.append((timestamp_s, position_mm / 1000.0))

    def position_m_at(self, timestamp_s: float) -> float:
        with self.lock:
            values = tuple(self.samples)
        if not values:
            raise RuntimeError("尚未收到ESP32滑台位置")
        if timestamp_s <= values[0][0]:
            return values[0][1]
        if timestamp_s >= values[-1][0]:
            return values[-1][1]
        lo, hi = 0, len(values) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if values[mid][0] <= timestamp_s:
                lo = mid
            else:
                hi = mid
        left_t, left_p = values[lo]
        right_t, right_p = values[hi]
        ratio = (timestamp_s - left_t) / max(1e-9, right_t - left_t)
        return left_p + ratio * (right_p - left_p)


class FullRouteCandidateDetector:
    """Find corridor clusters anywhere on the configured slide route.

    A candidate must recur in neighbouring X bins across several complete
    revolutions.  This intentionally produces a *candidate*, not a final safety
    verdict; the service combines it with the pressure detector.
    """

    def __init__(self, config: dict):
        self.history: deque[set[int]] = deque()
        self.last_result = self.empty_result("等待完整雷达扫描")
        self.update_config(config)

    @staticmethod
    def empty_result(reason: str) -> dict:
        return {"obstacle": False, "candidate_position_mm": None,
                "confidence": None, "reason": reason,
                "candidate_points": 0, "persistent_revolutions": 0}

    def update_config(self, config: dict) -> None:
        self.config = copy.deepcopy(config)
        self.x_min_m = float(config["route_min_mm"]) / 1000.0
        self.x_max_m = float(config["route_max_mm"]) / 1000.0
        self.y_abs_max_m = float(config["corridor_half_width_mm"]) / 1000.0
        self.z_min_m = float(config["height_min_mm"]) / 1000.0
        self.z_max_m = float(config["height_max_mm"]) / 1000.0
        self.bin_size_m = float(config["cluster_bin_mm"]) / 1000.0
        self.minimum_points = int(config["cluster_min_points"])
        self.persistence = int(config["persistence_revolutions"])
        self.history_revolutions = max(self.persistence, int(config["history_revolutions"]))
        self.history = deque(self.history, maxlen=self.history_revolutions)

    def reset(self) -> None:
        self.history.clear()
        self.last_result = self.empty_result("等待完整雷达扫描")

    def process(self, points: tuple[WorldPoint, ...]) -> dict:
        counts: Counter[int] = Counter()
        for point in points:
            if (self.x_min_m <= point.x_m <= self.x_max_m and
                    abs(point.y_m) <= self.y_abs_max_m and
                    self.z_min_m <= point.z_m <= self.z_max_m):
                counts[math.floor((point.x_m - self.x_min_m) / self.bin_size_m)] += 1
        occupied = {index for index, count in counts.items() if count >= self.minimum_points}
        self.history.append(occupied)
        if len(self.history) < self.persistence:
            self.last_result = self.empty_result("正在积累连续雷达扫描")
            return copy.deepcopy(self.last_result)

        support: Counter[int] = Counter()
        for current in self.history:
            # One-bin tolerance absorbs position and scan timing jitter.
            for index in current:
                support[index] += 1
                support[index - 1] += 0.25
                support[index + 1] += 0.25
        if not support:
            self.last_result = self.empty_result("全行程走廊内没有持续点簇")
            return copy.deepcopy(self.last_result)
        best_bin, score = max(support.items(), key=lambda item: item[1])
        persistent = sum(any(abs(index - best_bin) <= 1 for index in current)
                         for current in self.history)
        candidate_points = sum(count for index, count in counts.items()
                               if abs(index - best_bin) <= 1)
        obstacle = persistent >= self.persistence
        confidence = min(1.0, persistent / max(1, self.persistence))
        position = (self.x_min_m + (best_bin + 0.5) * self.bin_size_m) * 1000.0
        self.last_result = {
            "obstacle": obstacle,
            "candidate_position_mm": round(position, 3) if obstacle else None,
            "confidence": confidence if obstacle else 1.0 - confidence,
            "reason": "全行程走廊内检测到连续点簇" if obstacle else "点簇连续性不足",
            "candidate_points": candidate_points,
            "persistent_revolutions": persistent,
        }
        return copy.deepcopy(self.last_result)


class ContinuousLidarAnalyzer:
    """Convert a continuous byte stream into synchronized complete scans."""

    def __init__(self, config: dict, position_provider: Callable[[float], float],
                 scan_callback: Callable[[tuple[WorldPoint, ...], dict], None] | None = None):
        self.parser = RplidarNodeParser()
        self.position_provider = position_provider
        self.scan_callback = scan_callback
        self.current: list[WorldPoint] = []
        self.revolutions = 0
        self.rejected_revolutions = 0
        self.last_node_time: float | None = None
        self.lock = threading.RLock()
        self.update_config(config)

    def update_config(self, config: dict) -> None:
        with self.lock:
            self.config = copy.deepcopy(config)
            self.pitch_deg = float(config["pitch_deg"])
            self.minimum_distance_mm = float(config["minimum_distance_mm"])
            self.maximum_distance_mm = float(config["maximum_distance_mm"])
            self.minimum_quality = int(config["minimum_quality"])
            self.minimum_points = int(config["minimum_points_per_revolution"])
            self.minimum_coverage = float(config["minimum_angle_coverage_deg"])
            self.node_interval_s = 50.0 / float(config["baud"])
            if hasattr(self, "detector"):
                self.detector.update_config(config)
            else:
                self.detector = FullRouteCandidateDetector(config)

    def reset(self) -> None:
        with self.lock:
            self.parser.reset()
            self.current.clear()
            self.detector.reset()
            self.revolutions = 0
            self.rejected_revolutions = 0
            self.last_node_time = None

    @staticmethod
    def _coverage(points: list[WorldPoint]) -> float:
        angles = sorted({point.angle_deg % 360.0 for point in points})
        if len(angles) < 2:
            return 0.0
        gaps = [right - left for left, right in zip(angles, angles[1:])]
        gaps.append(angles[0] + 360.0 - angles[-1])
        return 360.0 - max(gaps)

    def _finish_scan(self) -> None:
        points = tuple(self.current)
        self.current.clear()
        if len(points) < self.minimum_points or self._coverage(list(points)) < self.minimum_coverage:
            self.rejected_revolutions += 1
            return
        self.revolutions += 1
        result = self.detector.process(points)
        if self.scan_callback:
            self.scan_callback(points, result)

    def feed(self, data: bytes, received_at_s: float) -> int:
        with self.lock:
            nodes = self.parser.feed(data)
            if not nodes:
                return 0
            first_time = received_at_s - self.node_interval_s * (len(nodes) - 1)
            if self.last_node_time is not None:
                first_time = max(first_time, self.last_node_time + 1e-6)
            accepted = 0
            for index, node in enumerate(nodes):
                node_time = min(received_at_s, first_time + index * self.node_interval_s)
                self.last_node_time = node_time
                if node.new_scan and self.current:
                    self._finish_scan()
                if not (self.minimum_distance_mm < node.distance_mm < self.maximum_distance_mm and
                        node.quality >= self.minimum_quality):
                    continue
                try:
                    rail_position_m = self.position_provider(node_time)
                except RuntimeError:
                    continue
                self.current.append(to_world(node, node_time, rail_position_m, self.pitch_deg))
                accepted += 1
            return accepted

    def status(self) -> dict:
        with self.lock:
            result = copy.deepcopy(self.detector.last_result)
            return {"revolutions": self.revolutions,
                    "rejected_revolutions": self.rejected_revolutions,
                    "current_points": len(self.current),
                    "discarded_bytes": self.parser.discarded_bytes,
                    "detection": result}


class LidarController(threading.Thread):
    """Long-running serial owner for the independent RPLIDAR USB link."""

    def __init__(self, state):
        super().__init__(name="lidar-controller", daemon=True)
        self.state = state
        self.stop_event = threading.Event()
        self.reopen_event = threading.Event()
        self.serial = None
        self.analyzer = ContinuousLidarAnalyzer(
            state.config["lidar"], state.position_timeline.position_m_at,
            state.on_lidar_scan)

    def reconfigure(self) -> None:
        self.reopen_event.set()
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass

    def _publish(self, connected: bool, error: str = "") -> None:
        status = self.analyzer.status()
        status.update({"enabled": bool(self.state.config["lidar"]["enabled"]),
                       "connected": connected, "error": error})
        self.state.on_lidar_status(status)

    def run(self) -> None:
        try:
            import serial
        except ImportError:
            self._publish(False, "缺少pyserial，请安装requirements.txt")
            return
        while not self.stop_event.is_set():
            with self.state.lock:
                settings = copy.deepcopy(self.state.config["lidar"])
            if not settings["enabled"]:
                self._publish(False)
                self.reopen_event.wait(.5)
                self.reopen_event.clear()
                continue
            try:
                self.analyzer.update_config(settings)
                self.analyzer.reset()
                self.serial = serial.Serial(settings["port"], int(settings["baud"]),
                                            timeout=.05, write_timeout=.2)
                self.serial.write(RPLIDAR_STOP)
                time.sleep(.2)
                self.serial.reset_input_buffer()
                self.serial.write(RPLIDAR_START_SCAN)
                self.reopen_event.clear()
                last_publish = 0.0
                while not self.stop_event.is_set() and not self.reopen_event.is_set():
                    raw = self.serial.read(max(512, int(getattr(self.serial, "in_waiting", 0))))
                    now = time.monotonic()
                    if raw:
                        self.analyzer.feed(raw, now)
                    if now - last_publish >= .25:
                        self._publish(True)
                        last_publish = now
            except Exception as exc:
                self._publish(False, str(exc))
                self.reopen_event.wait(1.0)
                self.reopen_event.clear()
            finally:
                if self.serial:
                    try:
                        self.serial.write(RPLIDAR_STOP)
                        self.serial.close()
                    except Exception:
                        pass
                self.serial = None
