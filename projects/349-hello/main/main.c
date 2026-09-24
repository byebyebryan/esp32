/*
 * 349-hello - bouncing-ball demo used to bring up the board and tune the
 * display pipeline. The pipeline itself lives in components/display_349; this
 * app only builds the demo UI and measures frame time (see the perf idle
 * reader below).
 */

#include <stdio.h>

#include "display_349.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#include "lvgl.h"

/* Bouncing-ball demo state */
static lv_obj_t *ball = NULL;
static int ball_x = 0, ball_y = 0;
static int ball_dx = 5, ball_dy = 4;
#define BALL_SIZE 48

/*
 * Idle percentage of core 0 (the core running the LVGL task), consumed by the
 * LVGL perf monitor via LV_SYSMON_GET_IDLE (see the compile definition in
 * CMakeLists.txt). Requires CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS with the
 * esp_timer clock source: a 1MHz uint32 counter that wraps every ~71 minutes.
 */
uint32_t my_idle_percent(void)
{
    static uint32_t last_idle = 0;
    static int64_t last_us = 0;

    uint32_t idle = (uint32_t)ulTaskGetIdleRunTimeCounterForCore(0);
    int64_t now = esp_timer_get_time();
    uint32_t pct = 0;

    if (last_us != 0 && now > last_us)
    {
        uint32_t idle_delta = idle - last_idle; /* unsigned subtraction is wrap-safe */
        uint32_t time_delta = (uint32_t)(now - last_us);
        if (time_delta)
        {
            pct = (uint32_t)(((uint64_t)idle_delta * 100) / time_delta);
            if (pct > 100) pct = 100;
        }
    }

    last_idle = idle;
    last_us = now;
    return pct;
}

/*
 * Bouncing-ball demo: constant-speed motion with reflections at the edges. Each
 * 16ms tick invalidates two small rectangles (old and new position), which the
 * shadow framebuffer turns into two small transposes.
 */
static void ball_timer_cb(lv_timer_t *timer)
{
    const int w = lv_obj_get_width(lv_screen_active());
    const int h = lv_obj_get_height(lv_screen_active());

    ball_x += ball_dx;
    ball_y += ball_dy;

    if (ball_x <= 0)
    {
        ball_x = 0;
        ball_dx = -ball_dx;
    }
    else if (ball_x + BALL_SIZE >= w)
    {
        ball_x = w - BALL_SIZE;
        ball_dx = -ball_dx;
    }
    if (ball_y <= 0)
    {
        ball_y = 0;
        ball_dy = -ball_dy;
    }
    else if (ball_y + BALL_SIZE >= h)
    {
        ball_y = h - BALL_SIZE;
        ball_dy = -ball_dy;
    }

    lv_obj_set_pos(ball, ball_x, ball_y);
}

static void demo_ui_create(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x000000), LV_PART_MAIN);

    lv_obj_t *cont = lv_obj_create(scr);
    lv_obj_remove_style_all(cont);
    lv_obj_set_size(cont, LV_PCT(100), LV_PCT(100));
    lv_obj_set_flex_flow(cont, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(cont, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(cont, 6, 0);

    lv_obj_t *title = lv_label_create(cont);
    lv_label_set_text(title, "Hello World");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(title, lv_color_hex(0x33ff99), 0);

    lv_obj_t *sub = lv_label_create(cont);
    lv_label_set_text(sub, "ESP32-S3-Touch-LCD-3.49 V2  |  640x172 landscape");
    lv_obj_set_style_text_font(sub, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(sub, lv_color_hex(0x9fb3c8), 0);

    ball = lv_obj_create(scr);
    lv_obj_remove_style_all(ball);
    lv_obj_set_size(ball, BALL_SIZE, BALL_SIZE);
    lv_obj_set_style_radius(ball, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ball, lv_color_hex(0xff5566), 0);
    lv_obj_set_style_bg_opa(ball, LV_OPA_COVER, 0);
    ball_x = 40;
    ball_y = 40;
    lv_obj_set_pos(ball, ball_x, ball_y);
    lv_obj_move_foreground(ball);

    lv_timer_create(ball_timer_cb, 16, NULL);
}

void app_main(void)
{
    /* Frame-time profiling in display_349 logs at debug level. */
    esp_log_level_set("display349", ESP_LOG_DEBUG);

    ESP_ERROR_CHECK(display_349_init());

    if (display_349_lock(-1))
    {
        demo_ui_create();
        display_349_backlight(100);
        display_349_unlock();
    }
}
