#include <assert.h>
#include <stdio.h>
#include "../main/motion_profile.h"

int main(void)
{
    const uint32_t total = 1890 * 80, ap = 225 * 80, dp = 75 * 80;
    for (uint32_t peak = 80000; peak <= 160000; peak += 40000) {
        assert(distance_profile_frequency(total, 0, peak, 200, ap, dp) == 200);
        assert(distance_profile_frequency(total, ap, peak, 200, ap, dp) == peak);
        assert(distance_profile_frequency(total, total-dp, peak, 200, ap, dp) == peak);
        assert(distance_profile_frequency(total, total-dp+1, peak, 200, ap, dp) < peak);
        assert(distance_profile_frequency(total, total, peak, 200, ap, dp) == 200);
        // At 10 -> 1900 mm, braking always starts at 1825 mm, regardless of speed.
        assert(10 + (total-dp)/80 == 1825);
    }
    uint32_t previous = 200;
    for (uint32_t i = 0; i <= 800; ++i) {
        uint32_t f = distance_profile_frequency(800, i, 80000, 200, 12000, 12000);
        assert(f >= 200 && f < 15000); // Short 10 mm return: reduced triangular peak.
        if (i <= 400) assert(f >= previous); else assert(f <= previous);
        assert(f == distance_profile_frequency(800, 800-i, 80000, 200, 12000, 12000));
        previous = f;
    }
    // Asymmetric overlapping ramps: min(envelopes), never overwrite acceleration.
    assert(distance_profile_frequency(800, 0, 80000, 200, 18000, 6000) == 200);
    assert(distance_profile_frequency(800, 800, 80000, 200, 18000, 6000) == 200);
    assert(distance_profile_frequency(800, 1, 80000, 200, 0, 0) == 80000);
    assert(distance_profile_frequency(1, 0, 200, 200, UINT32_MAX, UINT32_MAX) == 200);
    puts("motion profile: distance boundaries, speeds, short moves and zero ramps passed");
    return 0;
}
