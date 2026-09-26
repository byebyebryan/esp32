/*
 * Board display support for the Waveshare ESP32-S3-Touch-LCD-3.49 V2,
 * extracted from projects/349-hello (which was adapted from Waveshare's
 * 10_LVGL_V9_Test example, Apache-2.0).
 *
 * The AXS15231B QSPI path does not support hardware rotation (Waveshare's own
 * config notes "software rotation"), so the 640x172 landscape view comes from
 * LVGL's 90-degree display rotation. The flush callback fuses the transpose,
 * the RGB565 byte swap and the chunk copy into a single cache-friendly pass:
 * LVGL's generic rotate fallback is a scalar transpose with strided reads and
 * dominated the frame time (~64ms of a ~90ms frame).
 *
 * The panel only accepts complete frames, so each refresh cycle sends the
 * whole shadow framebuffer in row chunks staged through two DMA buffers.
 */

#include "display_349.h"

#include <assert.h>
#include <stdint.h>

#include "board_349.h"
#include "esp_async_memcpy.h"
#include "esp_check.h"
#include "esp_lcd_axs15231b.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lvgl.h"

static const char *TAG = "display349";

#define LCD_BIT_PER_PIXEL 16
#define BYTES_PER_PIXEL (LV_COLOR_FORMAT_GET_SIZE(LV_COLOR_FORMAT_RGB565))
#define DMA_BUFF_LEN (BOARD_349_NATIVE_W * 64 * BYTES_PER_PIXEL)
#define UI_BUFF_LEN (BOARD_349_UI_W * BOARD_349_UI_H * BYTES_PER_PIXEL)

#define LVGL_TICK_PERIOD_MS    5
#define LVGL_TASK_MAX_DELAY_MS 500
#define LVGL_TASK_MIN_DELAY_MS 5
#define LVGL_TASK_STACK_SIZE   (8 * 1024)
#define LVGL_TASK_PRIORITY     2
#define PANEL_TRANSFERS_IN_FLIGHT 2
#define DMA_WAIT_TIMEOUT_MS 2000

/* Dirty areas above this many pixels rebuild the whole shadow instead of
 * transposing the rectangle (the strided rectangle transpose loses to the
 * cache-friendly full rebuild beyond roughly this size). */
#define SHADOW_REBUILD_MAX_PIXELS 16384

static SemaphoreHandle_t s_lvgl_mux;
static SemaphoreHandle_t s_flush_done;
static SemaphoreHandle_t s_mcp_done;
static async_memcpy_handle_t s_mcp;
static uint16_t *s_trans_buf[2];
static uint16_t *s_shadow;
static lv_display_t *s_disp;

static int64_t s_prof_flush;
static int64_t s_prof_period;
static int64_t s_prof_period_min;
static int64_t s_last_flush_start;
static int s_prof_frames;

static const axs15231b_lcd_init_cmd_t LCD_INIT_CMDS[] = {
    {0x11, (uint8_t[]){0x00}, 0, 100},
    {0x29, (uint8_t[]){0x00}, 0, 100},
};

static bool flush_done_cb(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_io_event_data_t *edata, void *user_ctx)
{
    BaseType_t high_task_awoken = pdFALSE;
    xSemaphoreGiveFromISR(s_flush_done, &high_task_awoken);
    return false;
}

/*
 * The shadow lives in PSRAM, and a plain memcpy of 220KB per frame costs about
 * as much CPU as the transpose did. Use the GDMA-backed async memcpy instead:
 * the copy runs on its own DMA engine (overlapping the SPI transfer) and the
 * CPU only waits on a semaphore.
 */
static bool mcp_done_cb(async_memcpy_handle_t mcp, async_memcpy_event_t *event, void *cb_args)
{
    BaseType_t high_task_woken = pdFALSE;
    xSemaphoreGiveFromISR(s_mcp_done, &high_task_woken);
    return high_task_woken == pdTRUE;
}

static void wait_for_dma(SemaphoreHandle_t done, const char *operation)
{
    if (xSemaphoreTake(done, pdMS_TO_TICKS(DMA_WAIT_TIMEOUT_MS)) != pdTRUE) {
        ESP_LOGE(TAG, "%s completion timed out after %d ms; restarting", operation, DMA_WAIT_TIMEOUT_MS);
        esp_restart();
    }
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
    const int ny1 = (BOARD_349_UI_W - 1) - area->x2;
    const int ny2 = (BOARD_349_UI_W - 1) - area->x1;

    for (int ny = ny1; ny <= ny2; ny++) {
        const int u = (BOARD_349_UI_W - 1) - ny;
        uint16_t *dst = s_shadow + (size_t)ny * BOARD_349_NATIVE_W;
        for (int v = nx1; v <= nx2; v++) {
            const uint16_t px = src[(size_t)v * BOARD_349_UI_W + u];
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
    const int flush_count = UI_BUFF_LEN / DMA_BUFF_LEN;
    const int rows_per_chunk = BOARD_349_NATIVE_H / flush_count;

    for (int c = 0; c < flush_count; c++) {
        const int y0 = c * rows_per_chunk;
        for (int v = 0; v < BOARD_349_NATIVE_W; v++) {
            const uint16_t *src_row = src + (size_t)v * BOARD_349_UI_W;
            const uint32_t *src32 = (const uint32_t *)(src_row + (BOARD_349_NATIVE_H - 1 - y0 - (rows_per_chunk - 1)));
            uint16_t *dst_col = s_shadow + v;
            for (int j = 0; j < rows_per_chunk / 2; j++) {
                const uint32_t w = src32[j];
                const uint16_t p0 = (uint16_t)w;
                const uint16_t p1 = (uint16_t)(w >> 16);
                dst_col[(y0 + rows_per_chunk - 1 - 2 * j) * BOARD_349_NATIVE_W] = (uint16_t)((p0 >> 8) | (p0 << 8));
                dst_col[(y0 + rows_per_chunk - 2 - 2 * j) * BOARD_349_NATIVE_W] = (uint16_t)((p1 >> 8) | (p1 << 8));
            }
        }
    }
}

/*
 * Send the whole shadow to the panel in full-width row chunks. Two chunk
 * buffers let the copy of chunk c overlap the DMA transfer of chunk c-1.
 */
static void lcd_send_shadow(esp_lcd_panel_handle_t panel)
{
    const int flush_count = UI_BUFF_LEN / DMA_BUFF_LEN;
    const int rows_per_chunk = BOARD_349_NATIVE_H / flush_count;

    for (int c = 0; c < flush_count; c++) {
        if (c >= PANEL_TRANSFERS_IN_FLIGHT) {
            wait_for_dma(s_flush_done, "panel transfer");
        }

        uint16_t *chunk = s_trans_buf[c & 1];
        ESP_ERROR_CHECK(esp_async_memcpy(s_mcp, chunk, s_shadow + (size_t)c * rows_per_chunk * BOARD_349_NATIVE_W,
                                         DMA_BUFF_LEN, mcp_done_cb, NULL));
        wait_for_dma(s_mcp_done, "async memcpy");
        ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel, 0, c * rows_per_chunk, BOARD_349_NATIVE_W,
                                                  (c + 1) * rows_per_chunk, chunk));
    }
    for (int i = 0; i < PANEL_TRANSFERS_IN_FLIGHT; i++) {
        wait_for_dma(s_flush_done, "panel transfer");
    }
}

static void lvgl_flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *color_p)
{
    esp_lcd_panel_handle_t panel = (esp_lcd_panel_handle_t)lv_display_get_user_data(disp);
    const uint16_t *src = (const uint16_t *)color_p;

    const int area_px = (area->x2 - area->x1 + 1) * (area->y2 - area->y1 + 1);
    if (area_px > SHADOW_REBUILD_MAX_PIXELS) {
        shadow_rebuild(src);
    } else {
        shadow_update(area, src);
    }

    /*
     * The panel only accepts complete frames, so send the shadow once per
     * refresh cycle on the last flush: by then every rendered area has been
     * applied to it.
     */
    if (lv_display_flush_is_last(disp)) {
        const int64_t t_start = esp_timer_get_time();
        if (s_last_flush_start) {
            const int64_t period = t_start - s_last_flush_start;
            s_prof_period += period;
            if (period < s_prof_period_min) {
                s_prof_period_min = period;
            }
        }
        s_last_flush_start = t_start;

        lcd_send_shadow(panel);

        s_prof_flush += esp_timer_get_time() - t_start;
        if (++s_prof_frames >= 30) {
            ESP_LOGD(TAG, "frame us: period=%d min=%d flush=%d", (int)(s_prof_period / 30), (int)s_prof_period_min,
                     (int)(s_prof_flush / 30));
            s_prof_period = s_prof_flush = 0;
            s_prof_period_min = INT64_MAX;
            s_prof_frames = 0;
        }
    }
    lv_display_flush_ready(disp);
}

static void lvgl_tick_cb(void *arg)
{
    lv_tick_inc(LVGL_TICK_PERIOD_MS);
}

static void lvgl_port_task(void *arg)
{
    uint32_t task_delay_ms = LVGL_TASK_MAX_DELAY_MS;
    for (;;) {
        if (display_349_lock(-1)) {
            task_delay_ms = lv_timer_handler();
            display_349_unlock();
        }
        if (task_delay_ms > LVGL_TASK_MAX_DELAY_MS) {
            task_delay_ms = LVGL_TASK_MAX_DELAY_MS;
        } else if (task_delay_ms < LVGL_TASK_MIN_DELAY_MS) {
            task_delay_ms = LVGL_TASK_MIN_DELAY_MS;
        }
        vTaskDelay(pdMS_TO_TICKS(task_delay_ms));
    }
}

esp_err_t display_349_init(void)
{
    ESP_RETURN_ON_ERROR(board_349_init(), TAG, "board init");

    async_memcpy_config_t mcp_cfg = ASYNC_MEMCPY_DEFAULT_CONFIG();
    ESP_RETURN_ON_ERROR(esp_async_memcpy_install(&mcp_cfg, &s_mcp), TAG, "async memcpy");

    s_mcp_done = xSemaphoreCreateBinary();
    /* Two panel transactions can complete before the task waits. A binary
     * semaphore would collapse those callbacks and deadlock the next frame. */
    s_flush_done = xSemaphoreCreateCounting(PANEL_TRANSFERS_IN_FLIGHT, 0);
    s_lvgl_mux = xSemaphoreCreateMutex();
    if (s_mcp_done == NULL || s_flush_done == NULL || s_lvgl_mux == NULL) {
        return ESP_ERR_NO_MEM;
    }
    s_prof_period_min = INT64_MAX;

    const spi_bus_config_t buscfg = {
        .sclk_io_num = BOARD_349_PIN_LCD_PCLK,
        .data0_io_num = BOARD_349_PIN_LCD_DATA0,
        .data1_io_num = BOARD_349_PIN_LCD_DATA1,
        .data2_io_num = BOARD_349_PIN_LCD_DATA2,
        .data3_io_num = BOARD_349_PIN_LCD_DATA3,
        .max_transfer_sz = DMA_BUFF_LEN,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(BOARD_349_LCD_HOST, &buscfg, SPI_DMA_CH_AUTO), TAG, "spi bus");

    esp_lcd_panel_io_handle_t panel_io = NULL;
    esp_lcd_panel_handle_t panel = NULL;

    const esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = BOARD_349_PIN_LCD_CS,
        .dc_gpio_num = -1,
        .spi_mode = 3,
        .pclk_hz = 40 * 1000 * 1000,
        .trans_queue_depth = 10,
        .on_color_trans_done = flush_done_cb,
        .lcd_cmd_bits = 32,
        .lcd_param_bits = 8,
        .flags.quad_mode = true,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(BOARD_349_LCD_HOST, &io_config, &panel_io), TAG, "panel io");

    const axs15231b_vendor_config_t vendor_config = {
        .init_cmds = LCD_INIT_CMDS,
        .init_cmds_size = sizeof(LCD_INIT_CMDS) / sizeof(LCD_INIT_CMDS[0]),
        .flags.use_qspi_interface = 1,
    };
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = -1,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = LCD_BIT_PER_PIXEL,
        .vendor_config = (void *)&vendor_config,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_axs15231b(panel_io, &panel_config, &panel), TAG, "panel");

    ESP_RETURN_ON_ERROR(board_349_lcd_reset(), TAG, "lcd reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(panel), TAG, "panel init");

    lv_init();
    s_disp = lv_display_create(BOARD_349_NATIVE_W, BOARD_349_NATIVE_H);
    if (s_disp == NULL) {
        return ESP_ERR_NO_MEM;
    }
    lv_display_set_flush_cb(s_disp, lvgl_flush_cb);
    lv_display_set_user_data(s_disp, panel);

    uint8_t *buffer = heap_caps_malloc(UI_BUFF_LEN, MALLOC_CAP_SPIRAM);
    s_trans_buf[0] = heap_caps_malloc(DMA_BUFF_LEN, MALLOC_CAP_DMA);
    s_trans_buf[1] = heap_caps_malloc(DMA_BUFF_LEN, MALLOC_CAP_DMA);
    s_shadow = heap_caps_malloc(UI_BUFF_LEN, MALLOC_CAP_SPIRAM);
    assert(buffer != NULL && s_trans_buf[0] != NULL && s_trans_buf[1] != NULL && s_shadow != NULL);

    /*
     * DIRECT mode: LVGL renders only the invalidated areas into the
     * full-screen buffer, and the flush callback sends complete frames to the
     * panel (its QSPI path cannot do partial rows).
     */
    lv_display_set_buffers(s_disp, buffer, NULL, UI_BUFF_LEN, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_rotation(s_disp, LV_DISPLAY_ROTATION_90);

    const esp_timer_create_args_t tick_args = {
        .callback = lvgl_tick_cb,
        .name = "lvgl_tick",
    };
    esp_timer_handle_t tick_timer = NULL;
    ESP_RETURN_ON_ERROR(esp_timer_create(&tick_args, &tick_timer), TAG, "tick timer");
    ESP_RETURN_ON_ERROR(esp_timer_start_periodic(tick_timer, LVGL_TICK_PERIOD_MS * 1000), TAG, "tick start");

    if (xTaskCreatePinnedToCore(lvgl_port_task, "LVGL", LVGL_TASK_STACK_SIZE, NULL, LVGL_TASK_PRIORITY, NULL, 0) !=
        pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

lv_display_t *display_349_lvgl(void)
{
    return s_disp;
}

static bool s_touch_was_pressed;
static int s_touch_release_ticks;
static uint16_t s_touch_last_x;
static uint16_t s_touch_last_y;

/* The controller can drop a sample or two mid-press; treat short gaps as still
 * pressed so LVGL sees a stable press/release pair. */
#define TOUCH_RELEASE_DEBOUNCE 3

/*
 * The controller reports in landscape space (640x172); LVGL applies the
 * display rotation itself, so the indev must return native (172x640)
 * coordinates: x = raw_y, y = 639 - raw_x.
 */
static void touch_read_cb(lv_indev_t *indev, lv_indev_data_t *data)
{
    uint16_t x = 0;
    uint16_t y = 0;
    bool pressed = touch_349_read_raw(&x, &y);

    if (pressed) {
        s_touch_release_ticks = 0;
        s_touch_last_x = x;
        s_touch_last_y = y;
    } else if (s_touch_was_pressed && ++s_touch_release_ticks < TOUCH_RELEASE_DEBOUNCE) {
        pressed = true;
        x = s_touch_last_x;
        y = s_touch_last_y;
    }

    if (pressed) {
        if (x > BOARD_349_UI_W - 1) {
            x = BOARD_349_UI_W - 1;
        }
        if (y > BOARD_349_UI_H - 1) {
            y = BOARD_349_UI_H - 1;
        }
        data->point.x = y;
        data->point.y = (BOARD_349_UI_W - 1) - x;
        data->state = LV_INDEV_STATE_PRESSED;
        if (!s_touch_was_pressed) {
            ESP_LOGD(TAG, "touch raw=(%u,%u) native=(%u,%u)", (unsigned)x, (unsigned)y, (unsigned)data->point.x,
                     (unsigned)data->point.y);
        }
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
    s_touch_was_pressed = pressed;
}

esp_err_t display_349_touch_init(void)
{
    if (s_disp == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    /* The LVGL task is already running after display_349_init(). */
    if (!display_349_lock(-1)) {
        return ESP_ERR_TIMEOUT;
    }
    lv_indev_t *indev = lv_indev_create();
    if (indev == NULL) {
        display_349_unlock();
        return ESP_ERR_NO_MEM;
    }
    lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev, touch_read_cb);
    lv_indev_set_display(indev, s_disp);
    display_349_unlock();
    return ESP_OK;
}

bool display_349_lock(int timeout_ms)
{
    assert(s_lvgl_mux && "display_349_init must be called first");
    const TickType_t timeout_ticks = (timeout_ms == -1) ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    return xSemaphoreTake(s_lvgl_mux, timeout_ticks) == pdTRUE;
}

void display_349_unlock(void)
{
    assert(s_lvgl_mux && "display_349_init must be called first");
    xSemaphoreGive(s_lvgl_mux);
}

esp_err_t display_349_backlight(uint8_t percent)
{
    return board_349_backlight(percent);
}

i2c_master_bus_handle_t display_349_i2c_bus0(void)
{
    return board_349_i2c_bus0();
}
