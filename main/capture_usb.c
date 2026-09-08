#include "capture_usb.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "driver/usb_serial_jtag.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "sensor_test.h"
#include "servo.h"

#define RX_LINE_BYTES 256

static char input[RX_LINE_BYTES];
static size_t input_len;
// Stream by default so opening the native USB port cannot lose the one-shot STREAM command
// while the ESP32-S3 is resetting. Writes are non-blocking when no host is attached.
static bool streaming = true;
static bool announced_to_host;
static uint32_t sequence;

static void send_bytes(const void *data, size_t bytes)
{
    (void)usb_serial_jtag_write_bytes(data, bytes, 0);
}

static void send_text(const char *text)
{
    send_bytes(text, strlen(text));
}

static void send_hello(void)
{
    send_text("{\"type\":\"hello\",\"protocol\":2,\"device\":\"drone-esp32s3\",\"sensors\":5}\r\n");
    announced_to_host = true;
}

static void reply(const char *command, esp_err_t result)
{
    char out[160];
    snprintf(out, sizeof(out),
             "{\"type\":\"reply\",\"command\":\"%s\",\"ok\":%s,\"code\":%ld,\"message\":\"%s\"}\r\n",
             command, result == ESP_OK ? "true" : "false", (long)result,
             result == ESP_OK ? "OK" : esp_err_to_name(result));
    send_text(out);
}

static void send_status(void)
{
    servo_status_t s = servo_get_status();
    char out[320];
    float position_mm = s.pulses_per_meter
                            ? (float)s.position_pulses * 1000.0f / (float)s.pulses_per_meter
                            : 0.0f;
    snprintf(out, sizeof(out),
             "{\"type\":\"status\",\"enabled\":%s,\"stopped\":%s,\"moving\":%s,"
             "\"position_mm\":%.3f,\"pulses_per_mm\":%.3f,\"remaining\":%lu,"
             "\"sensor_test_mode\":%s,\"link_age_ms\":%lu}\r\n",
             s.enabled ? "true" : "false", s.emergency_stop ? "true" : "false",
             s.moving ? "true" : "false", position_mm,
             (float)s.pulses_per_meter / 1000.0f, (unsigned long)s.pulses_remaining,
             sensor_test_is_enabled() ? "true" : "false", (unsigned long)s.link_age_ms);
    send_text(out);
}

static void handle_line(char *line)
{
    char *argv[10] = {0};
    int argc = 0;
    for (char *token = strtok(line, " ,\t"); token && argc < 10;
         token = strtok(NULL, " ,\t")) {
        argv[argc++] = token;
    }
    if (!argc) return;

    if (!strcasecmp(argv[0], "HELLO")) {
        send_hello();
        return;
    }
    if (!strcasecmp(argv[0], "HEARTBEAT")) {
        servo_link_heartbeat();
        // The browser repeats HEARTBEAT, so this also recovers when HELLO/STREAM were
        // sent during a USB-triggered reset. Do not emit five unused replies per second.
        if (!announced_to_host) send_hello();
        return;
    }
    if (!strcasecmp(argv[0], "STREAM") && argc == 2) {
        streaming = atoi(argv[1]) != 0;
        reply("STREAM", ESP_OK);
        return;
    }
    if (!strcasecmp(argv[0], "STATUS")) {
        send_status();
        return;
    }
    if (!strcasecmp(argv[0], "ENABLE") && argc == 2) {
        reply("ENABLE", servo_enable(atoi(argv[1]) != 0));
        return;
    }
    if (!strcasecmp(argv[0], "STOP")) {
        servo_emergency_stop();
        reply("STOP", ESP_OK);
        return;
    }
    if (!strcasecmp(argv[0], "CLEAR")) {
        servo_clear_emergency();
        reply("CLEAR", ESP_OK);
        return;
    }
    if (!strcasecmp(argv[0], "CONFIG") && argc == 5) {
        float min_mm = strtof(argv[1], NULL);
        float max_mm = strtof(argv[2], NULL);
        float pulses_per_mm = strtof(argv[3], NULL);
        float max_speed_mm_s = strtof(argv[4], NULL);
        esp_err_t result = servo_configure_limits(
            (int32_t)(min_mm * 1000.0f), (int32_t)(max_mm * 1000.0f),
            (uint32_t)(pulses_per_mm * 1000.0f), (uint32_t)(max_speed_mm_s * 1000.0f));
        reply("CONFIG", result);
        return;
    }
    if (!strcasecmp(argv[0], "MOVE_MM") && argc == 5) {
        reply("MOVE_MM", servo_move_distance_text(argv[1], argv[2], argv[3], argv[4]));
        return;
    }
    if (!strcasecmp(argv[0], "MOVE") && argc == 5) {
        float target_mm = strtof(argv[1], NULL);
        float speed_mm_s = strtof(argv[2], NULL);
        esp_err_t result = servo_move_to_um((int32_t)(target_mm * 1000.0f),
                                             (uint32_t)(speed_mm_s * 1000.0f),
                                             strtoul(argv[3], NULL, 0),
                                             strtoul(argv[4], NULL, 0));
        reply("MOVE", result);
        return;
    }
    if (!strcasecmp(argv[0], "TEST") && argc == 6) {
        esp_err_t result = sensor_test_configure(
            atoi(argv[1]) != 0, (int32_t)(strtof(argv[2], NULL) * 1000.0f),
            (int32_t)(strtof(argv[3], NULL) * 1000.0f), strtoul(argv[4], NULL, 0),
            strtoul(argv[5], NULL, 0));
        reply("TEST", result);
        return;
    }
    reply(argv[0], ESP_ERR_INVALID_ARG);
}

void capture_usb_init(void)
{
    usb_serial_jtag_driver_config_t config = {
        .tx_buffer_size = 16384,
        .rx_buffer_size = 1024,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&config));
    // This boot announcement can precede host enumeration. The first received
    // heartbeat repeats it if necessary.
    send_text("{\"type\":\"hello\",\"protocol\":2,\"device\":\"drone-esp32s3\",\"sensors\":5}\r\n");
}

void capture_usb_poll(void)
{
    uint8_t bytes[128];
    int count = usb_serial_jtag_read_bytes(bytes, sizeof(bytes), 0);
    for (int index = 0; index < count; index++) {
        uint8_t byte = bytes[index];
        if (byte == '\r' || byte == '\n') {
            if (input_len) {
                input[input_len] = '\0';
                handle_line(input);
                input_len = 0;
            }
        } else if (input_len < sizeof(input) - 1) {
            input[input_len++] = (char)byte;
        } else {
            input_len = 0;
        }
    }
}

void capture_usb_send_samples(uint32_t uptime_ms,
                              const sensor_sample_t samples[SENSOR_COUNT])
{
    if (!streaming) return;
    servo_status_t s = servo_get_status();
    char out[640];
    int used = snprintf(out, sizeof(out),
                        "{\"type\":\"data\",\"ms\":%lu,\"seq\":%lu,\"p\":[",
                        (unsigned long)uptime_ms, (unsigned long)sequence++);
    for (int index = 0; index < SENSOR_COUNT; index++) {
        used += snprintf(out + used, sizeof(out) - (size_t)used, "%s%.4f",
                         index ? "," : "", samples[index].pressure_pa);
    }
    used += snprintf(out + used, sizeof(out) - (size_t)used, "],\"t\":[");
    uint8_t valid_mask = 0;
    for (int index = 0; index < SENSOR_COUNT; index++) {
        used += snprintf(out + used, sizeof(out) - (size_t)used, "%s%.2f",
                         index ? "," : "", samples[index].temperature_c);
        if (samples[index].valid) valid_mask |= 1U << index;
    }
    used += snprintf(out + used, sizeof(out) - (size_t)used, "],\"diag\":[");
    for (int index = 0; index < SENSOR_COUNT; index++)
        used += snprintf(out + used, sizeof(out) - (size_t)used, "%s%u", index ? "," : "",
                         (unsigned)samples[index].diagnostic);
    used += snprintf(out + used, sizeof(out) - (size_t)used, "],\"sensor_errors\":[");
    for (int index = 0; index < SENSOR_COUNT; index++)
        used += snprintf(out + used, sizeof(out) - (size_t)used, "%s%lu", index ? "," : "",
                         (unsigned long)samples[index].errors);
    used += snprintf(out + used, sizeof(out) - (size_t)used, "],\"i2c_errors\":[");
    for (int index = 0; index < SENSOR_COUNT; index++)
        used += snprintf(out + used, sizeof(out) - (size_t)used, "%s%ld", index ? "," : "",
                         (long)samples[index].last_error);
    float position_mm = s.pulses_per_meter
                            ? (float)s.position_pulses * 1000.0f / (float)s.pulses_per_meter
                            : 0.0f;
    snprintf(out + used, sizeof(out) - (size_t)used,
             "],\"valid_mask\":%u,\"position_mm\":%.3f,\"moving\":%s,"
             "\"enabled\":%s,\"stopped\":%s,\"sensor_test_mode\":%s}\r\n",
             valid_mask, position_mm, s.moving ? "true" : "false",
             s.enabled ? "true" : "false", s.emergency_stop ? "true" : "false",
             sensor_test_is_enabled() ? "true" : "false");
    send_text(out);
}
