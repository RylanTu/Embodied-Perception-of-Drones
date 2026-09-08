#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_types.h"
#include "esp_err.h"

esp_err_t sensor_test_configure(bool enabled, int32_t baseline_mpa, int32_t amplitude_mpa,
                                uint32_t period_ms, uint32_t pulse_width_ms);
bool sensor_test_is_enabled(void);
void sensor_test_generate(uint32_t uptime_ms, sensor_sample_t samples[SENSOR_COUNT]);
