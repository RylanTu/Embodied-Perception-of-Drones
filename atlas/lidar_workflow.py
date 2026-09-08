"""RPLIDAR acquisition/decision state machine extracted from the 45-degree GUI demo.

The module has no GUI or serial dependency.  A serial adapter feeds bytes and an
optional, measured rail-position provider supplies translation.  Decisions always
operate on a frozen set of complete revolutions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
import threading
from typing import Callable, Iterable, Protocol


RPLIDAR_START_SCAN = b"\xA5\x20"
RPLIDAR_STOP = b"\xA5\x25"


@dataclass(frozen=True)
class LidarNode:
    new_scan: bool
    angle_deg: float
    distance_mm: float
    quality: int


class RplidarNodeParser:
    """Incrementally decode standard five-byte RPLIDAR measurement nodes."""

    def __init__(self, maximum_buffer_bytes: int = 65536):
        self.buffer = bytearray()
        self.maximum_buffer_bytes = maximum_buffer_bytes
        self.discarded_bytes = 0
        self.locked = False
        self._last_node: LidarNode | None = None

    @staticmethod
    def parse_node(data: bytes | bytearray) -> LidarNode | None:
        if len(data) != 5:
            return None
        start = data[0] & 1
        inverse_start = (data[0] >> 1) & 1
        # Both complementary start bits and the mandatory angle check bit matter.
        if start == inverse_start or not (data[1] & 1):
            return None
        angle_q6 = (data[1] >> 1) | (data[2] << 7)
        distance_q2 = data[3] | (data[4] << 8)
        angle = angle_q6 / 64.0
        distance = distance_q2 / 4.0
        if not (0.0 <= angle < 360.0) or distance < 0.0:
            return None
        return LidarNode(bool(start), angle, distance, data[0] >> 2)

    def reset(self) -> None:
        self.buffer.clear()
        self.discarded_bytes = 0
        self.locked = False
        self._last_node = None

    @staticmethod
    def _continuous(first: LidarNode, second: LidarNode) -> bool:
        advance = (second.angle_deg - first.angle_deg) % 360.0
        return 0.0 < advance <= 45.0

    def feed(self, data: bytes) -> list[LidarNode]:
        self.buffer.extend(data)
        if len(self.buffer) > self.maximum_buffer_bytes:
            drop = len(self.buffer) - self.maximum_buffer_bytes
            del self.buffer[:drop]
            self.discarded_bytes += drop
        result: list[LidarNode] = []
        while len(self.buffer) >= 5:
            if not self.locked:
                if len(self.buffer) < 10:
                    break
                first = self.parse_node(self.buffer[:5])
                second = self.parse_node(self.buffer[5:10])
                if first is None or second is None or not self._continuous(first, second):
                    del self.buffer[0]
                    self.discarded_bytes += 1
                    continue
                self.locked = True
                result.extend((first, second))
                self._last_node = second
                del self.buffer[:10]
                continue
            node = self.parse_node(self.buffer[:5])
            if node is None or (self._last_node is not None and
                                not self._continuous(self._last_node, node)):
                self.locked = False
                self._last_node = None
                del self.buffer[0]
                self.discarded_bytes += 1
            else:
                result.append(node)
                self._last_node = node
                del self.buffer[:5]
        return result


@dataclass(frozen=True)
class WorldPoint:
    angle_deg: float
    distance_mm: float
    timestamp_s: float
    rail_position_m: float
    x_m: float
    y_m: float
    z_m: float
    quality: int


def to_world(node: LidarNode, timestamp_s: float, rail_position_m: float,
             pitch_deg: float = 45.0) -> WorldPoint:
    """Apply the coordinate convention used by the supplied 45-degree program."""
    angle = math.radians(360.0 - node.angle_deg)
    pitch = math.radians(pitch_deg)
    distance_m = node.distance_mm / 1000.0
    y_local = distance_m * math.sin(angle)
    z_local = distance_m * math.cos(angle)
    return WorldPoint(
        angle_deg=node.angle_deg, distance_mm=node.distance_mm,
        timestamp_s=timestamp_s, rail_position_m=rail_position_m,
        x_m=rail_position_m + math.sin(pitch) * z_local,
        y_m=y_local, z_m=math.cos(pitch) * z_local, quality=node.quality,
    )


class WorkflowState(str, Enum):
    IDLE = "idle"
    WARMING = "warming"
    SAMPLING = "sampling"
    DECIDING = "deciding"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAULT = "fault"


@dataclass(frozen=True)
class AcquisitionConfig:
    pitch_deg: float = 45.0
    minimum_distance_mm: float = 50.0
    maximum_distance_mm: float = 6000.0
    minimum_quality: int = 1
    minimum_points_per_revolution: int = 80
    minimum_angle_coverage_deg: float = 270.0
    warmup_revolutions: int = 2
    sample_revolutions: int = 5
    data_timeout_s: float = 2.0
    session_timeout_s: float = 20.0

    def validate(self) -> None:
        finite = (self.pitch_deg, self.minimum_distance_mm, self.maximum_distance_mm,
                  self.minimum_angle_coverage_deg, self.data_timeout_s,
                  self.session_timeout_s)
        if not all(math.isfinite(value) for value in finite):
            raise ValueError("lidar configuration must be finite")
        if not 0 <= self.minimum_distance_mm < self.maximum_distance_mm:
            raise ValueError("invalid lidar distance range")
        if not 0 <= self.minimum_quality <= 63:
            raise ValueError("minimum quality must be in 0..63")
        if self.minimum_points_per_revolution < 3:
            raise ValueError("too few points per revolution")
        if not 0 < self.minimum_angle_coverage_deg <= 360:
            raise ValueError("invalid angular coverage")
        if self.warmup_revolutions < 0 or self.sample_revolutions < 1:
            raise ValueError("invalid revolution counts")
        if self.data_timeout_s <= 0 or self.session_timeout_s <= self.data_timeout_s:
            raise ValueError("invalid acquisition timeouts")


@dataclass(frozen=True)
class DecisionResult:
    verdict: str
    confidence: float | None
    reason: str
    metrics: dict


class DecisionStrategy(Protocol):
    def decide(self, scans: tuple[tuple[WorldPoint, ...], ...]) -> DecisionResult: ...


@dataclass(frozen=True)
class RoiOccupancyDecision:
    """Transparent geometry baseline; thresholds must be calibrated from labelled data."""
    x_min_m: float
    x_max_m: float
    y_abs_max_m: float
    z_min_m: float
    z_max_m: float
    minimum_hits_per_revolution: int = 3
    minimum_occupied_revolutions: int = 3
    minimum_total_hit_ratio: float = 0.01

    def __post_init__(self) -> None:
        values = (self.x_min_m, self.x_max_m, self.y_abs_max_m,
                  self.z_min_m, self.z_max_m, self.minimum_total_hit_ratio)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("ROI values must be finite")
        if self.x_min_m >= self.x_max_m or self.z_min_m >= self.z_max_m or self.y_abs_max_m <= 0:
            raise ValueError("invalid ROI bounds")
        if self.minimum_hits_per_revolution < 1 or self.minimum_occupied_revolutions < 1:
            raise ValueError("invalid ROI persistence")
        if not 0 < self.minimum_total_hit_ratio <= 1:
            raise ValueError("invalid ROI hit ratio")

    def decide(self, scans: tuple[tuple[WorldPoint, ...], ...]) -> DecisionResult:
        hits = []
        for scan in scans:
            hits.append(sum(self.x_min_m <= p.x_m <= self.x_max_m and
                            abs(p.y_m) <= self.y_abs_max_m and
                            self.z_min_m <= p.z_m <= self.z_max_m for p in scan))
        total = sum(len(scan) for scan in scans)
        ratio = sum(hits) / max(1, total)
        occupied = sum(value >= self.minimum_hits_per_revolution for value in hits)
        obstacle = occupied >= self.minimum_occupied_revolutions and ratio >= self.minimum_total_hit_ratio
        persistence_score = occupied / max(1, self.minimum_occupied_revolutions)
        ratio_score = ratio / self.minimum_total_hit_ratio
        obstacle_support = min(1.0, min(persistence_score, ratio_score))
        confidence = obstacle_support if obstacle else 1.0 - obstacle_support
        return DecisionResult(
            "obstacle" if obstacle else "clear", confidence,
            "ROI occupancy met calibrated persistence thresholds" if obstacle else
            "ROI occupancy did not meet calibrated persistence thresholds",
            {"hits_per_revolution": hits, "occupied_revolutions": occupied,
             "total_points": total, "hit_ratio": ratio},
        )


class StableAngleGate:
    """Optional adapter for a real IMU: trigger once pitch is stable near the target."""
    def __init__(self, target_deg: float = 45.0, tolerance_deg: float = 2.0,
                 hold_s: float = 0.5):
        if not all(math.isfinite(v) for v in (target_deg, tolerance_deg, hold_s)) or \
                not 0 < tolerance_deg <= 180 or hold_s < 0:
            raise ValueError("invalid stable-angle gate")
        self.target_deg = target_deg
        self.tolerance_deg = tolerance_deg
        self.hold_s = hold_s
        self.stable_since: float | None = None
        self.triggered = False

    def reset(self) -> None:
        self.stable_since = None
        self.triggered = False

    def update(self, pitch_deg: float, now_s: float) -> bool:
        if not math.isfinite(pitch_deg) or not math.isfinite(now_s):
            self.stable_since = None
            return False
        angular_error = (pitch_deg - self.target_deg + 180.0) % 360.0 - 180.0
        if abs(angular_error) > self.tolerance_deg:
            self.stable_since = None
            return False
        if self.stable_since is None:
            self.stable_since = now_s
        if not self.triggered and now_s - self.stable_since >= self.hold_s:
            self.triggered = True
            return True
        return False


class LidarDecisionWorkflow:
    """Thread-safe lifecycle from manual/angle trigger to a frozen decision."""

    ACTIVE = {WorkflowState.WARMING, WorkflowState.SAMPLING, WorkflowState.DECIDING}

    def __init__(self, config: AcquisitionConfig | None = None,
                 decision: DecisionStrategy | None = None):
        self.config = config or AcquisitionConfig()
        self.config.validate()
        if (isinstance(decision, RoiOccupancyDecision) and
                decision.minimum_occupied_revolutions > self.config.sample_revolutions):
            raise ValueError("ROI persistence exceeds captured revolutions")
        self.decision = decision
        self.parser = RplidarNodeParser()
        self.lock = threading.RLock()
        self.state = WorkflowState.IDLE
        self.result: DecisionResult | None = None
        self.error = ""
        self.started_at: float | None = None
        self.last_data_at: float | None = None
        self.warmup_completed = 0
        self.rejected_revolutions = 0
        self.scans: list[tuple[WorldPoint, ...]] = []
        self.current_scan: list[WorldPoint] = []

    def start(self, now_s: float) -> bytes:
        if not math.isfinite(now_s):
            raise ValueError("start time must be finite")
        with self.lock:
            if self.state in self.ACTIVE:
                raise RuntimeError("lidar acquisition is already active")
            self.parser.reset()
            self.state = WorkflowState.WARMING if self.config.warmup_revolutions else WorkflowState.SAMPLING
            self.result = None; self.error = ""; self.started_at = now_s; self.last_data_at = now_s
            self.warmup_completed = 0; self.rejected_revolutions = 0
            self.scans.clear(); self.current_scan.clear()
            return RPLIDAR_START_SCAN

    def stop(self) -> bytes:
        with self.lock:
            if self.state in self.ACTIVE:
                self.state = WorkflowState.STOPPED
            self.current_scan.clear()
            return RPLIDAR_STOP

    @staticmethod
    def _angle_coverage(points: Iterable[WorldPoint]) -> float:
        angles = sorted({point.angle_deg % 360.0 for point in points})
        if len(angles) < 2:
            return 0.0
        gaps = [b-a for a, b in zip(angles, angles[1:])]
        gaps.append(angles[0] + 360.0 - angles[-1])
        return 360.0 - max(gaps)

    def _valid_revolution(self, points: list[WorldPoint]) -> bool:
        return (len(points) >= self.config.minimum_points_per_revolution and
                self._angle_coverage(points) >= self.config.minimum_angle_coverage_deg)

    def _finish_revolution(self) -> None:
        points = self.current_scan
        self.current_scan = []
        if not self._valid_revolution(points):
            self.rejected_revolutions += 1
            return
        frozen = tuple(points)
        if self.state == WorkflowState.WARMING:
            self.warmup_completed += 1
            if self.warmup_completed >= self.config.warmup_revolutions:
                self.state = WorkflowState.SAMPLING
            return
        if self.state != WorkflowState.SAMPLING:
            return
        self.scans.append(frozen)
        if len(self.scans) < self.config.sample_revolutions:
            return
        self.state = WorkflowState.DECIDING
        snapshot = tuple(self.scans)
        try:
            self.result = (self.decision.decide(snapshot) if self.decision else
                           DecisionResult("undetermined", None,
                                          "samples complete; no calibrated decision strategy configured",
                                          {"revolutions": len(snapshot),
                                           "points": sum(map(len, snapshot))}))
            self.state = WorkflowState.COMPLETE
        except Exception as exc:
            self.error = f"decision failed: {exc}"
            self.state = WorkflowState.FAULT

    def feed(self, data: bytes, now_s: float,
             rail_position_m: float | Callable[[float], float] = 0.0) -> int:
        if not math.isfinite(now_s):
            raise ValueError("sample time must be finite")
        with self.lock:
            nodes = self.parser.feed(data)
            if self.state not in (WorkflowState.WARMING, WorkflowState.SAMPLING):
                return 0
            if nodes:
                self.last_data_at = now_s
            accepted = 0
            for node in nodes:
                if node.new_scan and self.current_scan:
                    self._finish_revolution()
                    if self.state not in (WorkflowState.WARMING, WorkflowState.SAMPLING):
                        break
                position = rail_position_m(now_s) if callable(rail_position_m) else rail_position_m
                if not math.isfinite(position):
                    self.error = "rail position is not finite"
                    self.state = WorkflowState.FAULT
                    break
                if (self.config.minimum_distance_mm < node.distance_mm < self.config.maximum_distance_mm
                        and node.quality >= self.config.minimum_quality):
                    self.current_scan.append(to_world(node, now_s, position, self.config.pitch_deg))
                    accepted += 1
            return accepted

    def poll(self, now_s: float) -> WorkflowState:
        if not math.isfinite(now_s):
            raise ValueError("poll time must be finite")
        with self.lock:
            if self.state in (WorkflowState.WARMING, WorkflowState.SAMPLING):
                if self.last_data_at is not None and now_s - self.last_data_at > self.config.data_timeout_s:
                    self.error = "lidar data timeout"
                    self.state = WorkflowState.FAULT
                elif self.started_at is not None and now_s - self.started_at > self.config.session_timeout_s:
                    self.error = "lidar acquisition session timeout"
                    self.state = WorkflowState.FAULT
            return self.state

    def public_status(self) -> dict:
        with self.lock:
            return {"state": self.state.value, "warmup_completed": self.warmup_completed,
                    "warmup_required": self.config.warmup_revolutions,
                    "sampled_revolutions": len(self.scans),
                    "sample_revolutions_required": self.config.sample_revolutions,
                    "current_points": len(self.current_scan),
                    "rejected_revolutions": self.rejected_revolutions,
                    "discarded_bytes": self.parser.discarded_bytes,
                    "error": self.error,
                    "decision": asdict(self.result) if self.result else None}
