"""Configurable five-channel pressure-spike detector with optional Ascend LIF backend."""

from __future__ import annotations

from collections import deque
import math
import time


class PressureSpikeDetector:
    def __init__(self, config: dict):
        self.baseline = [0.0] * 5
        self.voltage = [0.0] * 5
        self.initialized = [False] * 5
        self.spike_history: deque[tuple[float, list[int]]] = deque()
        self.obstacle_until = 0.0
        self.npu = None
        self.backend = "cpu"
        self.update_config(config)

    def _try_npu(self) -> None:
        if not self.config.get("prefer_npu", True) or abs(self.alpha - 0.9) > 1e-7:
            return
        try:
            import whisker_npu

            self.npu = whisker_npu.LIFEngine(0)
            self.backend = "ascend_npu"
        except Exception:
            self.npu = None
            self.backend = "cpu"

    def update_config(self, config: dict) -> None:
        detection = config["detection"]
        self.config = config
        self.alpha = float(detection["alpha"])
        self.thresholds = [float(value) for value in detection["threshold_pa"]]
        self.gains = [float(value) for value in detection["gain"]]
        self.baseline_tau_s = max(0.1, float(detection["baseline_tau_s"]))
        self.channels_required = max(1, min(5, int(detection["channels_required"])))
        self.window_s = max(0.01, float(detection["window_ms"]) / 1000.0)
        self.hold_s = max(0.0, float(detection["hold_ms"]) / 1000.0)
        desired_npu = bool(config.get("prefer_npu", True)) and abs(self.alpha - 0.9) <= 1e-7
        if self.npu is not None and not desired_npu:
            self.npu.close()
            self.npu = None
            self.backend = "cpu"
        elif self.npu is None and desired_npu and hasattr(self, "baseline"):
            self._try_npu()

    def reset_baseline(self) -> None:
        self.initialized = [False] * 5
        self.voltage = [0.0] * 5
        self.spike_history.clear()
        self.obstacle_until = 0.0
        if self.npu is not None:
            self.npu.reset()

    def process(self, pressure: list[float], valid: list[bool], sample_rate_hz: float = 100.0) -> dict:
        now = time.monotonic()
        beta = 1.0 - math.exp(-1.0 / (max(1.0, sample_rate_hz) * self.baseline_tau_s))
        normalized = [0.0] * 5
        deviation = [0.0] * 5
        for index in range(5):
            if not valid[index]:
                continue
            value = float(pressure[index])
            if not self.initialized[index]:
                self.baseline[index] = value
                self.initialized[index] = True
            deviation[index] = abs(value - self.baseline[index]) * self.gains[index]
            normalized[index] = deviation[index] / max(self.thresholds[index], 1e-6)

        if self.npu is not None:
            spikes_raw, voltage_raw = self.npu.infer(normalized)
            spikes = [1 if value >= 0.5 and valid[index] else 0 for index, value in enumerate(spikes_raw)]
            self.voltage = [float(value) for value in voltage_raw]
        else:
            spikes = [0] * 5
            for index in range(5):
                if not valid[index]:
                    self.voltage[index] = 0.0
                    continue
                integrated = self.alpha * self.voltage[index] + normalized[index]
                spikes[index] = int(integrated >= 1.0)
                self.voltage[index] = 0.0 if spikes[index] else integrated

        self.spike_history.append((now, spikes))
        while self.spike_history and now - self.spike_history[0][0] > self.window_s:
            self.spike_history.popleft()
        active_channels = {
            index for _, frame in self.spike_history for index, spike in enumerate(frame) if spike
        }
        if len(active_channels) >= self.channels_required:
            self.obstacle_until = max(self.obstacle_until, now + self.hold_s)
        obstacle = now <= self.obstacle_until

        # Keep the slow baseline from learning an obstacle while it is asserted.
        if not obstacle:
            for index in range(5):
                if valid[index]:
                    self.baseline[index] += beta * (float(pressure[index]) - self.baseline[index])

        return {
            "baseline_pa": self.baseline.copy(),
            "deviation_pa": deviation,
            "normalized": normalized,
            "spikes": spikes,
            "voltage": self.voltage.copy(),
            "active_channels": len(active_channels),
            "obstacle": obstacle,
            "backend": self.backend,
        }
