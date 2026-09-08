#pragma once

#include "app_types.h"
#include "esp_err.h"

esp_err_t wifi_web_init(void);
void wifi_web_send_samples(uint32_t uptime_ms,
                           const sensor_sample_t samples[SENSOR_COUNT]);
