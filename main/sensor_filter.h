#pragma once

#include "app_types.h"

void sensor_filter_init(void);
void sensor_filter_apply(sensor_sample_t samples[SENSOR_COUNT]);
void sensor_filter_reset(void);
