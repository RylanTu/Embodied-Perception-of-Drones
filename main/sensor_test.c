#include "sensor_test.h"

#include <string.h>

typedef struct {
    bool enabled;
    int32_t baseline_mpa;
    int32_t amplitude_mpa;
    uint32_t period_ms;
    uint32_t pulse_width_ms;
} sensor_test_state_t;

static sensor_test_state_t state = {
    .enabled = false,
    .baseline_mpa = 0,
    .amplitude_mpa = 5000,
    .period_ms = 2000,
    .pulse_width_ms = 150,
};

esp_err_t sensor_test_configure(bool enabled, int32_t baseline_mpa, int32_t amplitude_mpa,
                                uint32_t period_ms, uint32_t pulse_width_ms)
{
    if (amplitude_mpa < 0 || amplitude_mpa > 1000000 || period_ms < 100 || period_ms > 600000 ||
        pulse_width_ms < 10 || pulse_width_ms >= period_ms) {
        return ESP_ERR_INVALID_ARG;
    }
    state = (sensor_test_state_t){enabled, baseline_mpa, amplitude_mpa, period_ms, pulse_width_ms};
    return ESP_OK;
}

bool sensor_test_is_enabled(void)
{
    return state.enabled;
}

void sensor_test_generate(uint32_t uptime_ms, sensor_sample_t samples[SENSOR_COUNT])
{
    uint32_t phase = uptime_ms % state.period_ms;
    float envelope = 0.0f;
    if (phase < state.pulse_width_ms) {
        uint32_t half = state.pulse_width_ms / 2U;
        if (!half) half = 1;
        envelope = phase <= half ? (float)phase / half
                                 : (float)(state.pulse_width_ms - phase) / (state.pulse_width_ms - half);
    }
    for (int i = 0; i < SENSOR_COUNT; i++) {
        // Deterministic small ripple makes the stream useful for plotting while keeping tests repeatable.
        int32_t ripple_mpa = (int32_t)(((uptime_ms / 10U + (uint32_t)i * 17U) % 41U)) - 20;
        float channel_scale = 1.0f + 0.08f * i;
        samples[i].pressure_pa = (state.baseline_mpa + ripple_mpa) / 1000.0f +
                                 envelope * (state.amplitude_mpa / 1000.0f) * channel_scale;
        samples[i].temperature_c = 25.0f + 0.2f * i;
        samples[i].scale_factor = 60;
        samples[i].valid = true;
        samples[i].diagnostic = SENSOR_DIAG_OK;
        samples[i].errors = 0;
        samples[i].last_error = ESP_OK;
    }
}
