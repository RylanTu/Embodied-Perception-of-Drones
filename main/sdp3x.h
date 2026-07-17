#pragma once
#include "app_types.h"
#include "esp_err.h"
esp_err_t sensors_init(void);
esp_err_t sensors_start(void);
esp_err_t sensors_read_all(sensor_sample_t samples[SENSOR_COUNT]);
