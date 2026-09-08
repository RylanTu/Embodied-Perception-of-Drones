#include "servo.h"
#include "motion_profile.h"

#include <limits.h>
#include <math.h>
#include <stdlib.h>

#include "driver/gpio.h"
#include "driver/rmt_tx.h"
#include "driver/uart.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#define TAG "servo"
#define RMT_RESOLUTION_HZ 10000000U
#define MIN_CHUNK_PULSES 8U
#define MAX_CHUNK_PULSES 1024U
#define CHUNK_TARGET_US 10000U

#ifndef CONFIG_DRONE_SERVO_DIR_INVERT
#define CONFIG_DRONE_SERVO_DIR_INVERT 0
#endif

static servo_status_t status = {
    .mode = CONFIG_DRONE_SERVO_MODE,
    .frequency_hz = CONFIG_DRONE_SERVO_DEFAULT_FREQ_HZ,
    .position_trusted = false,
    .soft_min_pulses = 0,
    .soft_max_pulses = 160000,
    .pulses_per_meter = 80000,
};
static SemaphoreHandle_t lock;
static rmt_channel_handle_t rmt_channel_pos;
static rmt_channel_handle_t rmt_channel_neg;
static rmt_encoder_handle_t rmt_encoder_pos;
static rmt_encoder_handle_t rmt_encoder_neg;
static rmt_sync_manager_handle_t rmt_sync_manager;
static bool rmt_sync_used;
static volatile bool stop_requested;
static bool test_wave_active;
static int64_t last_heartbeat_us;
static uint32_t max_speed_um_s = CONFIG_DRONE_SERVO_MAX_SPEED_MM_S * 1000U;

static esp_err_t transmit_pulse_pair(const void *symbols, size_t bytes,
                                     const rmt_transmit_config_t *config)
{
    if (rmt_sync_used) ESP_RETURN_ON_ERROR(rmt_sync_reset(rmt_sync_manager), TAG, "sync reset");
    ESP_RETURN_ON_ERROR(rmt_transmit(rmt_channel_pos, rmt_encoder_pos, symbols, bytes, config),
                        TAG, "Pulse+");
    ESP_RETURN_ON_ERROR(rmt_transmit(rmt_channel_neg, rmt_encoder_neg, symbols, bytes, config),
                        TAG, "Pulse-");
    rmt_sync_used = true;
    ESP_RETURN_ON_ERROR(rmt_tx_wait_all_done(rmt_channel_pos, -1), TAG, "wait Pulse+");
    return rmt_tx_wait_all_done(rmt_channel_neg, -1);
}

static uint16_t modbus_crc(const uint8_t *d, size_t n)
{
    uint16_t c = 0xffff;
    while (n--) {
        c ^= *d++;
        for (int i = 0; i < 8; i++) c = (c & 1) ? (c >> 1) ^ 0xa001 : c >> 1;
    }
    return c;
}

static esp_err_t modbus_exchange(uint8_t *req, size_t n, uint8_t *resp, size_t cap, size_t *out)
{
    if (status.mode != SERVO_MODBUS) return ESP_ERR_NOT_SUPPORTED;
    uint16_t crc = modbus_crc(req, n);
    req[n++] = crc;
    req[n++] = crc >> 8;
    uart_flush_input(CONFIG_DRONE_RS485_UART_NUM);
    if (uart_write_bytes(CONFIG_DRONE_RS485_UART_NUM, req, n) != (int)n) return ESP_FAIL;
    uart_wait_tx_done(CONFIG_DRONE_RS485_UART_NUM, pdMS_TO_TICKS(CONFIG_DRONE_MODBUS_TIMEOUT_MS));
    int got = uart_read_bytes(CONFIG_DRONE_RS485_UART_NUM, resp, cap,
                              pdMS_TO_TICKS(CONFIG_DRONE_MODBUS_TIMEOUT_MS));
    if (got < 5) return ESP_ERR_TIMEOUT;
    uint16_t rcrc = (uint16_t)resp[got - 2] | ((uint16_t)resp[got - 1] << 8);
    if (rcrc != modbus_crc(resp, got - 2)) return ESP_ERR_INVALID_CRC;
    if (resp[0] != CONFIG_DRONE_MODBUS_SLAVE_ADDR || (resp[1] & 0x80)) return ESP_ERR_INVALID_RESPONSE;
    *out = got;
    return ESP_OK;
}

esp_err_t modbus_write_single(uint16_t reg, uint16_t value)
{
    uint8_t q[8] = {CONFIG_DRONE_MODBUS_SLAVE_ADDR, 6, reg >> 8, reg, value >> 8, value};
    uint8_t r[8];
    size_t n;
    return modbus_exchange(q, 6, r, sizeof(r), &n);
}

esp_err_t modbus_read_holding(uint16_t reg, uint16_t count, uint16_t *values)
{
    if (!count || count > 16) return ESP_ERR_INVALID_ARG;
    uint8_t q[8] = {CONFIG_DRONE_MODBUS_SLAVE_ADDR, 3, reg >> 8, reg, count >> 8, count};
    uint8_t r[37];
    size_t n;
    ESP_RETURN_ON_ERROR(modbus_exchange(q, 6, r, sizeof(r), &n), TAG, "read");
    if (n != 5 + count * 2 || r[2] != count * 2) return ESP_ERR_INVALID_SIZE;
    for (int i = 0; i < count; i++) values[i] = ((uint16_t)r[3 + i * 2] << 8) | r[4 + i * 2];
    return ESP_OK;
}

static void set_motion_state(bool moving, uint32_t remaining, int32_t pulse_delta)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    status.moving = moving;
    status.pulses_remaining = remaining;
    status.position_pulses += pulse_delta;
    xSemaphoreGive(lock);
}

typedef struct {
    uint32_t pulses;
    uint32_t freq;
    uint32_t accel_pulses;
    uint32_t decel_pulses;
    bool dir;
} move_args_t;

static uint32_t motion_frequency(const move_args_t *a, uint32_t pulse_index,
                                 uint32_t accel_pulses, uint32_t decel_pulses)
{
    return distance_profile_frequency(a->pulses, pulse_index, a->freq,
                                      CONFIG_DRONE_SERVO_MIN_FREQ_HZ,
                                      accel_pulses, decel_pulses);
}

static void pulse_task(void *arg)
{
    move_args_t a = *(move_args_t *)arg;
    free(arg);
    bool reverse_level = a.dir ^ CONFIG_DRONE_SERVO_DIR_INVERT;
    gpio_set_level(CONFIG_DRONE_SERVO_DIR_POS_GPIO, reverse_level);
    gpio_set_level(CONFIG_DRONE_SERVO_DIR_NEG_GPIO, 0);
    // Allow the direction input to settle before starting the pulse train.
    vTaskDelay(pdMS_TO_TICKS(20));

    uint32_t done = 0;
    uint32_t ap = a.accel_pulses, dp = a.decel_pulses;
    while (done < a.pulses && !stop_requested) {
        uint32_t current_f = motion_frequency(&a, done, ap, dp);
        uint32_t count = (uint32_t)(((uint64_t)current_f * CHUNK_TARGET_US) / 1000000U);
        if (count < MIN_CHUNK_PULSES) count = MIN_CHUNK_PULSES;
        if (count > MAX_CHUNK_PULSES) count = MAX_CHUNK_PULSES;
        if (count > a.pulses - done) count = a.pulses - done;
        // Never let a constant-frequency chunk straddle a ramp boundary.
        if (ap > done && ap - done < count) count = ap - done;
        if (dp < a.pulses && done < a.pulses - dp && a.pulses - dp - done < count)
            count = a.pulses - dp - done;
        uint32_t mid = done + count / 2;
        uint32_t f = motion_frequency(&a, mid, ap, dp);
        uint32_t base_period = RMT_RESOLUTION_HZ / f;
        uint32_t period_remainder = RMT_RESOLUTION_HZ % f;
        uint32_t period_phase = 0;
        if (base_period < 2) base_period = 2;
        rmt_symbol_word_t symbols[MAX_CHUNK_PULSES];
        for (uint32_t i = 0; i < count; i++) {
            uint32_t period = base_period;
            period_phase += period_remainder;
            if (period_phase >= f) {
                period++;
                period_phase -= f;
            }
            uint32_t high = period / 2U, low = period - high;
            symbols[i] = (rmt_symbol_word_t){.level0 = 1, .duration0 = high, .level1 = 0, .duration1 = low};
        }
        rmt_transmit_config_t tx = {.flags.eot_level = 1};
        if (transmit_pulse_pair(symbols, count * sizeof(symbols[0]), &tx) != ESP_OK) break;
        done += count;
        set_motion_state(true, a.pulses - done, a.dir ? (int32_t)count : -(int32_t)count);
    }
    set_motion_state(false, 0, 0);
    vTaskDelete(NULL);
}

esp_err_t servo_init(void)
{
    lock = xSemaphoreCreateMutex();
    if (!lock) return ESP_ERR_NO_MEM;
    gpio_config_t o = {
        .pin_bit_mask = (1ULL << CONFIG_DRONE_SERVO_DIR_POS_GPIO) |
                        (1ULL << CONFIG_DRONE_SERVO_DIR_NEG_GPIO),
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&o), TAG, "GPIO");
    gpio_set_level(CONFIG_DRONE_SERVO_DIR_POS_GPIO, 0);
    gpio_set_level(CONFIG_DRONE_SERVO_DIR_NEG_GPIO, CONFIG_DRONE_SERVO_DIR_INVERT);

    if (status.mode == SERVO_PULSE) {
        rmt_tx_channel_config_t tc = {
            .gpio_num = CONFIG_DRONE_SERVO_PULSE_POS_GPIO,
            .clk_src = RMT_CLK_SRC_DEFAULT,
            .resolution_hz = RMT_RESOLUTION_HZ,
            .mem_block_symbols = 64,
            .trans_queue_depth = 2,
            .flags.init_level = 1,
        };
        ESP_RETURN_ON_ERROR(rmt_new_tx_channel(&tc, &rmt_channel_pos), TAG, "Pulse+ RMT");
        tc.gpio_num = CONFIG_DRONE_SERVO_PULSE_NEG_GPIO;
        tc.flags.invert_out = 1;
        ESP_RETURN_ON_ERROR(rmt_new_tx_channel(&tc, &rmt_channel_neg), TAG, "Pulse- RMT");
        rmt_copy_encoder_config_t ec = {};
        ESP_RETURN_ON_ERROR(rmt_new_copy_encoder(&ec, &rmt_encoder_pos), TAG, "Pulse+ encoder");
        ESP_RETURN_ON_ERROR(rmt_new_copy_encoder(&ec, &rmt_encoder_neg), TAG, "Pulse- encoder");
        ESP_RETURN_ON_ERROR(rmt_enable(rmt_channel_pos), TAG, "enable Pulse+");
        ESP_RETURN_ON_ERROR(rmt_enable(rmt_channel_neg), TAG, "enable Pulse-");
        rmt_channel_handle_t channels[] = {rmt_channel_pos, rmt_channel_neg};
        rmt_sync_manager_config_t sync_config = {
            .tx_channel_array = channels,
            .array_size = 2,
        };
        return rmt_new_sync_manager(&sync_config, &rmt_sync_manager);
    }

    uart_config_t uc = {
        .baud_rate = CONFIG_DRONE_RS485_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = CONFIG_DRONE_RS485_PARITY,
        .stop_bits = CONFIG_DRONE_RS485_STOP_BITS,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_RETURN_ON_ERROR(uart_driver_install(CONFIG_DRONE_RS485_UART_NUM, 256, 0, 0, NULL, 0), TAG, "UART");
    ESP_RETURN_ON_ERROR(uart_param_config(CONFIG_DRONE_RS485_UART_NUM, &uc), TAG, "UART config");
    ESP_RETURN_ON_ERROR(uart_set_pin(CONFIG_DRONE_RS485_UART_NUM, CONFIG_DRONE_RS485_TX_GPIO,
                                     CONFIG_DRONE_RS485_RX_GPIO, CONFIG_DRONE_RS485_DE_GPIO,
                                     UART_PIN_NO_CHANGE), TAG, "pins");
    return uart_set_mode(CONFIG_DRONE_RS485_UART_NUM, UART_MODE_RS485_HALF_DUPLEX);
}

esp_err_t servo_start_test_wave(void)
{
    if (status.mode != SERVO_PULSE) return ESP_ERR_NOT_SUPPORTED;
    if (status.moving || test_wave_active) return ESP_ERR_INVALID_STATE;
    uint32_t period = (RMT_RESOLUTION_HZ + CONFIG_DRONE_SERVO_DEFAULT_FREQ_HZ / 2U) /
                      CONFIG_DRONE_SERVO_DEFAULT_FREQ_HZ;
    uint32_t high = period / 2U, low = period - high;
    rmt_symbol_word_t symbol = {.level0 = 1, .duration0 = high, .level1 = 0, .duration1 = low};
    rmt_transmit_config_t tx = {.loop_count = -1, .flags.eot_level = 1};
    if (rmt_sync_used) ESP_RETURN_ON_ERROR(rmt_sync_reset(rmt_sync_manager), TAG, "sync reset");
    ESP_RETURN_ON_ERROR(rmt_transmit(rmt_channel_pos, rmt_encoder_pos, &symbol, sizeof(symbol), &tx),
                        TAG, "test Pulse+");
    esp_err_t err = rmt_transmit(rmt_channel_neg, rmt_encoder_neg, &symbol, sizeof(symbol), &tx);
    rmt_sync_used = err == ESP_OK;
    if (err == ESP_OK) test_wave_active = true;
    return err;
}

esp_err_t servo_stop_test_wave(void)
{
    if (!test_wave_active) return ESP_OK;
    ESP_RETURN_ON_ERROR(rmt_disable(rmt_channel_pos), TAG, "stop Pulse+");
    ESP_RETURN_ON_ERROR(rmt_disable(rmt_channel_neg), TAG, "stop Pulse-");
    ESP_RETURN_ON_ERROR(rmt_enable(rmt_channel_pos), TAG, "restart Pulse+");
    ESP_RETURN_ON_ERROR(rmt_enable(rmt_channel_neg), TAG, "restart Pulse-");
    test_wave_active = false;
    return ESP_OK;
}

esp_err_t servo_enable(bool en)
{
    if (!en) stop_requested = true;
    xSemaphoreTake(lock, portMAX_DELAY);
    bool forbidden = en && (status.emergency_stop || !last_heartbeat_us ||
                     esp_timer_get_time() - last_heartbeat_us > CONFIG_DRONE_LINK_WATCHDOG_MS * 1000LL);
    if (!forbidden) status.enabled = en;
    xSemaphoreGive(lock);
    if (forbidden) return ESP_ERR_INVALID_STATE;
    return ESP_OK;
}

static esp_err_t servo_move_profile(uint32_t pulses, bool dir, uint32_t freq, uint32_t accel, uint32_t decel)
{
    if (status.mode != SERVO_PULSE) return ESP_ERR_NOT_SUPPORTED;
    xSemaphoreTake(lock, portMAX_DELAY);
    int64_t target = (int64_t)status.position_pulses + (dir ? (int64_t)pulses : -(int64_t)pulses);
    bool invalid = !status.enabled || status.emergency_stop || status.moving || !pulses ||
                   freq < CONFIG_DRONE_SERVO_MIN_FREQ_HZ || freq > CONFIG_DRONE_SERVO_MAX_FREQ_HZ ||
                   target < status.soft_min_pulses || target > status.soft_max_pulses;
    xSemaphoreGive(lock);
    if (invalid) return ESP_ERR_INVALID_STATE;
    ESP_RETURN_ON_ERROR(servo_stop_test_wave(), TAG, "stop test wave");
    move_args_t *a = malloc(sizeof(*a));
    if (!a) return ESP_ERR_NO_MEM;
    *a = (move_args_t){pulses, freq, accel, decel, dir};
    stop_requested = false;
    xSemaphoreTake(lock, portMAX_DELAY);
    status.direction = dir;
    status.frequency_hz = freq;
    status.moving = true;
    status.pulses_remaining = pulses;
    xSemaphoreGive(lock);
    if (xTaskCreate(pulse_task, "servo_move", 8192, a, 8, NULL) != pdPASS) {
        free(a);
        set_motion_state(false, 0, 0);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

// Legacy millisecond interface, retained for existing clients (MOVE / 0x11).
esp_err_t servo_move(uint32_t pulses, bool dir, uint32_t freq, uint32_t accel_ms, uint32_t decel_ms)
{
    uint64_t ap = ((uint64_t)freq + CONFIG_DRONE_SERVO_MIN_FREQ_HZ) * accel_ms / 2000U;
    uint64_t dp = ((uint64_t)freq + CONFIG_DRONE_SERVO_MIN_FREQ_HZ) * decel_ms / 2000U;
    if (ap + dp > pulses) { ap = pulses / 2; dp = pulses - ap; }
    return servo_move_profile(pulses, dir, freq, (uint32_t)ap, (uint32_t)dp);
}

static esp_err_t move_to_um(int32_t target_um, uint32_t speed_um_s,
                            uint32_t accel, uint32_t decel, bool distance_mode)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    uint32_t ppm = status.pulses_per_meter;
    int32_t current = status.position_pulses;
    int32_t min_p = status.soft_min_pulses;
    int32_t max_p = status.soft_max_pulses;
    bool ready = status.enabled && !status.emergency_stop && !status.moving;
    xSemaphoreGive(lock);
    if (!ppm || !speed_um_s || speed_um_s > max_speed_um_s) return ESP_ERR_INVALID_ARG;
    if (!ready) return ESP_ERR_INVALID_STATE;
    int64_t target64 = ((int64_t)target_um * ppm + (target_um >= 0 ? 500000 : -500000)) / 1000000;
    if (target64 < min_p || target64 > max_p || target64 > INT32_MAX || target64 < INT32_MIN) return ESP_ERR_INVALID_ARG;
    int64_t delta = target64 - current;
    if (!delta) return ESP_OK;
    uint64_t freq64 = ((uint64_t)speed_um_s * ppm + 999999) / 1000000;
    if (freq64 < CONFIG_DRONE_SERVO_MIN_FREQ_HZ) freq64 = CONFIG_DRONE_SERVO_MIN_FREQ_HZ;
    if (freq64 > CONFIG_DRONE_SERVO_MAX_FREQ_HZ) return ESP_ERR_INVALID_ARG;
    if (!distance_mode)
        return servo_move((uint32_t)llabs(delta), delta > 0, (uint32_t)freq64, accel, decel);
    uint64_t ap = ((uint64_t)accel * ppm + 500000) / 1000000;
    uint64_t dp = ((uint64_t)decel * ppm + 500000) / 1000000;
    if (ap > UINT32_MAX || dp > UINT32_MAX) return ESP_ERR_INVALID_ARG;
    // A nonzero sub-pulse ramp is represented by at least one pulse.
    if (accel && !ap) ap = 1;
    if (decel && !dp) dp = 1;
    return servo_move_profile((uint32_t)llabs(delta), delta > 0, (uint32_t)freq64,
                              (uint32_t)ap, (uint32_t)dp);
}

esp_err_t servo_move_to_um(int32_t target, uint32_t speed, uint32_t accel_ms, uint32_t decel_ms)
{
    return move_to_um(target, speed, accel_ms, decel_ms, false);
}

esp_err_t servo_move_to_um_distance(int32_t target, uint32_t speed, uint32_t accel_um, uint32_t decel_um)
{
    return move_to_um(target, speed, accel_um, decel_um, true);
}

esp_err_t servo_move_distance_text(const char *target, const char *speed,
                                    const char *accel, const char *decel)
{
    const char *args[] = {target, speed, accel, decel};
    double values[4];
    for (int i = 0; i < 4; ++i) {
        char *end;
        double value = strtod(args[i], &end);
        if (end == args[i] || *end || !isfinite(value)) return ESP_ERR_INVALID_ARG;
        values[i] = round(value * 1000.0);
        if (!isfinite(values[i]) || (i == 0 ?
            (values[i] < INT32_MIN || values[i] > INT32_MAX) :
            (value < 0 || values[i] > UINT32_MAX))) return ESP_ERR_INVALID_ARG;
    }
    return servo_move_to_um_distance((int32_t)values[0], (uint32_t)values[1],
                                     (uint32_t)values[2], (uint32_t)values[3]);
}

esp_err_t servo_configure_limits(int32_t min_um, int32_t max_um, uint32_t pulses_per_meter,
                                 uint32_t new_max_speed_um_s)
{
    if (min_um >= max_um || !pulses_per_meter || !new_max_speed_um_s ||
        new_max_speed_um_s > CONFIG_DRONE_SERVO_MAX_SPEED_MM_S * 1000U) return ESP_ERR_INVALID_ARG;
    int64_t min64 = (int64_t)min_um * pulses_per_meter / 1000000;
    int64_t max64 = (int64_t)max_um * pulses_per_meter / 1000000;
    if (min64 < INT32_MIN || max64 > INT32_MAX) return ESP_ERR_INVALID_ARG;
    xSemaphoreTake(lock, portMAX_DELAY);
    bool invalid = status.moving || status.position_pulses < min64 || status.position_pulses > max64;
    if (!invalid) {
        status.soft_min_pulses = (int32_t)min64;
        status.soft_max_pulses = (int32_t)max64;
        status.pulses_per_meter = pulses_per_meter;
        max_speed_um_s = new_max_speed_um_s;
    }
    xSemaphoreGive(lock);
    return invalid ? ESP_ERR_INVALID_STATE : ESP_OK;
}

void servo_emergency_stop(void)
{
    stop_requested = true;
    (void)servo_stop_test_wave();
    xSemaphoreTake(lock, portMAX_DELAY);
    status.emergency_stop = true;
    status.enabled = false;
    xSemaphoreGive(lock);
}

void servo_clear_emergency(void)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    if (!status.moving) status.emergency_stop = false;
    xSemaphoreGive(lock);
}

void servo_link_heartbeat(void)
{
    last_heartbeat_us = esp_timer_get_time();
}

void servo_watchdog_poll(void)
{
    int64_t now = esp_timer_get_time();
    if (last_heartbeat_us && now - last_heartbeat_us > CONFIG_DRONE_LINK_WATCHDOG_MS * 1000LL) {
        servo_status_t s = servo_get_status();
        if (s.enabled || s.moving) servo_emergency_stop();
    }
}

servo_status_t servo_get_status(void)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    servo_status_t s = status;
    int64_t hb = last_heartbeat_us;
    xSemaphoreGive(lock);
    if (!hb) s.link_age_ms = UINT32_MAX;
    else {
        int64_t age = (esp_timer_get_time() - hb) / 1000;
        s.link_age_ms = age > UINT32_MAX ? UINT32_MAX : (uint32_t)age;
    }
    return s;
}
