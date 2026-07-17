#pragma once
#include "app_types.h"
void protocol_init(void);void protocol_poll(void);void protocol_send_samples(uint32_t ms,const sensor_sample_t samples[SENSOR_COUNT]);
