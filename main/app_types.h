#pragma once
#include <stdbool.h>
#include <stdint.h>
#define SENSOR_COUNT 5
typedef enum { OUTPUT_CSV = 0, OUTPUT_JSON = 1, OUTPUT_BINARY = 2 } output_format_t;
typedef enum { SERVO_PULSE = 0, SERVO_MODBUS = 1 } servo_mode_t;
typedef struct { float pressure_pa; float temperature_c; uint16_t scale_factor; bool valid; uint32_t errors; } sensor_sample_t;
typedef struct { servo_mode_t mode; bool enabled; bool emergency_stop; bool moving; bool direction; uint32_t frequency_hz; uint32_t pulses_remaining; } servo_status_t;
