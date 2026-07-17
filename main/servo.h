#pragma once
#include "app_types.h"
#include "esp_err.h"
esp_err_t servo_init(void);
esp_err_t servo_enable(bool enable);
esp_err_t servo_move(uint32_t pulses,bool direction,uint32_t frequency_hz,uint32_t accel_ms,uint32_t decel_ms);
esp_err_t servo_start_test_wave(void);
esp_err_t servo_stop_test_wave(void);
void servo_emergency_stop(void);
void servo_clear_emergency(void);
servo_status_t servo_get_status(void);
esp_err_t modbus_read_holding(uint16_t reg,uint16_t count,uint16_t *values);
esp_err_t modbus_write_single(uint16_t reg,uint16_t value);
