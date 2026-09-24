#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#include "esp_err.h"

typedef enum {
    RTC_SOURCE_NONE = 0,
    RTC_SOURCE_HW,
    RTC_SOURCE_FALLBACK,
} rtc_source_t;

/* PCF85063 on I2C0; non-fatal if absent (falls back to the last clock sync). */
esp_err_t rtc_pcf_init(void);

/* Set the hardware RTC from a UTC epoch + offset; always updates the fallback. */
esp_err_t rtc_pcf_set(int64_t epoch_utc, int offset_sec);

/* Local broken-down time; returns where the time came from. */
rtc_source_t rtc_pcf_get_local(struct tm *out);
