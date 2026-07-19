#include "sensor_filter.h"

#include <string.h>
#include "sdkconfig.h"

#define FILTER_MAX_WINDOW 32

typedef struct {
    bool initialized;
    float ema;
    float kalman_estimate;
    float kalman_covariance;
    float history[FILTER_MAX_WINDOW];
    float sum;
    uint8_t index;
    uint8_t count;
} filter_state_t;

static filter_state_t states[SENSOR_COUNT];

void sensor_filter_init(void)
{
    sensor_filter_reset();
}

void sensor_filter_reset(void)
{
    memset(states, 0, sizeof(states));
}

void sensor_filter_apply(sensor_sample_t samples[SENSOR_COUNT])
{
#if CONFIG_DRONE_FILTER_NONE
    (void)samples;
#else
    for (int i = 0; i < SENSOR_COUNT; ++i) {
        if (!samples[i].valid) {
            continue;
        }

        filter_state_t *state = &states[i];
        const float input = samples[i].pressure_pa;

#if CONFIG_DRONE_FILTER_EMA
        if (!state->initialized) {
            state->ema = input;
            state->initialized = true;
        } else {
            const float alpha = CONFIG_DRONE_FILTER_EMA_ALPHA_PERMILLE / 1000.0f;
            state->ema += alpha * (input - state->ema);
        }
        samples[i].pressure_pa = state->ema;
#elif CONFIG_DRONE_FILTER_MOVING_AVERAGE
        const uint8_t window = CONFIG_DRONE_FILTER_WINDOW_SIZE;
        if (state->count < window) {
            state->history[state->index] = input;
            state->sum += input;
            state->count++;
        } else {
            state->sum -= state->history[state->index];
            state->history[state->index] = input;
            state->sum += input;
        }
        state->index = (uint8_t)((state->index + 1) % window);
        samples[i].pressure_pa = state->sum / state->count;
#elif CONFIG_DRONE_FILTER_KALMAN
        const float process_noise =
            CONFIG_DRONE_FILTER_KALMAN_Q_MILLI / 1000.0f;
        const float measurement_noise =
            CONFIG_DRONE_FILTER_KALMAN_R_MILLI / 1000.0f;

        if (!state->initialized) {
            state->kalman_estimate = input;
            state->kalman_covariance = measurement_noise;
            state->initialized = true;
        } else {
            state->kalman_covariance += process_noise;
            const float gain = state->kalman_covariance /
                               (state->kalman_covariance + measurement_noise);
            state->kalman_estimate += gain *
                                      (input - state->kalman_estimate);
            state->kalman_covariance *= 1.0f - gain;
        }
        samples[i].pressure_pa = state->kalman_estimate;
#endif
    }
#endif
}
