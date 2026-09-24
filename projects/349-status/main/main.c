/*
 * 349-status: USB-Serial-JTAG link + host-composed status bar.
 *
 * The link task parses messages into the state model; the LVGL task (owned by
 * components/display_349) drains the dirty flag and renders. The link task
 * never touches LVGL.
 */

#include "display_349.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "link.h"
#include "proto.h"
#include "rtc.h"
#include "state.h"
#include "ui.h"

static const char *TAG = "349-status";

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
        ESP_LOGI(TAG, "alive, host=%s", link_host_connected() ? "yes" : "no");
    }
}
