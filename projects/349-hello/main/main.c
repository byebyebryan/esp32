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
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/spi_master.h"
#include "esp_timer.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_err.h"
#include "esp_log.h"
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
static esp_io_expander_handle_t io_expander = NULL;
static lv_obj_t *touch_dot = NULL;

static int64_t prof_xpose = 0, prof_flush = 0, prof_period = 0;
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

static void example_lvgl_flush_cb(lv_display_t * disp, const lv_area_t * area, uint8_t * color_p)
{
    esp_lcd_panel_handle_t panel_handle = (esp_lcd_panel_handle_t)lv_display_get_user_data(disp);

    /*
     * In DIRECT mode a single refresh cycle can produce several invalidated
     * areas (for example the old and the new position of the touch dot). The
     * panel can only accept complete frames, so only the last flush of the
     * cycle sends anything: by then all rendered areas are already in the
     * buffer.
     */
    if (!lv_display_flush_is_last(disp))
    {
        lv_disp_flush_ready(disp);
        return;
    }

    /*
     * LVGL renders the UI in landscape (640x172) because of the display
     * rotation. The panel needs native portrait frames (172x640) written as
     * full-width row chunks: its QSPI path sends only CASET and relies on
     * RAMWR/RAMWRC continuation, so partial rows are not possible.
     *
     * Mapping from lv_display_rotate_point() for ROTATION_90:
     *     native_x = ui_y
     *     native_y = 639 - ui_x
     *
     * The source is walked row-major (contiguous reads) and written transposed
     * into an internal DMA chunk buffer, with the byte swap applied in the
     * same pass.
     *
     * Two chunk buffers are used so the transpose of chunk c overlaps the DMA
     * transfer of chunk c-1; a chunk buffer is only reused after the transfer
     * of the chunk two positions earlier has completed.
     */
    const int flush_coun = (LVGL_SPIRAM_BUFF_LEN / LVGL_DMA_BUFF_LEN);
    const int rows_per_chunk = (EXAMPLE_LCD_V_RES / flush_coun);
    const uint16_t *src = (const uint16_t *)color_p;

    int64_t t_start = esp_timer_get_time();
    if (last_flush_start)
    {
        prof_period += t_start - last_flush_start;
    }
    last_flush_start = t_start;
    for (int c = 0; c < flush_coun; c++)
    {
        if (c >= 2)
        {
            xSemaphoreTake(flush_done_semaphore, portMAX_DELAY);
        }

        const int y0 = c * rows_per_chunk;
        uint16_t *chunk = trans_buf[c & 1];
        int64_t ta = esp_timer_get_time();
        /*
         * 32-bit loads: one word holds two horizontally adjacent UI pixels,
         * which land in two consecutive chunk rows. For chunk c the source
         * range is [639-y0-63, 639-y0] and starts on an even pixel, so every
         * load is naturally 4-byte aligned.
         */
        for (int v = 0; v < EXAMPLE_LCD_H_RES; v++)
        {
            const uint16_t *src_row = src + (size_t)v * DISP_H_RES;
            const uint32_t *src32 = (const uint32_t *)(src_row + (EXAMPLE_LCD_V_RES - 1 - y0 - (rows_per_chunk - 1)));
            uint16_t *dst_col = chunk + v;
            for (int j = 0; j < rows_per_chunk / 2; j++)
            {
                uint32_t w = src32[j];
                uint16_t p0 = (uint16_t)w;
                uint16_t p1 = (uint16_t)(w >> 16);
                dst_col[(rows_per_chunk - 1 - 2 * j) * EXAMPLE_LCD_H_RES] = (uint16_t)((p0 >> 8) | (p0 << 8));
                dst_col[(rows_per_chunk - 2 - 2 * j) * EXAMPLE_LCD_H_RES] = (uint16_t)((p1 >> 8) | (p1 << 8));
            }
        }
        prof_xpose += esp_timer_get_time() - ta;

        esp_lcd_panel_draw_bitmap(panel_handle, 0, y0, EXAMPLE_LCD_H_RES, y0 + rows_per_chunk, chunk);
    }
    xSemaphoreTake(flush_done_semaphore, portMAX_DELAY);
    xSemaphoreTake(flush_done_semaphore, portMAX_DELAY);
    prof_flush += esp_timer_get_time() - t_start;

    if (++prof_frames >= 30)
    {
        ESP_LOGI(TAG, "frame us: period=%d flush=%d xpose=%d",
                 (int)(prof_period / 30), (int)(prof_flush / 30), (int)(prof_xpose / 30));
        prof_period = prof_flush = prof_xpose = 0;
        prof_frames = 0;
    }
    lv_disp_flush_ready(disp);
}

static void TouchInputReadCallback(lv_indev_t * indev, lv_indev_data_t *indevData)
{
    uint8_t read_touchpad_cmd[11] = {0xb5, 0xab, 0xa5, 0x5a, 0x0, 0x0, 0x0, 0x0e,0x0, 0x0, 0x0};
    uint8_t buff[32] = {0};
    ESP_ERROR_CHECK_WITHOUT_ABORT(i2c_master_write_read_dev(disp_touch_dev_handle,read_touchpad_cmd,11,buff,32));
    uint16_t pointX;
    uint16_t pointY;
    pointX = (((uint16_t)buff[2] & 0x0f) << 8) | (uint16_t)buff[3];
    pointY = (((uint16_t)buff[4] & 0x0f) << 8) | (uint16_t)buff[5];
    if (buff[1]>0 && buff[1]<5)
    {
        /*
         * The touch controller reports in 640x172 (landscape) space.
         * Map it to the display's native 172x640 space; LVGL then applies
         * the display rotation itself (lv_display_rotate_point), so the
         * callback must NOT pre-rotate.
         */
        if(pointX > EXAMPLE_LCD_V_RES) pointX = EXAMPLE_LCD_V_RES;
        if(pointY > EXAMPLE_LCD_H_RES) pointY = EXAMPLE_LCD_H_RES;
        indevData->point.x = pointY;
        indevData->point.y = (EXAMPLE_LCD_V_RES - pointX);
        indevData->state = LV_INDEV_STATE_PRESSED;

        if (touch_dot)
        {
            lv_point_t p = { .x = indevData->point.x, .y = indevData->point.y };
            lv_display_rotate_point(lv_indev_get_display(indev), &p);
            lv_obj_clear_flag(touch_dot, LV_OBJ_FLAG_HIDDEN);
            lv_obj_set_pos(touch_dot, p.x - 12, p.y - 12);
        }
    }
    else
    {
        indevData->state = LV_INDEV_STATE_RELEASED;
        if (touch_dot)
        {
            lv_obj_add_flag(touch_dot, LV_OBJ_FLAG_HIDDEN);
        }
    }
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

static void hello_ui_create(void)
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

    touch_dot = lv_obj_create(scr);
    lv_obj_remove_style_all(touch_dot);
    lv_obj_set_size(touch_dot, 24, 24);
    lv_obj_set_style_radius(touch_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(touch_dot, lv_color_hex(0xff5566), 0);
    lv_obj_set_style_bg_opa(touch_dot, LV_OPA_70, 0);
    lv_obj_add_flag(touch_dot, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(touch_dot);
}

void app_main(void)
{
    lcd_bl_pwm_bsp_init(LCD_PWM_MODE_255);
    flush_done_semaphore = xSemaphoreCreateBinary();
    assert(flush_done_semaphore);
    touch_i2c_master_Init();
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
    io_config.pclk_hz = 80 * 1000 * 1000;
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
    /*
     * DIRECT mode: LVGL renders only the invalidated areas into the
     * full-screen buffer, and the flush callback sends complete frames to the
     * panel (its QSPI path cannot do partial rows).
     */
    lv_display_set_buffers(disp, buffer_1, NULL, BUFF_SIZE, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_user_data(disp, panel);
    lv_display_set_rotation(disp, LV_DISPLAY_ROTATION_90);

    lv_indev_t *touch_indev = NULL;
    touch_indev = lv_indev_create();
    lv_indev_set_type(touch_indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(touch_indev, TouchInputReadCallback);

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
        hello_ui_create();
        example_lcd_backlight_set(true);
        example_lvgl_unlock();
    }
}
