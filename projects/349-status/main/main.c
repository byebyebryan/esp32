/*
 * 349-status: USB-Serial-JTAG link + host-composed status bar.
 *
 * The link task parses messages into the state model; the LVGL task (owned by
 * components/display_349) drains the dirty flag and renders. The link task
 * never touches LVGL.
 */

#include <stdbool.h>
#include <stdint.h>

#include "display_349.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"

#include "link.h"
#include "proto.h"
#include "rtc.h"
#include "state.h"
#include "ui.h"

static const char *TAG = "349-status";

typedef struct {
    uint32_t core0_idle_us;
    uint32_t core1_idle_us;
    int64_t sampled_us;
    bool valid;
} cpu_idle_sample_t;

static cpu_idle_sample_t s_cpu_idle;

static uint32_t idle_percent(uint32_t idle_delta, uint64_t elapsed_us)
{
    uint64_t percent = ((uint64_t)idle_delta * 100U) / elapsed_us;
    return percent > 100U ? 100U : (uint32_t)percent;
}

static bool sample_cpu_idle(uint32_t *core0_percent, uint32_t *core1_percent)
{
    /* The configured ESP Timer runtime clock and esp_timer_get_time() are both
     * in microseconds. Unsigned deltas naturally handle a single 32-bit
     * runtime-counter wrap between these ten-second samples. */
    const uint32_t core0_idle_us = (uint32_t)ulTaskGetIdleRunTimeCounterForCore(0);
    const uint32_t core1_idle_us = (uint32_t)ulTaskGetIdleRunTimeCounterForCore(1);
    const int64_t sampled_us = esp_timer_get_time();

    bool available = false;
    if (s_cpu_idle.valid && sampled_us > s_cpu_idle.sampled_us) {
        const uint64_t elapsed_us = (uint64_t)(sampled_us - s_cpu_idle.sampled_us);
        const uint32_t core0_delta = core0_idle_us - s_cpu_idle.core0_idle_us;
        const uint32_t core1_delta = core1_idle_us - s_cpu_idle.core1_idle_us;
        *core0_percent = idle_percent(core0_delta, elapsed_us);
        *core1_percent = idle_percent(core1_delta, elapsed_us);
        available = true;
    }

    s_cpu_idle.core0_idle_us = core0_idle_us;
    s_cpu_idle.core1_idle_us = core1_idle_us;
    s_cpu_idle.sampled_us = sampled_us;
    s_cpu_idle.valid = true;
    return available;
}

void app_main(void)
{
    state_init();

    esp_log_level_set("display349", ESP_LOG_DEBUG);

    ESP_ERROR_CHECK(display_349_init());
    ESP_ERROR_CHECK(display_349_touch_init());
    ESP_ERROR_CHECK(rtc_pcf_init());

    if (display_349_lock(-1)) {
        ui_init();
        display_349_backlight(100);
        display_349_unlock();
    }

    /* Host sync can arrive as soon as the link starts; the RTC must be ready. */
    ESP_ERROR_CHECK(link_start(proto_handle_line));
    link_set_overflow_cb(proto_handle_overflow);
    proto_send_hello();

    ESP_LOGI(TAG, "link and display up");
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        uint32_t core0_idle_percent;
        uint32_t core1_idle_percent;
        const char *host = link_host_connected() ? "yes" : "no";
        const size_t min_internal = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
        const size_t min_psram = heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM);
        if (sample_cpu_idle(&core0_idle_percent, &core1_idle_percent)) {
            ESP_LOGI(TAG,
                     "alive, host=%s, cpu_idle_core0_pct=%u, cpu_idle_core1_pct=%u, min_internal=%zu, min_psram=%zu",
                     host, core0_idle_percent, core1_idle_percent, min_internal, min_psram);
        } else {
            ESP_LOGI(TAG,
                     "alive, host=%s, cpu_idle_core0_pct=na, cpu_idle_core1_pct=na, min_internal=%zu, min_psram=%zu",
                     host, min_internal, min_psram);
        }
    }
}
