#pragma once
#include <stdint.h>
#include <math.h>

// Constant acceleration in distance coordinates: v^2 = v0^2 + 2*a*x.
// Taking BOTH envelopes also gives a reduced-speed triangular short move.
static inline uint32_t distance_profile_frequency(uint32_t total, uint32_t index,
                                                  uint32_t peak, uint32_t minimum,
                                                  uint32_t accel, uint32_t decel)
{
    double ratio = 1.0;
    if (accel && index < accel) ratio = (double)index / accel;
    uint32_t left = total - index;
    if (decel && left < decel) {
        double braking = (double)left / decel;
        if (braking < ratio) ratio = braking;
    }
    uint32_t frequency = (uint32_t)sqrt((double)minimum * minimum +
        ((double)peak * peak - (double)minimum * minimum) * ratio);
    return frequency < minimum ? minimum : frequency;
}
