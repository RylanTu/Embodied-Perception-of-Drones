#pragma once

#include <stdint.h>

#include "app_types.h"

void capture_usb_init(void);
void capture_usb_poll(void);
void capture_usb_send_samples(uint32_t uptime_ms,
                              const sensor_sample_t samples[SENSOR_COUNT]);
