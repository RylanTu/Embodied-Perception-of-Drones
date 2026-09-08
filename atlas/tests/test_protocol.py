import struct
import unittest

from protocol import (
    FrameDecoder,
    HEARTBEAT,
    SENSOR_TEST_CONFIG_PAYLOAD,
    STATUS_PAYLOAD,
    TELEMETRY_PAYLOAD,
    crc16,
    decode_status,
    decode_telemetry,
    encode_frame,
    sensor_test_config_payload,
)


class ProtocolTests(unittest.TestCase):
    def test_fragmented_and_noisy_frame(self):
        frame = encode_frame(HEARTBEAT, 42)
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(b"noise" + frame[:4]), [])
        result = decoder.feed(frame[4:])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].message_type, HEARTBEAT)
        self.assertEqual(result[0].sequence, 42)

    def test_bad_crc_is_rejected_and_next_frame_survives(self):
        bad = bytearray(encode_frame(HEARTBEAT, 1))
        bad[-1] ^= 0x80
        decoder = FrameDecoder()
        result = decoder.feed(bytes(bad) + encode_frame(HEARTBEAT, 2))
        self.assertEqual([frame.sequence for frame in result], [2])
        self.assertEqual(decoder.crc_errors, 1)

    def test_telemetry_layout_matches_firmware(self):
        payload = TELEMETRY_PAYLOAD.pack(
            1000, 9, *[1000, -2000, 3000, 4000, 5000],
            0b11111, 0b00101, 800, 80000,
        )
        sample = decode_telemetry(payload)
        self.assertEqual(TELEMETRY_PAYLOAD.size, 38)
        self.assertEqual(sample["pressure_pa"][:2], [1.0, -2.0])
        self.assertTrue(sample["enabled"])
        self.assertTrue(sample["moving"])
        self.assertEqual(sample["position_mm"], 10.0)

    def test_status_layout_matches_firmware(self):
        payload = STATUS_PAYLOAD.pack(
            1000, *[2500] * 5, 200, 0, 160000, 80000, 10, 25,
            *[0, 1, 2, 4, 7], *[0, 3, 4, 5, 6], *[0, 263, 259, 5379, 5378],
            1, 2, 0x70, 7, 0,
        )
        status = decode_status(payload)
        self.assertEqual(STATUS_PAYLOAD.size, 92)
        self.assertEqual(status["temperature_c"], [25.0] * 5)
        self.assertEqual(status["sample_rate_hz"], 200)
        self.assertEqual(status["soft_max_mm"], 2000.0)
        self.assertEqual(status["pulses_remaining"], 10)
        self.assertEqual(status["sensor_diagnostics"][1]["reason"], "TCA9548A通道选择失败")
        self.assertEqual(status["sensor_diagnostics"][1]["error_count"], 3)
        self.assertEqual(status["sensor_diagnostics"][1]["esp_error"], 263)
        self.assertEqual(status["i2c_bus"]["sda_gpio"], 1)
        self.assertEqual(status["i2c_bus"]["mux_address"], 0x70)
        self.assertTrue(status["i2c_bus"]["sda_level"])
        self.assertTrue(status["i2c_bus"]["scl_level"])
        self.assertTrue(status["i2c_bus"]["direct_mode"])

    def test_standard_crc_vector_for_modbus_variant(self):
        self.assertEqual(crc16(b"123456789"), 0x4B37)

    def test_sensor_test_configuration_layout(self):
        payload = sensor_test_config_payload({"enabled": True, "baseline_pa": -1.25,
                                              "amplitude_pa": 5.5, "period_ms": 2000,
                                              "pulse_width_ms": 150})
        self.assertEqual(len(payload), 20)
        self.assertEqual(SENSOR_TEST_CONFIG_PAYLOAD.unpack(payload), (1, -1250, 5500, 2000, 150))


if __name__ == "__main__":
    unittest.main()
