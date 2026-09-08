#include "protocol.h"

#include <string.h>

#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "servo.h"
#include "sdp3x.h"
#include "sensor_test.h"
#include "sdkconfig.h"

#define TAG "link"
#define LINK_MAGIC 0xA55AU
#define LINK_VERSION 2U
#define HEADER_BYTES 8U
#define CRC_BYTES 2U
#define MAX_PAYLOAD 96U
#define RX_CAPACITY 256U

enum {
    MSG_TELEMETRY = 0x01,
    MSG_HEARTBEAT = 0x02,
    MSG_STATUS = 0x03,
    MSG_CONFIG_MOTION = 0x10,
    MSG_MOVE_ABSOLUTE = 0x11,
    MSG_STOP = 0x12,
    MSG_ENABLE = 0x13,
    MSG_CLEAR_STOP = 0x14,
    MSG_CONFIG_SENSOR_TEST = 0x15,
    MSG_MOVE_DISTANCE = 0x16,
    MSG_ACK = 0x7e,
};

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t version;
    uint8_t type;
    uint16_t sequence;
    uint16_t payload_bytes;
} frame_header_t;

typedef struct __attribute__((packed)) {
    uint32_t uptime_ms;
    uint32_t sample_sequence;
    int32_t pressure_mpa[SENSOR_COUNT];
    uint8_t valid_mask;
    uint8_t state_flags;
    int32_t position_pulses;
    uint32_t pulses_per_meter;
} telemetry_t;

typedef struct __attribute__((packed)) {
    uint32_t uptime_ms;
    int16_t temperature_cc[SENSOR_COUNT];
    uint16_t sample_rate_hz;
    int32_t soft_min_pulses;
    int32_t soft_max_pulses;
    uint32_t pulses_per_meter;
    uint32_t pulses_remaining;
    uint32_t link_age_ms;
    uint8_t sensor_diagnostic[SENSOR_COUNT];
    uint8_t reserved[3];
    uint32_t sensor_error_count[SENSOR_COUNT];
    int32_t sensor_last_error[SENSOR_COUNT];
    uint8_t i2c_sda_gpio;
    uint8_t i2c_scl_gpio;
    uint8_t mux_detected_address;
    uint8_t i2c_line_flags;
    int32_t mux_probe_error;
} status_t;

_Static_assert(sizeof(telemetry_t) == 38, "telemetry protocol layout mismatch");
_Static_assert(sizeof(status_t) == 92, "status protocol layout mismatch");

typedef struct __attribute__((packed)) {
    int32_t min_um;
    int32_t max_um;
    uint32_t pulses_per_meter;
    uint32_t max_speed_um_s;
} motion_config_t;

typedef struct __attribute__((packed)) {
    int32_t target_um;
    uint32_t speed_um_s;
    uint32_t accel_ms;
    uint32_t decel_ms;
} move_command_t;

typedef struct __attribute__((packed)) {
    uint8_t enabled;
    uint8_t reserved[3];
    int32_t baseline_mpa;
    int32_t amplitude_mpa;
    uint32_t period_ms;
    uint32_t pulse_width_ms;
} sensor_test_config_t;

typedef struct __attribute__((packed)) {
    uint8_t request_type;
    uint8_t reserved;
    int32_t result;
} ack_t;

static uint8_t rx_buffer[RX_CAPACITY];
static size_t rx_bytes;
static uint16_t tx_sequence;
static uint32_t sample_sequence;

static uint16_t crc16(const uint8_t *p, size_t n)
{
    uint16_t c = 0xffff;
    while (n--) {
        c ^= *p++;
        for (int i = 0; i < 8; i++) c = (c & 1) ? (c >> 1) ^ 0xa001 : c >> 1;
    }
    return c;
}

static esp_err_t send_frame(uint8_t type, uint16_t sequence, const void *payload, uint16_t payload_bytes)
{
    if (payload_bytes > MAX_PAYLOAD) return ESP_ERR_INVALID_SIZE;
    uint8_t frame[HEADER_BYTES + MAX_PAYLOAD + CRC_BYTES];
    frame_header_t h = {
        .magic = LINK_MAGIC,
        .version = LINK_VERSION,
        .type = type,
        .sequence = sequence,
        .payload_bytes = payload_bytes,
    };
    memcpy(frame, &h, sizeof(h));
    if (payload_bytes) memcpy(frame + sizeof(h), payload, payload_bytes);
    uint16_t crc = crc16(frame, sizeof(h) + payload_bytes);
    memcpy(frame + sizeof(h) + payload_bytes, &crc, sizeof(crc));
    size_t total = sizeof(h) + payload_bytes + sizeof(crc);
    return uart_write_bytes(CONFIG_DRONE_LINK_UART_NUM, frame, total) == (int)total ? ESP_OK : ESP_FAIL;
}

static void send_ack(uint16_t sequence, uint8_t request_type, esp_err_t result)
{
    ack_t ack = {.request_type = request_type, .result = result};
    (void)send_frame(MSG_ACK, sequence, &ack, sizeof(ack));
}

static void handle_frame(const frame_header_t *h, const uint8_t *payload)
{
    esp_err_t result = ESP_OK;
    switch (h->type) {
        case MSG_HEARTBEAT:
            if (h->payload_bytes != 0) result = ESP_ERR_INVALID_SIZE;
            else servo_link_heartbeat();
            break;
        case MSG_CONFIG_MOTION:
            if (h->payload_bytes != sizeof(motion_config_t)) result = ESP_ERR_INVALID_SIZE;
            else {
                motion_config_t c;
                memcpy(&c, payload, sizeof(c));
                result = servo_configure_limits(c.min_um, c.max_um, c.pulses_per_meter,
                                                c.max_speed_um_s);
            }
            break;
        case MSG_MOVE_ABSOLUTE:
        case MSG_MOVE_DISTANCE:
            if (h->payload_bytes != sizeof(move_command_t)) result = ESP_ERR_INVALID_SIZE;
            else {
                move_command_t m;
                memcpy(&m, payload, sizeof(m));
                // 0x11: milliseconds; 0x16: same packed layout, last two fields are um.
                result = h->type == MSG_MOVE_DISTANCE ?
                    servo_move_to_um_distance(m.target_um, m.speed_um_s, m.accel_ms, m.decel_ms) :
                    servo_move_to_um(m.target_um, m.speed_um_s, m.accel_ms, m.decel_ms);
            }
            break;
        case MSG_STOP:
            if (h->payload_bytes != 0) result = ESP_ERR_INVALID_SIZE;
            else servo_emergency_stop();
            break;
        case MSG_ENABLE:
            if (h->payload_bytes != 1) result = ESP_ERR_INVALID_SIZE;
            else result = servo_enable(payload[0] != 0);
            break;
        case MSG_CLEAR_STOP:
            if (h->payload_bytes != 0) result = ESP_ERR_INVALID_SIZE;
            else servo_clear_emergency();
            break;
        case MSG_CONFIG_SENSOR_TEST:
            if (h->payload_bytes != sizeof(sensor_test_config_t)) result = ESP_ERR_INVALID_SIZE;
            else {
                sensor_test_config_t c;
                memcpy(&c, payload, sizeof(c));
                result = sensor_test_configure(c.enabled != 0, c.baseline_mpa, c.amplitude_mpa,
                                               c.period_ms, c.pulse_width_ms);
            }
            break;
        default:
            result = ESP_ERR_NOT_SUPPORTED;
            break;
    }
    send_ack(h->sequence, h->type, result);
}

static void parse_rx(void)
{
    while (rx_bytes >= HEADER_BYTES) {
        frame_header_t h;
        memcpy(&h, rx_buffer, sizeof(h));
        if (h.magic != LINK_MAGIC) {
            memmove(rx_buffer, rx_buffer + 1, --rx_bytes);
            continue;
        }
        if (h.version != LINK_VERSION || h.payload_bytes > MAX_PAYLOAD) {
            memmove(rx_buffer, rx_buffer + 2, rx_bytes - 2);
            rx_bytes -= 2;
            continue;
        }
        size_t frame_bytes = sizeof(h) + h.payload_bytes + CRC_BYTES;
        if (rx_bytes < frame_bytes) return;
        uint16_t received_crc;
        memcpy(&received_crc, rx_buffer + sizeof(h) + h.payload_bytes, sizeof(received_crc));
        if (received_crc == crc16(rx_buffer, sizeof(h) + h.payload_bytes)) {
            handle_frame(&h, rx_buffer + sizeof(h));
            memmove(rx_buffer, rx_buffer + frame_bytes, rx_bytes - frame_bytes);
            rx_bytes -= frame_bytes;
        } else {
            memmove(rx_buffer, rx_buffer + 1, --rx_bytes);
        }
    }
}

void protocol_init(void)
{
    uart_config_t c = {
        .baud_rate = CONFIG_DRONE_LINK_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(CONFIG_DRONE_LINK_UART_NUM, 1024, 1024, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(CONFIG_DRONE_LINK_UART_NUM, &c));
    ESP_ERROR_CHECK(uart_set_pin(CONFIG_DRONE_LINK_UART_NUM, CONFIG_DRONE_LINK_TX_GPIO,
                                 CONFIG_DRONE_LINK_RX_GPIO, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_LOGI(TAG, "binary UART link: UART%d TX=%d RX=%d baud=%d",
             CONFIG_DRONE_LINK_UART_NUM, CONFIG_DRONE_LINK_TX_GPIO,
             CONFIG_DRONE_LINK_RX_GPIO, CONFIG_DRONE_LINK_BAUD_RATE);
}

void protocol_poll(void)
{
    uint8_t incoming[128];
    int n = uart_read_bytes(CONFIG_DRONE_LINK_UART_NUM, incoming, sizeof(incoming), 0);
    if (n <= 0) return;
    if ((size_t)n > sizeof(rx_buffer) - rx_bytes) {
        rx_bytes = 0;
        if ((size_t)n > sizeof(rx_buffer)) return;
    }
    memcpy(rx_buffer + rx_bytes, incoming, n);
    rx_bytes += n;
    parse_rx();
}

void protocol_send_samples(uint32_t ms, const sensor_sample_t s[SENSOR_COUNT])
{
    servo_status_t st = servo_get_status();
    uint32_t sequence = sample_sequence++;
    telemetry_t t = {
        .uptime_ms = ms,
        .sample_sequence = sequence,
        .position_pulses = st.position_pulses,
        .pulses_per_meter = st.pulses_per_meter,
    };
    for (int i = 0; i < SENSOR_COUNT; i++) {
        t.pressure_mpa[i] = (int32_t)(s[i].pressure_pa * 1000.0f);
        if (s[i].valid) t.valid_mask |= 1U << i;
    }
    if (st.enabled) t.state_flags |= 1U << 0;
    if (st.emergency_stop) t.state_flags |= 1U << 1;
    if (st.moving) t.state_flags |= 1U << 2;
    if (st.direction) t.state_flags |= 1U << 3;
    if (st.position_trusted) t.state_flags |= 1U << 4;
    if (sensor_test_is_enabled()) t.state_flags |= 1U << 5;
    (void)send_frame(MSG_TELEMETRY, tx_sequence++, &t, sizeof(t));

    // Diagnostics are intentionally limited to 2 Hz. The 200 Hz pressure
    // stream already occupies most of a 115200-baud link.
    const uint32_t status_divider = CONFIG_DRONE_SAMPLE_RATE_HZ >= 2 ?
                                    CONFIG_DRONE_SAMPLE_RATE_HZ / 2U : 1U;
    if (sequence % status_divider == 0) {
        sensor_bus_diag_t bus_diag = sensors_get_bus_diagnostics();
        status_t status = {
            .uptime_ms = ms,
            .sample_rate_hz = CONFIG_DRONE_SAMPLE_RATE_HZ,
            .soft_min_pulses = st.soft_min_pulses,
            .soft_max_pulses = st.soft_max_pulses,
            .pulses_per_meter = st.pulses_per_meter,
            .pulses_remaining = st.pulses_remaining,
            .link_age_ms = st.link_age_ms,
            .i2c_sda_gpio = bus_diag.sda_gpio,
            .i2c_scl_gpio = bus_diag.scl_gpio,
            .mux_detected_address = bus_diag.mux_detected_address,
            .i2c_line_flags = bus_diag.line_flags,
            .mux_probe_error = bus_diag.mux_probe_error,
        };
        for (int i = 0; i < SENSOR_COUNT; i++) {
            status.temperature_cc[i] = (int16_t)(s[i].temperature_c * 100.0f);
            status.sensor_diagnostic[i] = s[i].diagnostic;
            status.sensor_error_count[i] = s[i].errors;
            status.sensor_last_error[i] = s[i].last_error;
        }
        (void)send_frame(MSG_STATUS, tx_sequence++, &status, sizeof(status));
    }
}
