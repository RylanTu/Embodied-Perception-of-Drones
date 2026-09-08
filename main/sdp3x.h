#pragma once
#include "app_types.h"
#include "esp_err.h"
typedef struct {
    uint8_t sda_gpio;
    uint8_t scl_gpio;
    uint8_t mux_detected_address;
    uint8_t line_flags;
    int32_t mux_probe_error;
} sensor_bus_diag_t;
esp_err_t sensors_init(void);
esp_err_t sensors_start(void);
esp_err_t sensors_read_all(sensor_sample_t samples[SENSOR_COUNT]);
sensor_bus_diag_t sensors_get_bus_diagnostics(void);
