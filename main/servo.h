#pragma once
#include "app_types.h"
#include "esp_err.h"
esp_err_t servo_init(void);
esp_err_t servo_enable(bool enable);
esp_err_t servo_move(uint32_t pulses,bool direction,uint32_t frequency_hz,uint32_t accel_ms,uint32_t decel_ms);
esp_err_t servo_move_to_um(int32_t target_um,uint32_t speed_um_s,uint32_t accel_ms,uint32_t decel_ms);
// New distance-mode interface: acceleration/deceleration distances in micrometres.
esp_err_t servo_move_to_um_distance(int32_t target_um,uint32_t speed_um_s,uint32_t accel_um,uint32_t decel_um);
esp_err_t servo_move_distance_text(const char *target_mm,const char *speed_mm_s,const char *accel_mm,const char *decel_mm);
esp_err_t servo_configure_limits(int32_t min_um,int32_t max_um,uint32_t pulses_per_meter,uint32_t max_speed_um_s);
esp_err_t servo_start_test_wave(void);
esp_err_t servo_stop_test_wave(void);
void servo_emergency_stop(void);
void servo_clear_emergency(void);
void servo_link_heartbeat(void);
void servo_watchdog_poll(void);
servo_status_t servo_get_status(void);
esp_err_t modbus_read_holding(uint16_t reg,uint16_t count,uint16_t *values);
esp_err_t modbus_write_single(uint16_t reg,uint16_t value);
