#pragma once
#include <stdbool.h>
#include <stdint.h>
#define SENSOR_COUNT 5
typedef enum { OUTPUT_CSV = 0, OUTPUT_JSON = 1, OUTPUT_BINARY = 2 } output_format_t;
typedef enum { SERVO_PULSE = 0, SERVO_MODBUS = 1 } servo_mode_t;
typedef enum {
    SENSOR_DIAG_OK = 0,
    SENSOR_DIAG_MUX_SELECT = 1,
    SENSOR_DIAG_START_COMMAND = 2,
    SENSOR_DIAG_READ = 3,
    SENSOR_DIAG_PRESSURE_CRC = 4,
    SENSOR_DIAG_TEMPERATURE_CRC = 5,
    SENSOR_DIAG_SCALE_CRC = 6,
    SENSOR_DIAG_SCALE_ZERO = 7,
    SENSOR_DIAG_MUX_NOT_FOUND = 8,
    SENSOR_DIAG_SDA_LOW = 9,
    SENSOR_DIAG_SCL_LOW = 10,
    SENSOR_DIAG_BUS_LINES_LOW = 11,
    SENSOR_DIAG_NOT_ENABLED = 12,
} sensor_diag_t;
typedef struct {
    float pressure_pa;
    float temperature_c;
    uint16_t scale_factor;
    bool valid;
    uint8_t diagnostic;
    uint32_t errors;
    int32_t last_error;
} sensor_sample_t;
typedef struct {
    servo_mode_t mode;
    bool enabled;
    bool emergency_stop;
    bool moving;
    bool direction;
    bool position_trusted;
    uint32_t frequency_hz;
    uint32_t pulses_remaining;
    int32_t position_pulses;
    int32_t soft_min_pulses;
    int32_t soft_max_pulses;
    uint32_t pulses_per_meter;
    uint32_t link_age_ms;
} servo_status_t;
