#!/usr/bin/env python3
"""Integrated Atlas service: ESP32 link, inference, slide control and batch capture."""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote, urlparse

from model_detector import HybridDetector
from lidar_runtime import LidarController, PositionTimeline
from plot_generator import generate_experiment_plots
from protocol import (ACK, ACK_PAYLOAD, CLEAR_STOP, CONFIG_MOTION, CONFIG_SENSOR_TEST,
                      ENABLE, FrameDecoder, HEARTBEAT, MOVE_ABSOLUTE, MOVE_DISTANCE, STATUS, STOP, TELEMETRY,
                      decode_status, decode_telemetry, encode_frame, motion_config_payload, move_payload,
                      sensor_test_config_payload, move_distance_payload)

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"
DEFAULT_CONFIG = {
    "serial": {"port": "/dev/ttyACM0", "baud": 115200}, "prefer_npu": True,
    "lidar": {"enabled": False, "port": "/dev/ttyUSB0", "baud": 115200,
              "pitch_deg": 45.0, "minimum_distance_mm": 50.0,
              "maximum_distance_mm": 6000.0, "minimum_quality": 1,
              "minimum_points_per_revolution": 80,
              "minimum_angle_coverage_deg": 270.0,
              "route_min_mm": 0.0, "route_max_mm": 2000.0,
              "corridor_half_width_mm": 350.0,
              "height_min_mm": -100.0, "height_max_mm": 1800.0,
              "cluster_bin_mm": 50.0, "cluster_min_points": 3,
              "persistence_revolutions": 3, "history_revolutions": 4},
    "fusion": {"time_tolerance_ms": 1500, "position_tolerance_mm": 450,
               "event_hold_ms": 1200},
    "sample_rate_hz": 200,
    "plot_filter": {"enabled": True, "median_window": 5, "ema_alpha": 0.35},
    "model": {"enabled": False, "path": "models/pressure_ramp.om", "device_id": 0,
              "window_samples": 200, "input_channels": 1,
              "probability_threshold": 0.5, "hold_ms": 300},
    "detection": {"alpha": 0.9, "threshold_pa": [2.0] * 5, "gain": [1.0] * 5,
                  "baseline_tau_s": 3.0, "channels_required": 2, "window_ms": 80,
                  "hold_ms": 300},
}
CSV_COLUMNS = ["schema_version", "batch_id", "trial_id", "trial_index", "phase",
               "condition", "label", "obstacle_distance_mm", "commanded_speed_mm_s",
               "accel_mm", "decel_mm", "note", "host_time", "esp_uptime_ms",
               "host_monotonic_s", "sample_sequence"] + [f"pressure_pa_{i}" for i in range(1, 6)] + [
               f"temperature_c_{i}" for i in range(1, 6)] + ["valid_mask", "position_mm",
               "moving", "enabled", "stopped", "sensor_test_mode", "lidar_connected",
               "lidar_candidate_position_mm", "lidar_confidence", "fusion_state",
               "fusion_obstacle"]
LIDAR_CSV_COLUMNS = ["schema_version", "batch_id", "trial_id", "trial_index",
                     "host_monotonic_s", "rail_position_mm", "angle_deg", "distance_mm",
                     "quality", "x_m", "y_m", "z_m", "candidate_position_mm",
                     "candidate_confidence"]


def merge_defaults(defaults, value):
    result = copy.deepcopy(defaults)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(result.get(key), dict):
            result[key] = merge_defaults(result[key], item)
        elif key in result:
            result[key] = item
    return result


def validate_config(config):
    def valid_device_path(value):
        return str(value).startswith(("/dev/tty", "/dev/serial/"))

    if not valid_device_path(config["serial"]["port"]):
        raise ValueError("ESP32串口必须是/dev/tty或/dev/serial下的设备路径")
    if not 1200 <= int(config["serial"]["baud"]) <= 3000000:
        raise ValueError("串口波特率无效")
    if not 1 <= float(config["sample_rate_hz"]) <= 1000:
        raise ValueError("采样率无效")
    lidar = config["lidar"]
    if not valid_device_path(lidar["port"]):
        raise ValueError("雷达串口必须是/dev/tty或/dev/serial下的设备路径")
    if not 1200 <= int(lidar["baud"]) <= 3000000:
        raise ValueError("雷达串口波特率无效")
    lidar_finite = [float(lidar[key]) for key in (
        "pitch_deg", "minimum_distance_mm", "maximum_distance_mm",
        "minimum_angle_coverage_deg", "route_min_mm", "route_max_mm",
        "corridor_half_width_mm", "height_min_mm", "height_max_mm", "cluster_bin_mm")]
    if not all(math.isfinite(value) for value in lidar_finite):
        raise ValueError("雷达参数必须为有限数值")
    if not 0 <= float(lidar["minimum_distance_mm"]) < float(lidar["maximum_distance_mm"]):
        raise ValueError("雷达距离范围无效")
    if float(lidar["route_min_mm"]) >= float(lidar["route_max_mm"]):
        raise ValueError("雷达全行程范围无效")
    if float(lidar["height_min_mm"]) >= float(lidar["height_max_mm"]):
        raise ValueError("雷达高度范围无效")
    if float(lidar["corridor_half_width_mm"]) <= 0 or float(lidar["cluster_bin_mm"]) <= 0:
        raise ValueError("雷达走廊或聚类尺寸无效")
    if not 0 <= int(lidar["minimum_quality"]) <= 63:
        raise ValueError("雷达质量阈值无效")
    if int(lidar["minimum_points_per_revolution"]) < 3 or not 0 < float(lidar["minimum_angle_coverage_deg"]) <= 360:
        raise ValueError("雷达完整圈判定参数无效")
    if min(int(lidar["cluster_min_points"]), int(lidar["persistence_revolutions"])) < 1:
        raise ValueError("雷达聚类连续性参数无效")
    if int(lidar["history_revolutions"]) < int(lidar["persistence_revolutions"]):
        raise ValueError("雷达历史圈数不能小于连续确认圈数")
    fusion = config["fusion"]
    fusion_values = [float(fusion["time_tolerance_ms"]),
                     float(fusion["position_tolerance_mm"]),
                     float(fusion["event_hold_ms"])]
    if not all(math.isfinite(value) for value in fusion_values) or min(fusion_values) <= 0:
        raise ValueError("融合时间或位置容差无效")
    plot_filter = config["plot_filter"]
    if int(plot_filter["median_window"]) not in (1, 3, 5) or not 0 < float(plot_filter["ema_alpha"]) <= 1:
        raise ValueError("绘图滤波参数无效")
    model, detection = config["model"], config["detection"]
    if not 20 <= int(model["window_samples"]) <= 2000 or not 0 < float(model["probability_threshold"]) < 1:
        raise ValueError("模型窗口或概率阈值无效")
    if not 1 <= int(model.get("input_channels", 1)) <= 5:
        raise ValueError("模型输入通道数无效")
    if len(detection["threshold_pa"]) != 5 or len(detection["gain"]) != 5:
        raise ValueError("必须提供5路阈值和增益")


def safe_name(value):
    return re.sub(r"[^0-9A-Za-z_-]+", "_", str(value).strip() or "batch")[:64]


def wait_state_not_moving(state, timeout=5.0):
    after = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with state.lock:
            sample = copy.deepcopy(state.latest)
            received = state.last_telemetry_time
        if sample and received > after and not sample.get("moving", False): return
        time.sleep(.05)
    raise RuntimeError("停止后运动状态未释放")


class AppState:
    def __init__(self, config_path: Path, data_dir: Path | None = None):
        self.lock = threading.RLock(); self.ack_condition = threading.Condition(self.lock)
        self.config_path = config_path; self.data_dir = data_dir or DATA_ROOT
        self.presets_path = config_path.with_name("experiment_presets.json")
        try: loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError: loaded = {}
        try: self.experiment_presets = json.loads(self.presets_path.read_text(encoding="utf-8"))
        except FileNotFoundError: self.experiment_presets = {}
        if not isinstance(self.experiment_presets, dict):
            raise ValueError("实验参数文件格式无效")
        self.config = merge_defaults(DEFAULT_CONFIG, loaded); validate_config(self.config)
        self.detector = HybridDetector(self.config, ROOT)
        self.position_timeline = PositionTimeline()
        self.latest = None; self.last_telemetry_time = 0.0; self.serial_error = ""; self.crc_errors = 0
        self.lidar_status = {"enabled": bool(self.config["lidar"]["enabled"]),
                             "connected": False, "error": "", "revolutions": 0,
                             "rejected_revolutions": 0, "current_points": 0,
                             "discarded_bytes": 0,
                             "detection": {"obstacle": False,
                                           "candidate_position_mm": None,
                                           "confidence": None,
                                           "reason": "雷达尚未启动"}}
        self.latest_pressure_event = None
        self.latest_lidar_event = None
        self.status_fields = {"temperature_c": [0.0] * 5, "soft_min_mm": 0.0,
                              "soft_max_mm": 0.0, "pulses_remaining": 0, "link_age_ms": 0,
                              "sample_rate_hz": 200,
                              "i2c_bus": None,
                              "sensor_diagnostics": [
                                  {"code": None, "reason": "尚未收到ESP32诊断状态",
                                   "error_count": None, "esp_error": None}
                                  for _ in range(5)]}
        self.acks = {}; self.batch = None

    def save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    def save_experiment_presets(self):
        self.presets_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.presets_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.experiment_presets, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(self.presets_path)

    def public_experiment_presets(self):
        with self.lock:
            return [{"name": name, "parameters": validate_batch(parameters)}
                    for name, parameters in sorted(self.experiment_presets.items())]

    def save_experiment_preset(self, name, parameters):
        if not isinstance(name, str):
            raise ValueError("请输入实验参数方案名称")
        name = name.strip()
        if not name or len(name) > 64 or any(ord(character) < 32 for character in name):
            raise ValueError("方案名称必须为1到64个有效字符")
        normalized = validate_batch(parameters)
        with self.lock:
            self.experiment_presets[name] = normalized
            self.save_experiment_presets()

    def delete_experiment_preset(self, name):
        if not isinstance(name, str):
            raise ValueError("请选择实验参数方案")
        name = name.strip()
        with self.lock:
            if name not in self.experiment_presets:
                raise ValueError("实验参数方案不存在")
            del self.experiment_presets[name]
            self.save_experiment_presets()

    def public_status(self):
        with self.lock:
            return {"connected": time.monotonic() - self.last_telemetry_time < 1.0,
                    "serial_error": self.serial_error, "crc_errors": self.crc_errors,
                    "latest": copy.deepcopy(self.latest), "role": "integrated_controller",
                    "lidar": copy.deepcopy(self.lidar_status),
                    "batch": self.batch.public_status() if self.batch else {"active": False}}

    def _fusion_locked(self, now, pressure_detection=None):
        fusion = self.config["fusion"]
        hold_s = float(fusion["event_hold_ms"]) / 1000.0
        time_tolerance_s = float(fusion["time_tolerance_ms"]) / 1000.0
        position_tolerance = float(fusion["position_tolerance_mm"])
        lidar_enabled = bool(self.config["lidar"]["enabled"])
        pressure_event = self.latest_pressure_event
        lidar_event = self.latest_lidar_event
        pressure_active = bool(pressure_event and now - pressure_event["time"] <= hold_s)
        lidar_active = bool(lidar_event and now - lidar_event["time"] <= hold_s)
        if not lidar_enabled:
            obstacle = bool(pressure_detection and pressure_detection.get("obstacle"))
            return {"state": "pressure_only", "obstacle": obstacle,
                    "reason": "雷达未启用，沿用压力检测结果",
                    "pressure_position_mm": pressure_event["position_mm"] if pressure_active else None,
                    "lidar_position_mm": None}
        confirmed = False
        if pressure_active and lidar_active:
            confirmed = (abs(pressure_event["time"] - lidar_event["time"]) <= time_tolerance_s and
                         abs(pressure_event["position_mm"] - lidar_event["position_mm"]) <= position_tolerance)
        if confirmed:
            state, reason = "confirmed", "雷达点簇与压力异常在时间和位置上吻合"
        elif lidar_active:
            state, reason = "lidar_suspected", "雷达发现持续点簇，等待压力确认"
        elif pressure_active:
            state, reason = "pressure_suspected", "压力出现异常，等待雷达确认"
        elif not self.lidar_status.get("connected"):
            state, reason = "unknown", "雷达未连接，无法进行双传感器确认"
        else:
            state, reason = "clear", "当前没有持续雷达点簇或压力异常"
        return {"state": state, "obstacle": confirmed, "reason": reason,
                "pressure_position_mm": pressure_event["position_mm"] if pressure_active else None,
                "lidar_position_mm": lidar_event["position_mm"] if lidar_active else None}

    def on_telemetry(self, sample):
        with self.lock:
            now = time.monotonic()
            for key, value in self.status_fields.items():
                sample.setdefault(key, copy.deepcopy(value))
            sample.setdefault("position_mm", float((self.latest or {}).get("position_mm", 0.0)))
            self.position_timeline.add(now, float(sample["position_mm"]))
            detection = self.detector.process(sample["pressure_pa"], sample["valid"],
                                              float(sample.get("sample_rate_hz", self.config["sample_rate_hz"])))
            if detection.get("obstacle"):
                self.latest_pressure_event = {"time": now,
                                              "position_mm": float(sample["position_mm"])}
            fusion = self._fusion_locked(now, detection)
            detection["pressure_obstacle"] = bool(detection.get("obstacle"))
            detection["fusion"] = fusion
            detection["obstacle"] = fusion["obstacle"]
            sample["detection"] = detection
            sample["lidar"] = copy.deepcopy(self.lidar_status)
            sample["host_time"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
            sample["host_monotonic_s"] = now
            self.latest = sample; self.last_telemetry_time = now; batch = self.batch
        if batch: batch.on_sample(sample)

    def on_lidar_status(self, status):
        with self.lock:
            now = time.monotonic()
            self.lidar_status = copy.deepcopy(status)
            detection = status.get("detection") or {}
            if detection.get("obstacle") and detection.get("candidate_position_mm") is not None:
                self.latest_lidar_event = {"time": now,
                                           "position_mm": float(detection["candidate_position_mm"]),
                                           "confidence": detection.get("confidence")}
            if self.latest and self.latest.get("detection"):
                fusion = self._fusion_locked(now, self.latest["detection"])
                self.latest["detection"]["fusion"] = fusion
                self.latest["detection"]["obstacle"] = fusion["obstacle"]

    def on_lidar_scan(self, points, detection):
        self.on_lidar_status({**copy.deepcopy(self.lidar_status),
                              "enabled": bool(self.config["lidar"]["enabled"]),
                              "connected": True, "error": "",
                              "detection": copy.deepcopy(detection)})
        with self.lock:
            batch = self.batch
        if batch:
            batch.on_lidar_scan(points, detection)

    def on_status(self, status):
        with self.lock:
            self.status_fields.update(status)
            if self.latest:
                self.latest.update(copy.deepcopy(status))


class SerialController(threading.Thread):
    def __init__(self, state):
        super().__init__(name="serial-controller", daemon=True)
        self.state = state; self.decoder = FrameDecoder(); self.stop_event = threading.Event()
        self.reopen_event = threading.Event(); self.write_lock = threading.Lock(); self.serial = None; self.sequence = 0

    def reconfigure(self):
        self.reopen_event.set()
        if self.serial:
            try: self.serial.close()
            except Exception: pass

    def send(self, message_type, payload=b""):
        with self.write_lock:
            if self.serial is None or not self.serial.is_open: raise RuntimeError("串口尚未连接")
            sequence = self.sequence; self.sequence = (self.sequence + 1) & 0xFFFF
            self.serial.write(encode_frame(message_type, sequence, payload)); return sequence

    def command(self, message_type, payload=b"", timeout=3.0):
        sequence = self.send(message_type, payload); deadline = time.monotonic() + timeout
        with self.state.ack_condition:
            while sequence not in self.state.acks:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise RuntimeError(f"命令0x{message_type:02X}等待ESP32确认超时")
                self.state.ack_condition.wait(remaining)
            ack = self.state.acks.pop(sequence)
        if ack["request"] != message_type or ack["result"] != 0:
            raise RuntimeError(f"ESP32拒绝命令0x{message_type:02X}，错误码{ack['result']}")

    def run(self):
        try: import serial
        except ImportError:
            with self.state.lock: self.state.serial_error = "缺少pyserial，请安装requirements.txt"
            return
        while not self.stop_event.is_set():
            try:
                with self.state.lock: settings = copy.deepcopy(self.state.config["serial"])
                self.serial = serial.Serial(settings["port"], int(settings["baud"]), timeout=.05, write_timeout=.2)
                self.reopen_event.clear(); self.decoder = FrameDecoder()
                with self.state.lock: self.state.serial_error = ""
                last_heartbeat = 0.0
                while not self.stop_event.is_set() and not self.reopen_event.is_set():
                    now = time.monotonic()
                    if now - last_heartbeat >= .2: self.send(HEARTBEAT); last_heartbeat = now
                    for frame in self.decoder.feed(self.serial.read(512)):
                        if frame.message_type == TELEMETRY: self.state.on_telemetry(decode_telemetry(frame.payload))
                        elif frame.message_type == STATUS: self.state.on_status(decode_status(frame.payload))
                        elif frame.message_type == ACK and len(frame.payload) == ACK_PAYLOAD.size:
                            request, _, result = ACK_PAYLOAD.unpack(frame.payload)
                            if request != HEARTBEAT:
                                with self.state.ack_condition:
                                    self.state.acks[frame.sequence] = {"request": request, "result": result}
                                    self.state.ack_condition.notify_all()
                    with self.state.lock: self.state.crc_errors = self.decoder.crc_errors
            except Exception as exc:
                with self.state.lock: self.state.serial_error = str(exc)
                time.sleep(1)
            finally:
                if self.serial:
                    try: self.serial.close()
                    except Exception: pass
                self.serial = None


class BatchRunner(threading.Thread):
    def __init__(self, state, link, settings):
        super().__init__(name="batch-capture", daemon=True)
        self.state = state; self.link = link; self.s = settings; self.guard = threading.RLock()
        self.stop_event = threading.Event(); self.pause_event = threading.Event(); self.recording = False
        self.active = True; self.status = "准备中"; self.completed = 0; self.rows = 0; self.dropped = 0
        self.last_sequence = None; self.trial_index = 0; self.error = ""; self.csv_path = None
        self.lidar_csv_path = None
        self.batch_plot_path = None; self.plot_path = None
        self.file = None; self.writer = None; self.lidar_file = None; self.lidar_writer = None

    def public_status(self):
        with self.guard:
            return {"active": self.active, "paused": self.pause_event.is_set(), "status": self.status,
                    "completed": self.completed, "total": self.s["count"], "rows": self.rows,
                    "dropped": self.dropped, "error": self.error,
                    "csv": self.csv_path.name if self.csv_path else None,
                    "lidar_csv": self.lidar_csv_path.name if self.lidar_csv_path else None,
                    "batch_plot": self.batch_plot_path.name if self.batch_plot_path else None,
                    "plot": self.plot_path.name if self.plot_path else None}

    def set_status(self, text):
        with self.guard: self.status = text

    def on_sample(self, sample):
        with self.guard:
            if not self.recording or not self.writer: return
            lidar_sample = sample.get("lidar", getattr(self.state, "lidar_status", {}))
            fusion = sample.get("detection", {}).get(
                "fusion", {"state": "unavailable", "obstacle": False})
            seq = sample["sample_sequence"]
            if self.last_sequence is not None:
                gap = (seq - self.last_sequence - 1) & 0xFFFFFFFF
                if gap < 100000: self.dropped += gap
            self.last_sequence = seq; self.rows += 1
            trial_id = f"{safe_name(self.s['batch_id'])}_{self.trial_index:04d}"
            values = [6, self.s["batch_id"], trial_id, self.trial_index, "measure", self.s["condition"],
                      self.s["label"], self.s["obstacle_distance_mm"], self.s["speed"], self.s["accel"],
                      self.s["decel"], self.s["note"], sample["host_time"], sample["uptime_ms"],
                      sample.get("host_monotonic_s", ""), seq,
                      *sample["pressure_pa"], *sample["temperature_c"], sample["valid_mask"],
                      sample["position_mm"], int(sample["moving"]), int(sample["enabled"]),
                      int(sample["stopped"]), int(sample["sensor_test_mode"]),
                      int(lidar_sample.get("connected", False)),
                      lidar_sample.get("detection", {}).get("candidate_position_mm"),
                      lidar_sample.get("detection", {}).get("confidence"),
                      fusion["state"], int(fusion["obstacle"])]
            self.writer.writerow(values)

    def on_lidar_scan(self, points, detection):
        with self.guard:
            if not self.recording or not self.lidar_writer:
                return
            trial_id = f"{safe_name(self.s['batch_id'])}_{self.trial_index:04d}"
            candidate = detection.get("candidate_position_mm")
            confidence = detection.get("confidence")
            for point in points:
                self.lidar_writer.writerow([
                    1, self.s["batch_id"], trial_id, self.trial_index,
                    f"{point.timestamp_s:.9f}", f"{point.rail_position_m * 1000.0:.3f}",
                    f"{point.angle_deg:.4f}", f"{point.distance_mm:.3f}", point.quality,
                    f"{point.x_m:.6f}", f"{point.y_m:.6f}", f"{point.z_m:.6f}",
                    candidate, confidence,
                ])

    def command(self, kind, payload=b""): self.link.command(kind, payload)
    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_event.is_set(): raise InterruptedError
            time.sleep(min(.05, end - time.monotonic()))

    def wait_position(self, target, speed, after=None, ramp_distance=0):
        with self.state.lock: initial = (self.state.latest or {}).get("position_mm", target)
        if after is None: after = time.monotonic()
        deadline = time.monotonic() + (abs(target - initial) + 2 * ramp_distance) / max(.1, speed) + 15
        while time.monotonic() < deadline:
            if self.stop_event.is_set(): raise InterruptedError
            with self.state.lock:
                sample = copy.deepcopy(self.state.latest)
                received = self.state.last_telemetry_time
            if time.monotonic() - max(received, after) > 2:
                raise RuntimeError("等待到位时ESP32数据中断")
            if sample and received > after:
                if sample.get("stopped") or not sample.get("enabled"):
                    raise RuntimeError("等待到位时滑台已停止锁定或失能")
                if not sample["moving"] and abs(sample["position_mm"] - target) <= .2: return
            time.sleep(.05)
        raise RuntimeError(f"等待到达{target} mm超时")

    def wait_not_moving(self, timeout=5.0):
        wait_state_not_moving(self.state, timeout)

    def move(self, target, speed, accel, decel):
        self.command(MOVE_DISTANCE, move_distance_payload(target, speed, accel, decel))
        self.wait_position(target, speed, after=time.monotonic(), ramp_distance=accel + decel)

    def run(self):
        condition_dir = self.state.data_dir / safe_name(self.s["condition"]); condition_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = condition_dir / f"{stamp}_{safe_name(self.s['batch_id'])}.csv"
        self.lidar_csv_path = condition_dir / f"{stamp}_{safe_name(self.s['batch_id'])}_lidar.csv"
        try:
            self.file = self.csv_path.open("w", encoding="utf-8-sig", newline="")
            self.writer = csv.writer(self.file); self.writer.writerow(CSV_COLUMNS)
            if self.state.config["lidar"]["enabled"]:
                self.lidar_file = self.lidar_csv_path.open("w", encoding="utf-8-sig", newline="")
                self.lidar_writer = csv.writer(self.lidar_file)
                self.lidar_writer.writerow(LIDAR_CSV_COLUMNS)
            with self.state.lock: latest = copy.deepcopy(self.state.latest)
            if latest and (latest["moving"] or latest["stopped"]):
                self.command(STOP); self.wait_not_moving()
            self.command(CLEAR_STOP)
            self.command(CONFIG_MOTION, motion_config_payload({"soft_min_mm": self.s["soft_min"], "soft_max_mm": self.s["soft_max"], "pulses_per_mm": self.s["pulses_per_mm"], "max_speed_mm_s": self.s["max_speed"]}))
            self.command(ENABLE, b"\x01")
            for index in range(1, self.s["count"] + 1):
                while self.pause_event.is_set() and not self.stop_event.is_set(): self.set_status("已暂停"); time.sleep(.1)
                if self.stop_event.is_set(): raise InterruptedError
                self.trial_index = index; self.set_status(f"第{index}/{self.s['count']}次：回起点")
                self.move(self.s["start"], self.s["return_speed"], self.s["return_accel"], self.s["return_decel"])
                self.set_status(f"第{index}/{self.s['count']}次：等待基线"); self.wait(self.s["baseline_wait"] / 1000)
                self.last_sequence = None; self.recording = True; self.set_status(f"第{index}/{self.s['count']}次：正向采集")
                self.move(self.s["end"], self.s["speed"], self.s["accel"], self.s["decel"])
                self.wait(self.s["end_dwell"] / 1000); self.recording = False; self.completed = index; self.file.flush()
                if self.lidar_file: self.lidar_file.flush()
                self.set_status(f"第{index}/{self.s['count']}次：反向复位")
                self.move(self.s["start"], self.s["return_speed"], self.s["return_accel"], self.s["return_decel"])
                if index < self.s["count"]: self.wait(self.s["between_wait"] / 1000)
            self.command(ENABLE, b"\x00"); self.set_status("批次完成")
        except InterruptedError: self.set_status("批次已停止")
        except Exception as exc: self.error = str(exc); self.set_status("批次失败：" + str(exc))
        finally:
            self.recording = False
            try: self.command(STOP)
            except Exception: pass
            if self.file: self.file.close()
            if self.lidar_file: self.lidar_file.close()
            try: self.batch_plot_path, self.plot_path = generate_experiment_plots(
                self.csv_path, copy.deepcopy(self.state.config["plot_filter"]))
            except Exception as exc: self.error += ("；" if self.error else "") + "绘图失败：" + str(exc)
            self.active = False


def validate_batch(body):
    numeric = ["count","start","end","speed","accel","decel","baseline_wait","end_dwell","return_speed","return_accel","return_decel","between_wait","soft_min","soft_max","pulses_per_mm","max_speed","label","obstacle_distance_mm"]
    result = {key: float(body[key]) for key in numeric}
    if not all(math.isfinite(value) for value in result.values()): raise ValueError("实验参数必须为有限数值")
    if any(result[key] < 0 for key in ["accel","decel","return_accel","return_decel","baseline_wait","end_dwell","between_wait"]):
        raise ValueError("距离和等待时间不能为负数")
    for key in ["count","label","baseline_wait","end_dwell","between_wait"]: result[key] = int(result[key])
    result.update({key: str(body.get(key,"")) for key in ["batch_id","condition","note"]})
    if not 1 <= result["count"] <= 500 or result["start"] == result["end"]: raise ValueError("次数或起止位置无效")
    if not result["soft_min"] <= min(result["start"], result["end"]) <= max(result["start"], result["end"]) <= result["soft_max"]: raise ValueError("起止位置超出软限位")
    if min(result["speed"], result["return_speed"], result["pulses_per_mm"]) <= 0 or max(result["speed"],result["return_speed"]) > result["max_speed"] or result["max_speed"] > 2000: raise ValueError("速度或脉冲参数无效；硬上限为2000 mm/s")
    unit = body.get("ramp_unit", "ms")
    if unit not in ("mm", "ms"): raise ValueError("未知缓启停单位")
    if unit == "ms":
        # Legacy presets/API: convert nominal full-speed time ramps, never relabel ms as mm.
        minimum_speed = 200 / result["pulses_per_mm"]
        for key, speed_key in [("accel", "speed"), ("decel", "speed"), ("return_accel", "return_speed"), ("return_decel", "return_speed")]:
            speed = max(result[speed_key], minimum_speed)
            result[key] = round((speed + minimum_speed) * result[key] / 2000, 3)
    result["ramp_unit"] = "mm"
    for speed_key, accel_key, decel_key in [("speed", "accel", "decel"), ("return_speed", "return_accel", "return_decel")]:
        move_distance_payload(result["end"], result[speed_key], result[accel_key], result[decel_key])
    return result


class ApiHandler(SimpleHTTPRequestHandler):
    state: AppState; link: SerialController; lidar: LidarController
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(ROOT / "web"), **kwargs)
    def log_message(self, fmt, *args): print(f"[web] {self.address_string()} {fmt % args}")
    def _json(self, value, status=HTTPStatus.OK):
        body=json.dumps(value,ensure_ascii=False,allow_nan=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
    def _body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/status": self._json(self.state.public_status()); return
        if path=="/api/config":
            with self.state.lock: self._json(self.state.config)
            return
        if path=="/api/experiment-presets":
            self._json({"presets": self.state.public_experiment_presets()}); return
        if path=="/api/files":
            files=[{"name":str(p.relative_to(self.state.data_dir)),"url":"/data/"+str(p.relative_to(self.state.data_dir)).replace("\\","/")} for p in sorted(self.state.data_dir.rglob("*")) if p.is_file()]
            self._json(files[-100:]); return
        if path.startswith("/data/"):
            relative=Path(unquote(path[6:])); target=(self.state.data_dir/relative).resolve(); root=self.state.data_dir.resolve()
            if root not in target.parents or not target.is_file(): self.send_error(404); return
            content=target.read_bytes(); is_svg=target.suffix.lower()==".svg"; self.send_response(200); self.send_header("Content-Type","image/svg+xml" if is_svg else "text/csv; charset=utf-8"); self.send_header("Content-Disposition",f'{"inline" if is_svg else "attachment"}; filename="{target.name}"'); self.send_header("Content-Length",str(len(content))); self.end_headers(); self.wfile.write(content); return
        super().do_GET()
    def do_POST(self):
        try:
            path=urlparse(self.path).path; body=self._body()
            if path=="/api/config":
                candidate=merge_defaults(self.state.config,body); validate_config(candidate)
                changed=candidate["serial"]!=self.state.config["serial"]
                lidar_changed=candidate["lidar"]!=self.state.config["lidar"]
                with self.state.lock:
                    self.state.config=candidate
                    self.state.detector.update_config(candidate)
                    self.state.save_config()
                if changed:self.link.reconfigure()
                if lidar_changed:self.lidar.reconfigure()
            elif path=="/api/experiment-presets":
                action=body.get("action")
                if action=="save": self.state.save_experiment_preset(body.get("name"),body.get("parameters",{}))
                elif action=="delete": self.state.delete_experiment_preset(body.get("name"))
                else: raise ValueError("未知实验参数操作")
            elif path=="/api/control":
                action=body.get("action")
                if action=="baseline": self.state.detector.reset_baseline()
                elif action=="stop": self.link.command(STOP); wait_state_not_moving(self.state)
                elif action=="clear_enable":
                    with self.state.lock: moving=bool(self.state.latest and self.state.latest.get("moving"))
                    if moving: self.link.command(STOP); wait_state_not_moving(self.state)
                    self.link.command(CLEAR_STOP); self.link.command(ENABLE,b"\x01")
                elif action=="disable": self.link.command(ENABLE,b"\x00")
                elif action=="configure": self.link.command(CONFIG_MOTION,motion_config_payload(body))
                elif action=="move":
                    if "accel_mm" in body or "decel_mm" in body:
                        self.link.command(MOVE_DISTANCE,move_distance_payload(body["target_mm"],body["speed_mm_s"],body["accel_mm"],body["decel_mm"]))
                    else: self.link.command(MOVE_ABSOLUTE,move_payload(body["target_mm"],body["speed_mm_s"],body["accel_ms"],body["decel_ms"]))
                elif action=="test": self.link.command(CONFIG_SENSOR_TEST,sensor_test_config_payload(body))
                else: raise ValueError("未知控制动作")
            elif path=="/api/batch":
                action=body.get("action")
                if action=="start":
                    if self.state.batch and self.state.batch.active: raise ValueError("已有批次运行中")
                    runner=BatchRunner(self.state,self.link,validate_batch(body)); self.state.batch=runner; runner.start()
                elif action=="pause": self.state.batch.pause_event.set()
                elif action=="resume": self.state.batch.pause_event.clear()
                elif action=="stop": self.state.batch.stop_event.set(); self.link.command(STOP)
                else: raise ValueError("未知批次动作")
            else: self._json({"ok":False,"error":"not found"},404); return
            self._json({"ok":True})
        except (ValueError,KeyError,TypeError,RuntimeError,AttributeError) as exc: self._json({"ok":False,"error":str(exc)},400)
        except Exception as exc: self._json({"ok":False,"error":str(exc)},500)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="0.0.0.0"); parser.add_argument("--port",type=int,default=8080); parser.add_argument("--config",type=Path,default=ROOT/"config.json"); parser.add_argument("--data",type=Path,default=DATA_ROOT); args=parser.parse_args()
    state=AppState(args.config,args.data); state.save_config(); link=SerialController(state); lidar=LidarController(state); ApiHandler.state=state; ApiHandler.link=link; ApiHandler.lidar=lidar; link.start(); lidar.start(); server=ThreadingHTTPServer((args.host,args.port),ApiHandler); print(f"Atlas integrated UI: http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: link.stop_event.set(); lidar.stop_event.set(); lidar.reconfigure(); server.server_close()

if __name__=="__main__": main()
