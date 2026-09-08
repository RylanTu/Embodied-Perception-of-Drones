"""Optional Ascend OM ramp classifier with the existing rule detector as fallback."""

from __future__ import annotations

from collections import deque
import math
from pathlib import Path
import threading
import time

from detector import PressureSpikeDetector


class HybridDetector:
    def __init__(self, config: dict, root: Path):
        self.root = root
        self.rule = PressureSpikeDetector(config)
        self.samples: deque[list[float]] = deque()
        self.session = None
        self.model_error = ""
        self.obstacle_until = 0.0
        self.loaded_path = ""
        self.session_thread_id = None
        self.reload_pending = False
        self.update_config(config)

    def _load_model(self) -> None:
        self.session = None
        self.session_thread_id = None
        self.loaded_path = ""
        self.model_error = ""
        self.reload_pending = False
        if not self.enabled:
            return
        path = Path(self.model_path)
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            self.model_error = f"模型不存在: {path}"
            return
        try:
            from ais_bench.infer.interface import InferSession

            self.session = InferSession(self.device_id, str(path))
            self.session_thread_id = threading.get_ident()
            self.loaded_path = str(path)
        except Exception as exc:
            self.model_error = f"OM模型加载失败: {exc}"

    def update_config(self, config: dict) -> None:
        self.rule.update_config(config)
        model = config.get("model", {})
        enabled = bool(model.get("enabled", False))
        path = str(model.get("path", "models/pressure_ramp.om"))
        device_id = int(model.get("device_id", 0))
        reload_needed = (enabled, path, device_id) != (
            getattr(self, "enabled", None), getattr(self, "model_path", None),
            getattr(self, "device_id", None))
        self.enabled = enabled
        self.model_path = path
        self.device_id = device_id
        self.window_samples = int(model.get("window_samples", 100))
        self.input_channels = max(1, min(5, int(model.get("input_channels", 1))))
        self.threshold = float(model.get("probability_threshold", 0.5))
        self.hold_s = max(0.0, float(model.get("hold_ms", 300)) / 1000.0)
        self.samples = deque(self.samples, maxlen=self.window_samples)
        if reload_needed:
            # ACL contexts are thread-local. AppState is constructed on the main
            # thread, while telemetry and inference run on SerialController.
            # Defer session creation (and old-session destruction) to process().
            self.reload_pending = True

    def reset_baseline(self) -> None:
        self.rule.reset_baseline()
        self.samples.clear()
        self.obstacle_until = 0.0

    def process(self, pressure: list[float], valid: list[bool], sample_rate_hz: float = 100.0) -> dict:
        fallback = self.rule.process(pressure, valid, sample_rate_hz)
        if self.reload_pending or (self.session is not None and
                                   self.session_thread_id != threading.get_ident()):
            self._load_model()
        if self.session is None or not all(valid[:self.input_channels]):
            fallback["model_error"] = self.model_error
            fallback["model_probability"] = None
            return fallback
        self.samples.append([float(value) for value in pressure[:self.input_channels]])
        if len(self.samples) < self.window_samples:
            fallback["backend"] = "ascend_om_warming"
            fallback["model_error"] = ""
            fallback["model_probability"] = None
            fallback["obstacle"] = False
            return fallback
        try:
            import numpy as np

            array = np.ascontiguousarray(
                np.asarray(self.samples, dtype=np.float32).T[None, :, None, :]
            )
            output = self.session.infer(feeds=[array], mode="static")
            logit = float(np.asarray(output[0]).reshape(-1)[0])
            probability = 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, logit))))
            now = time.monotonic()
            if probability >= self.threshold:
                self.obstacle_until = max(self.obstacle_until, now + self.hold_s)
            fallback["obstacle"] = now <= self.obstacle_until
            fallback["backend"] = "ascend_om"
            fallback["model_probability"] = probability
            fallback["model_error"] = ""
            return fallback
        except Exception as exc:
            shape = tuple(array.shape) if "array" in locals() else None
            dtype = str(array.dtype) if "array" in locals() else "unknown"
            self.model_error = f"OM推理失败: {exc}; input={shape}/{dtype}"
            self.session = None
            self.session_thread_id = None
            fallback["model_error"] = self.model_error
            fallback["model_probability"] = None
            return fallback
