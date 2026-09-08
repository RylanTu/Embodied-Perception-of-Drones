"""Shared binary protocol for the Atlas USB serial link."""

from __future__ import annotations

from dataclasses import dataclass
import struct

MAGIC = 0xA55A
VERSION = 2
HEADER = struct.Struct("<HBBHH")
CRC = struct.Struct("<H")
MAX_PAYLOAD = 96

TELEMETRY = 0x01
HEARTBEAT = 0x02
STATUS = 0x03
CONFIG_MOTION = 0x10
MOVE_ABSOLUTE = 0x11
STOP = 0x12
ENABLE = 0x13
CLEAR_STOP = 0x14
CONFIG_SENSOR_TEST = 0x15
MOVE_DISTANCE = 0x16
ACK = 0x7E

TELEMETRY_PAYLOAD = struct.Struct("<II5iBBiI")
LEGACY_STATUS_PAYLOAD = struct.Struct("<I5hHiiIII")
DIAGNOSTIC_STATUS_PAYLOAD = struct.Struct("<I5hHiiIII5B3x5I5i")
STATUS_PAYLOAD = struct.Struct("<I5hHiiIII5B3x5I5i4Bi")
MOTION_CONFIG_PAYLOAD = struct.Struct("<iiII")
MOVE_PAYLOAD = struct.Struct("<iIII")
ACK_PAYLOAD = struct.Struct("<BBi")
SENSOR_TEST_CONFIG_PAYLOAD = struct.Struct("<B3xiiII")


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
    return value


def encode_frame(message_type: int, sequence: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    body = HEADER.pack(MAGIC, VERSION, message_type, sequence & 0xFFFF, len(payload)) + payload
    return body + CRC.pack(crc16(body))


@dataclass(frozen=True)
class Frame:
    message_type: int
    sequence: int
    payload: bytes


class FrameDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer.extend(data)
        frames: list[Frame] = []
        while len(self.buffer) >= HEADER.size:
            magic, version, message_type, sequence, payload_bytes = HEADER.unpack_from(self.buffer)
            if magic != MAGIC:
                del self.buffer[0]
                continue
            if version != VERSION or payload_bytes > MAX_PAYLOAD:
                del self.buffer[:2]
                continue
            total = HEADER.size + payload_bytes + CRC.size
            if len(self.buffer) < total:
                break
            expected = CRC.unpack_from(self.buffer, HEADER.size + payload_bytes)[0]
            if expected != crc16(self.buffer[: HEADER.size + payload_bytes]):
                self.crc_errors += 1
                del self.buffer[0]
                continue
            payload = bytes(self.buffer[HEADER.size : HEADER.size + payload_bytes])
            frames.append(Frame(message_type, sequence, payload))
            del self.buffer[:total]
        return frames


def decode_telemetry(payload: bytes) -> dict:
    if len(payload) != TELEMETRY_PAYLOAD.size:
        raise ValueError(f"telemetry size {len(payload)} != {TELEMETRY_PAYLOAD.size}")
    values = TELEMETRY_PAYLOAD.unpack(payload)
    uptime_ms, sample_sequence = values[:2]
    pressure_mpa = values[2:7]
    valid_mask, flags = values[7:9]
    position, pulses_per_meter = values[9:11]
    scale = pulses_per_meter / 1000.0 if pulses_per_meter else 1.0
    return {
        "uptime_ms": uptime_ms,
        "sample_sequence": sample_sequence,
        "pressure_pa": [value / 1000.0 for value in pressure_mpa],
        "valid": [bool(valid_mask & (1 << index)) for index in range(5)],
        "valid_mask": valid_mask,
        "enabled": bool(flags & 1),
        "stopped": bool(flags & 2),
        "moving": bool(flags & 4),
        "direction_positive": bool(flags & 8),
        "position_trusted": bool(flags & 16),
        "sensor_test_mode": bool(flags & 32),
        "position_mm": position / scale,
        "pulses_per_meter": pulses_per_meter,
    }


SENSOR_DIAGNOSTIC_REASONS = {
    0: "正常",
    1: "TCA9548A通道选择失败",
    2: "SDP3x连续测量启动失败",
    3: "SDP3x读取失败",
    4: "压力数据CRC错误",
    5: "温度数据CRC错误",
    6: "比例因子CRC错误",
    7: "比例因子为0",
    8: "未发现TCA9548A（已扫描0x70～0x77）",
    9: "I²C SDA被拉低",
    10: "I²C SCL被拉低",
    11: "I²C SDA和SCL均被拉低",
    12: "直连模式未启用此通道",
}


def decode_status(payload: bytes) -> dict:
    if len(payload) == STATUS_PAYLOAD.size:
        values = STATUS_PAYLOAD.unpack(payload)
        diagnostic_codes = values[12:17]
        error_counts = values[17:22]
        last_errors = values[22:27]
        diagnostics = [
            {"code": code, "reason": SENSOR_DIAGNOSTIC_REASONS.get(code, f"未知故障{code}"),
             "error_count": count, "esp_error": error}
            for code, count, error in zip(diagnostic_codes, error_counts, last_errors)
        ]
        sda_gpio, scl_gpio, mux_address, line_flags, probe_error = values[27:32]
        i2c_bus = {
            "sda_gpio": sda_gpio, "scl_gpio": scl_gpio,
            "sda_level": bool(line_flags & 1), "scl_level": bool(line_flags & 2),
            "direct_mode": bool(line_flags & 4),
            "mux_address": None if mux_address == 0xFF else mux_address,
            "probe_error": probe_error,
        }
    elif len(payload) == DIAGNOSTIC_STATUS_PAYLOAD.size:
        values = DIAGNOSTIC_STATUS_PAYLOAD.unpack(payload)
        diagnostic_codes = values[12:17]
        error_counts = values[17:22]
        last_errors = values[22:27]
        diagnostics = [
            {"code": code, "reason": SENSOR_DIAGNOSTIC_REASONS.get(code, f"未知故障{code}"),
             "error_count": count, "esp_error": error}
            for code, count, error in zip(diagnostic_codes, error_counts, last_errors)
        ]
        i2c_bus = None
    elif len(payload) == LEGACY_STATUS_PAYLOAD.size:
        values = LEGACY_STATUS_PAYLOAD.unpack(payload)
        diagnostics = [
            {"code": None, "reason": "旧版ESP32固件未提供诊断", "error_count": None,
             "esp_error": None} for _ in range(5)
        ]
        i2c_bus = None
    else:
        raise ValueError(
            f"status size {len(payload)} != {LEGACY_STATUS_PAYLOAD.size}/"
            f"{DIAGNOSTIC_STATUS_PAYLOAD.size}/{STATUS_PAYLOAD.size}"
        )
    uptime_ms = values[0]
    temperature_cc = values[1:6]
    sample_rate_hz = values[6]
    soft_min, soft_max = values[7:9]
    pulses_per_meter, remaining, link_age_ms = values[9:12]
    scale = pulses_per_meter / 1000.0 if pulses_per_meter else 1.0
    return {
        "status_uptime_ms": uptime_ms,
        "temperature_c": [value / 100.0 for value in temperature_cc],
        "sample_rate_hz": sample_rate_hz,
        "soft_min_mm": soft_min / scale,
        "soft_max_mm": soft_max / scale,
        "pulses_remaining": remaining,
        "link_age_ms": link_age_ms,
        "sensor_diagnostics": diagnostics,
        "i2c_bus": i2c_bus,
    }


def motion_config_payload(config: dict) -> bytes:
    return MOTION_CONFIG_PAYLOAD.pack(
        round(float(config["soft_min_mm"]) * 1000),
        round(float(config["soft_max_mm"]) * 1000),
        round(float(config["pulses_per_mm"]) * 1000),
        round(float(config["max_speed_mm_s"]) * 1000),
    )


def move_distance_payload(target_mm: float, speed_mm_s: float, accel_mm: float, decel_mm: float) -> bytes:
    import math
    values = [float(value) for value in (target_mm, speed_mm_s, accel_mm, decel_mm)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("运动参数必须是有限数值")
    if values[1] <= 0 or min(values[2:]) < 0:
        raise ValueError("速度必须大于0，缓启停距离不能为负数")
    if not -2147483.648 <= values[0] <= 2147483.647 or any(v > 4294967.295 for v in values[1:]):
        raise ValueError("运动参数超出协议范围")
    scaled = [round(value * 1000) for value in values]
    if not -2147483648 <= scaled[0] <= 2147483647 or any(not 0 <= v <= 4294967295 for v in scaled[1:]) or not scaled[1]:
        raise ValueError("运动参数超出协议范围")
    return MOVE_PAYLOAD.pack(*scaled)


def move_payload(target_mm: float, speed_mm_s: float, accel_ms: int, decel_ms: int) -> bytes:
    return MOVE_PAYLOAD.pack(
        round(target_mm * 1000), round(speed_mm_s * 1000), int(accel_ms), int(decel_ms)
    )


def sensor_test_config_payload(config: dict) -> bytes:
    return SENSOR_TEST_CONFIG_PAYLOAD.pack(
        int(bool(config["enabled"])),
        round(float(config["baseline_pa"]) * 1000),
        round(float(config["amplitude_pa"]) * 1000),
        int(config["period_ms"]),
        int(config["pulse_width_ms"]),
    )
