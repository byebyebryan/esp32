#include "board_349.h"

#include "driver/ledc.h"
#include "esp_check.h"
#include "esp_io_expander_tca9554.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "board349";

static i2c_master_bus_handle_t s_i2c0;
static i2c_master_bus_handle_t s_i2c1;
static i2c_master_dev_handle_t s_touch;
static esp_io_expander_handle_t s_expander;

static esp_err_t i2c_bus_init(i2c_port_t port, gpio_num_t scl, gpio_num_t sda, i2c_master_bus_handle_t *out)
{
    const i2c_master_bus_config_t cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = port,
        .scl_io_num = scl,
        .sda_io_num = sda,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    return i2c_new_master_bus(&cfg, out);
}

static esp_err_t backlight_pwm_init(void)
{
    const ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_8_BIT,
        .timer_num = LEDC_TIMER_3,
        .freq_hz = 50 * 1000,
        .clk_cfg = LEDC_SLOW_CLK_RC_FAST,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "ledc timer");

    const ledc_channel_config_t channel = {
        .gpio_num = BOARD_349_PIN_BK_LIGHT,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_1,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LEDC_TIMER_3,
        .duty = 0,
        .hpoint = 0,
    };
    return ledc_channel_config(&channel);
}

esp_err_t board_349_init(void)
{
    ESP_RETURN_ON_ERROR(i2c_bus_init(I2C_NUM_0, BOARD_349_I2C0_SCL, BOARD_349_I2C0_SDA, &s_i2c0), TAG, "i2c0");
    ESP_RETURN_ON_ERROR(i2c_bus_init(I2C_NUM_1, BOARD_349_I2C1_SCL, BOARD_349_I2C1_SDA, &s_i2c1), TAG, "i2c1");

    ESP_RETURN_ON_ERROR(
        esp_io_expander_new_i2c_tca9554(s_i2c0, ESP_IO_EXPANDER_I2C_TCA9554_ADDRESS_000, &s_expander), TAG, "tca9554");
    ESP_RETURN_ON_ERROR(esp_io_expander_set_dir(s_expander, BOARD_349_EXIO_TOUCH_INT, IO_EXPANDER_INPUT), TAG, "exio touch int");
    ESP_RETURN_ON_ERROR(
        esp_io_expander_set_dir(s_expander, BOARD_349_EXIO_BL_EN | BOARD_349_EXIO_LCD_RST, IO_EXPANDER_OUTPUT), TAG, "exio outputs");
    ESP_RETURN_ON_ERROR(esp_io_expander_set_level(s_expander, BOARD_349_EXIO_BL_EN, 0), TAG, "backlight off");
    ESP_RETURN_ON_ERROR(esp_io_expander_set_level(s_expander, BOARD_349_EXIO_LCD_RST, 1), TAG, "lcd reset high");

    const i2c_device_config_t touch_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .scl_speed_hz = 300000,
        .device_address = BOARD_349_TOUCH_ADDR,
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(s_i2c1, &touch_cfg, &s_touch), TAG, "touch device");

    return backlight_pwm_init();
}

esp_err_t board_349_backlight(uint8_t percent)
{
    if (percent == 0) {
        return esp_io_expander_set_level(s_expander, BOARD_349_EXIO_BL_EN, 0);
    }
    if (percent > 100) {
        percent = 100;
    }

    /* The PWM input is active-low: duty 0 is full brightness. */
    const uint32_t duty = 255 - (uint32_t)percent * 255 / 100;
    ESP_RETURN_ON_ERROR(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, duty), TAG, "ledc duty");
    ESP_RETURN_ON_ERROR(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1), TAG, "ledc update");
    return esp_io_expander_set_level(s_expander, BOARD_349_EXIO_BL_EN, 1);
}

esp_err_t board_349_lcd_reset(void)
{
    ESP_RETURN_ON_ERROR(esp_io_expander_set_level(s_expander, BOARD_349_EXIO_LCD_RST, 1), TAG, "rst high");
    vTaskDelay(pdMS_TO_TICKS(30));
    ESP_RETURN_ON_ERROR(esp_io_expander_set_level(s_expander, BOARD_349_EXIO_LCD_RST, 0), TAG, "rst low");
    vTaskDelay(pdMS_TO_TICKS(250));
    ESP_RETURN_ON_ERROR(esp_io_expander_set_level(s_expander, BOARD_349_EXIO_LCD_RST, 1), TAG, "rst high");
    vTaskDelay(pdMS_TO_TICKS(30));
    return ESP_OK;
}

i2c_master_bus_handle_t board_349_i2c_bus0(void)
{
    return s_i2c0;
}

i2c_master_bus_handle_t board_349_i2c_bus1(void)
{
    return s_i2c1;
}

i2c_master_dev_handle_t board_349_touch_dev(void)
{
    return s_touch;
}
