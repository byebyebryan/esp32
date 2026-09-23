/*
 * Minimal hello-world display test for the ESP32-S3-Touch-LCD-3.49 V2.
 *
 * Board bring-up (QSPI AXS15231B panel, TCA9554 expander, backlight, touch)
 * adapted from Waveshare's 10_LVGL_V9_Test example (Apache-2.0).
 *
 * The AXS15231B QSPI path does not support hardware rotation (Waveshare's own
 * config notes "software rotation"), so the 640x172 landscape view comes from
 * LVGL's 90-degree display rotation. The flush callback fuses the transpose,
 * the RGB565 byte swap and the chunk copy into a single cache-friendly pass:
 * LVGL's generic rotate fallback is a scalar transpose with strided reads and
 * dominated the frame time (~64ms of a ~90ms frame).
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/idf_additions.h"
#include "driver/spi_master.h"
#include "esp_timer.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_async_memcpy.h"
#include "esp_io_expander_tca9554.h"

#include "lvgl.h"
#include "esp_lcd_axs15231b.h"
#include "user_config.h"
#include "i2c_bsp.h"
#include "lcd_bl_pwm_bsp.h"

static const char *TAG = "349-hello";

static SemaphoreHandle_t lvgl_mux = NULL;
static SemaphoreHandle_t flush_done_semaphore = NULL;

static uint16_t *trans_buf[2];
static uint16_t *shadow = NULL;

/*
 * The shadow lives in PSRAM, and a plain memcpy of 220KB per frame costs about
 * as much CPU as the transpose did. Use the GDMA-backed async memcpy instead:
 * the copy runs on its own DMA engine (overlapping the SPI transfer) and the
 * CPU only waits on a semaphore.
 */
static async_memcpy_handle_t s_mcp = NULL;
static SemaphoreHandle_t s_mcp_done = NULL;

static bool mcp_done_cb(async_memcpy_handle_t mcp, async_memcpy_event_t *event, void *cb_args)
{
    BaseType_t high_task_woken = pdFALSE;
    xSemaphoreGiveFromISR(s_mcp_done, &high_task_woken);
    return high_task_woken == pdTRUE;
}

/* Dirty areas above this many pixels rebuild the whole shadow instead of
 * transposing the rectangle (the strided rectangle transpose loses to the
 * cache-friendly full rebuild beyond roughly this size). */
#define SHADOW_REBUILD_MAX_PIXELS 16384
static esp_io_expander_handle_t io_expander = NULL;

/* Bouncing-ball demo state */
static lv_obj_t *ball = NULL;
static int ball_x = 0, ball_y = 0;
static int ball_dx = 5, ball_dy = 4;
#define BALL_SIZE 48

static int64_t prof_flush = 0, prof_period = 0;
static int64_t prof_period_min = INT64_MAX;
static int64_t last_flush_start = 0;
static int prof_frames = 0;

#define LCD_BIT_PER_PIXEL 16
#define BYTES_PER_PIXEL (LV_COLOR_FORMAT_GET_SIZE(LV_COLOR_FORMAT_RGB565))
#define BUFF_SIZE (DISP_H_RES * DISP_V_RES * BYTES_PER_PIXEL)

#define LVGL_TICK_PERIOD_MS    5
#define LVGL_TASK_MAX_DELAY_MS 500
#define LVGL_TASK_MIN_DELAY_MS 5
#define LVGL_TASK_STACK_SIZE   (8 * 1024)
#define LVGL_TASK_PRIORITY     2

static void example_lcd_exio_init(void);
static void example_lcd_reset(void);
static void example_lcd_backlight_set(bool enable);

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

static const axs15231b_lcd_init_cmd_t lcd_init_cmds[] =
{
    {0x11, (uint8_t []){0x00}, 0, 100},
    {0x29, (uint8_t []){0x00}, 0, 100},
};

static bool example_notify_lvgl_flush_ready(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_io_event_data_t *edata, void *user_ctx)
{
    BaseType_t high_task_awoken = pdFALSE;
    xSemaphoreGiveFromISR(flush_done_semaphore, &high_task_awoken);
    return false;
}

static void example_lcd_exio_init(void)
{
    i2c_master_bus_handle_t tca9554_i2c_bus = NULL;
    ESP_ERROR_CHECK(i2c_master_get_bus_handle(0, &tca9554_i2c_bus));
    ESP_ERROR_CHECK(esp_io_expander_new_i2c_tca9554(tca9554_i2c_bus, ESP_IO_EXPANDER_I2C_TCA9554_ADDRESS_000, &io_expander));
    ESP_ERROR_CHECK(esp_io_expander_set_dir(io_expander, EXAMPLE_EXIO_PIN_TOUCH_INT, IO_EXPANDER_INPUT));
    ESP_ERROR_CHECK(esp_io_expander_set_dir(io_expander, EXAMPLE_EXIO_PIN_BL_EN | EXAMPLE_EXIO_PIN_LCD_RST, IO_EXPANDER_OUTPUT));
    ESP_ERROR_CHECK(esp_io_expander_set_level(io_expander, EXAMPLE_EXIO_PIN_BL_EN, 0));
    ESP_ERROR_CHECK(esp_io_expander_set_level(io_expander, EXAMPLE_EXIO_PIN_LCD_RST, 1));
}

static void example_lcd_reset(void)
{
    ESP_ERROR_CHECK(esp_io_expander_set_level(io_expander, EXAMPLE_EXIO_PIN_LCD_RST, 1));
    vTaskDelay(pdMS_TO_TICKS(30));
    ESP_ERROR_CHECK(esp_io_expander_set_level(io_expander, EXAMPLE_EXIO_PIN_LCD_RST, 0));
    vTaskDelay(pdMS_TO_TICKS(250));
    ESP_ERROR_CHECK(esp_io_expander_set_level(io_expander, EXAMPLE_EXIO_PIN_LCD_RST, 1));
    vTaskDelay(pdMS_TO_TICKS(30));
}

static void example_lcd_backlight_set(bool enable)
{
    ESP_ERROR_CHECK(esp_io_expander_set_level(io_expander, EXAMPLE_EXIO_PIN_BL_EN, enable ? 1 : 0));
}

/*
 * Shadow framebuffer in the panel's native orientation (172x640, big-endian
 * RGB565), kept in sync incrementally: each flush transposes only its dirty
 * rectangle into the shadow, so the 90-degree transpose cost is proportional to
 * what changed instead of a full 110K-pixel pass per frame. Sending a frame is
 * then a plain copy of the shadow - the panel still requires complete frames,
 * but there is no per-frame transpose.
 */
static void shadow_update(const lv_area_t *area, const uint16_t *src)
{
    /* UI (landscape 640x172) -> native (172x640):
     *   native_x = ui_y,  native_y = 639 - ui_x */
    const int nx1 = area->y1;
    const int nx2 = area->y2;
    const int ny1 = (DISP_H_RES - 1) - area->x2;
    const int ny2 = (DISP_H_RES - 1) - area->x1;

    for (int ny = ny1; ny <= ny2; ny++)
    {
        const int u = (DISP_H_RES - 1) - ny;
        uint16_t *dst = shadow + (size_t)ny * EXAMPLE_LCD_H_RES;
        for (int v = nx1; v <= nx2; v++)
        {
            uint16_t px = src[(size_t)v * DISP_H_RES + u];
            dst[v] = (uint16_t)((px >> 8) | (px << 8));
        }
    }
}

/*
 * Full shadow rebuild with 32-bit loads: one word covers two horizontally
 * adjacent UI pixels, which land in two consecutive native rows. Used for large
 * areas, where the cache-friendly rebuild beats the strided rectangle
 * transpose.
 */
static void shadow_rebuild(const uint16_t *src)
{
    const int flush_coun = (LVGL_SPIRAM_BUFF_LEN / LVGL_DMA_BUFF_LEN);
    const int rows_per_chunk = (EXAMPLE_LCD_V_RES / flush_coun);

    for (int c = 0; c < flush_coun; c++)
    {
        const int y0 = c * rows_per_chunk;
        for (int v = 0; v < EXAMPLE_LCD_H_RES; v++)
        {
            const uint16_t *src_row = src + (size_t)v * DISP_H_RES;
            const uint32_t *src32 = (const uint32_t *)(src_row + (EXAMPLE_LCD_V_RES - 1 - y0 - (rows_per_chunk - 1)));
            uint16_t *dst_col = shadow + v;
            for (int j = 0; j < rows_per_chunk / 2; j++)
            {
                uint32_t w = src32[j];
                uint16_t p0 = (uint16_t)w;
                uint16_t p1 = (uint16_t)(w >> 16);
                dst_col[(y0 + rows_per_chunk - 1 - 2 * j) * EXAMPLE_LCD_H_RES] = (uint16_t)((p0 >> 8) | (p0 << 8));
                dst_col[(y0 + rows_per_chunk - 2 - 2 * j) * EXAMPLE_LCD_H_RES] = (uint16_t)((p1 >> 8) | (p1 << 8));
            }
        }
    }
}

/*
 * Send the whole shadow to the panel in full-width row chunks. Two chunk
 * buffers let the copy of chunk c overlap the DMA transfer of chunk c-1.
 */
static void lcd_send_shadow(esp_lcd_panel_handle_t panel_handle)
{
    const int flush_coun = (LVGL_SPIRAM_BUFF_LEN / LVGL_DMA_BUFF_LEN);
    const int rows_per_chunk = (EXAMPLE_LCD_V_RES / flush_coun);

    for (int c = 0; c < flush_coun; c++)
    {
        if (c >= 2)
        {
            xSemaphoreTake(flush_done_semaphore, portMAX_DELAY);
        }

        uint16_t *chunk = trans_buf[c & 1];
        ESP_ERROR_CHECK(esp_async_memcpy(s_mcp, chunk, shadow + (size_t)c * rows_per_chunk * EXAMPLE_LCD_H_RES,
                                         LVGL_DMA_BUFF_LEN, mcp_done_cb, NULL));
        xSemaphoreTake(s_mcp_done, portMAX_DELAY);
        esp_lcd_panel_draw_bitmap(panel_handle, 0, c * rows_per_chunk, EXAMPLE_LCD_H_RES, (c + 1) * rows_per_chunk, chunk);
    }
    xSemaphoreTake(flush_done_semaphore, portMAX_DELAY);
    xSemaphoreTake(flush_done_semaphore, portMAX_DELAY);
}

static void example_lvgl_flush_cb(lv_display_t * disp, const lv_area_t * area, uint8_t * color_p)
{
    esp_lcd_panel_handle_t panel_handle = (esp_lcd_panel_handle_t)lv_display_get_user_data(disp);
    const uint16_t *src = (const uint16_t *)color_p;

    const int area_px = (area->x2 - area->x1 + 1) * (area->y2 - area->y1 + 1);
    if (area_px > SHADOW_REBUILD_MAX_PIXELS)
    {
        shadow_rebuild(src);
    }
    else
    {
        shadow_update(area, src);
    }

    /*
     * The panel only accepts complete frames, so send the shadow once per
     * refresh cycle on the last flush: by then every rendered area has been
     * applied to it.
     */
    if (lv_display_flush_is_last(disp))
    {
        int64_t t_start = esp_timer_get_time();
        if (last_flush_start)
        {
            int64_t period = t_start - last_flush_start;
            prof_period += period;
            if (period < prof_period_min)
            {
                prof_period_min = period;
            }
        }
        last_flush_start = t_start;

        lcd_send_shadow(panel_handle);

        prof_flush += esp_timer_get_time() - t_start;
        if (++prof_frames >= 30)
        {
            ESP_LOGI(TAG, "frame us: period=%d min=%d flush=%d",
                     (int)(prof_period / 30), (int)prof_period_min, (int)(prof_flush / 30));
            prof_period = prof_flush = 0;
            prof_period_min = INT64_MAX;
            prof_frames = 0;
        }
    }
    lv_disp_flush_ready(disp);
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

static void example_increase_lvgl_tick(void *arg)
{
    lv_tick_inc(LVGL_TICK_PERIOD_MS);
}

static bool example_lvgl_lock(int timeout_ms)
{
    assert(lvgl_mux && "bsp_display_start must be called first");

    const TickType_t timeout_ticks = (timeout_ms == -1) ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    return xSemaphoreTake(lvgl_mux, timeout_ticks) == pdTRUE;
}

static void example_lvgl_unlock(void)
{
    assert(lvgl_mux && "bsp_display_start must be called first");
    xSemaphoreGive(lvgl_mux);
}

static void example_lvgl_port_task(void *arg)
{
    uint32_t task_delay_ms = LVGL_TASK_MAX_DELAY_MS;
    for(;;)
    {
        if (example_lvgl_lock(-1))
        {
            task_delay_ms = lv_timer_handler();
            example_lvgl_unlock();
        }
        if (task_delay_ms > LVGL_TASK_MAX_DELAY_MS)
        {
            task_delay_ms = LVGL_TASK_MAX_DELAY_MS;
        }
        else if (task_delay_ms < LVGL_TASK_MIN_DELAY_MS)
        {
            task_delay_ms = LVGL_TASK_MIN_DELAY_MS;
        }
        vTaskDelay(pdMS_TO_TICKS(task_delay_ms));
    }
}

static void demo_ui_create(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0b1020), LV_PART_MAIN);

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
    async_memcpy_config_t mcp_cfg = ASYNC_MEMCPY_DEFAULT_CONFIG();
    ESP_ERROR_CHECK(esp_async_memcpy_install(&mcp_cfg, &s_mcp));
    s_mcp_done = xSemaphoreCreateBinary();
    assert(s_mcp_done);

    lcd_bl_pwm_bsp_init(LCD_PWM_MODE_255);
    flush_done_semaphore = xSemaphoreCreateBinary();
    assert(flush_done_semaphore);
    touch_i2c_master_Init(); /* also brings up I2C port 0 for the TCA9554 */
    example_lcd_exio_init();
    ESP_LOGI(TAG, "Initialize SPI bus");

    spi_bus_config_t buscfg = {};
    buscfg.sclk_io_num =  EXAMPLE_PIN_NUM_LCD_PCLK;
    buscfg.data0_io_num = EXAMPLE_PIN_NUM_LCD_DATA0;
    buscfg.data1_io_num = EXAMPLE_PIN_NUM_LCD_DATA1;
    buscfg.data2_io_num = EXAMPLE_PIN_NUM_LCD_DATA2;
    buscfg.data3_io_num = EXAMPLE_PIN_NUM_LCD_DATA3;
    buscfg.max_transfer_sz = LVGL_DMA_BUFF_LEN;
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    ESP_LOGI(TAG, "Install panel IO");
    esp_lcd_panel_io_handle_t panel_io = NULL;
    esp_lcd_panel_handle_t panel = NULL;

    esp_lcd_panel_io_spi_config_t io_config = {};
    io_config.cs_gpio_num = EXAMPLE_PIN_NUM_LCD_CS;
    io_config.dc_gpio_num = -1;
    io_config.spi_mode = 3;
    io_config.pclk_hz = 40 * 1000 * 1000;
    io_config.trans_queue_depth = 10;
    io_config.on_color_trans_done = example_notify_lvgl_flush_ready;
    io_config.lcd_cmd_bits = 32;
    io_config.lcd_param_bits = 8;
    io_config.flags.quad_mode = true;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(LCD_HOST, &io_config, &panel_io));

    axs15231b_vendor_config_t vendor_config = {};
    vendor_config.flags.use_qspi_interface = 1;
    vendor_config.init_cmds = lcd_init_cmds;
    vendor_config.init_cmds_size = sizeof(lcd_init_cmds) / sizeof(lcd_init_cmds[0]);

    esp_lcd_panel_dev_config_t panel_config = {};
    panel_config.reset_gpio_num = -1;
    panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
    panel_config.bits_per_pixel = LCD_BIT_PER_PIXEL;
    panel_config.vendor_config = &vendor_config;

    ESP_LOGI(TAG, "Install panel driver");
    ESP_ERROR_CHECK(esp_lcd_new_panel_axs15231b(panel_io, &panel_config, &panel));

    example_lcd_reset();
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel));

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();
    lv_display_t * disp = lv_display_create(EXAMPLE_LCD_H_RES, EXAMPLE_LCD_V_RES);
    lv_display_set_flush_cb(disp, example_lvgl_flush_cb);

    uint8_t *buffer_1 = NULL;
    buffer_1 = (uint8_t *)heap_caps_malloc(BUFF_SIZE, MALLOC_CAP_SPIRAM);
    assert(buffer_1);
    trans_buf[0] = (uint16_t *)heap_caps_malloc(LVGL_DMA_BUFF_LEN, MALLOC_CAP_DMA);
    assert(trans_buf[0]);
    trans_buf[1] = (uint16_t *)heap_caps_malloc(LVGL_DMA_BUFF_LEN, MALLOC_CAP_DMA);
    assert(trans_buf[1]);
    shadow = (uint16_t *)heap_caps_malloc(BUFF_SIZE, MALLOC_CAP_SPIRAM);
    assert(shadow);
    /*
     * DIRECT mode: LVGL renders only the invalidated areas into the
     * full-screen buffer, and the flush callback sends complete frames to the
     * panel (its QSPI path cannot do partial rows).
     */
    lv_display_set_buffers(disp, buffer_1, NULL, BUFF_SIZE, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_user_data(disp, panel);
    lv_display_set_rotation(disp, LV_DISPLAY_ROTATION_90);

    esp_timer_create_args_t lvgl_tick_timer_args = {};
    lvgl_tick_timer_args.callback = &example_increase_lvgl_tick;
    lvgl_tick_timer_args.name = "lvgl_tick";
    esp_timer_handle_t lvgl_tick_timer = NULL;
    ESP_ERROR_CHECK(esp_timer_create(&lvgl_tick_timer_args, &lvgl_tick_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(lvgl_tick_timer, LVGL_TICK_PERIOD_MS * 1000));

    lvgl_mux = xSemaphoreCreateMutex();
    assert(lvgl_mux);
    xTaskCreatePinnedToCore(example_lvgl_port_task, "LVGL", LVGL_TASK_STACK_SIZE, NULL, LVGL_TASK_PRIORITY, NULL, 0);

    if (example_lvgl_lock(-1))
    {
        demo_ui_create();
        example_lcd_backlight_set(true);
        example_lvgl_unlock();
    }
}
