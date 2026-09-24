#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_err.h"

/* Panel geometry: native portrait vs LVGL landscape UI. */
#define BOARD_349_NATIVE_W 172
#define BOARD_349_NATIVE_H 640
#define BOARD_349_UI_W     640
#define BOARD_349_UI_H     172

/* QSPI bus to the AXS15231B panel. */
#define BOARD_349_LCD_HOST      SPI3_HOST
#define BOARD_349_PIN_LCD_CS    GPIO_NUM_9
#define BOARD_349_PIN_LCD_PCLK  GPIO_NUM_10
#define BOARD_349_PIN_LCD_DATA0 GPIO_NUM_11
#define BOARD_349_PIN_LCD_DATA1 GPIO_NUM_12
#define BOARD_349_PIN_LCD_DATA2 GPIO_NUM_13
#define BOARD_349_PIN_LCD_DATA3 GPIO_NUM_14
#define BOARD_349_PIN_LCD_TE    GPIO_NUM_21

/* I2C0: TCA9554 expander, RTC, IMU. I2C1: touch. */
#define BOARD_349_I2C0_SCL GPIO_NUM_48
#define BOARD_349_I2C0_SDA GPIO_NUM_47
#define BOARD_349_I2C1_SCL GPIO_NUM_18
#define BOARD_349_I2C1_SDA GPIO_NUM_17

#define BOARD_349_TOUCH_ADDR 0x3B

/* Backlight: BL_EN gates the rail, PWM sets brightness (active-low duty). */
#define BOARD_349_PIN_BK_LIGHT GPIO_NUM_42

/* TCA9554 bit assignments. */
#define BOARD_349_EXIO_TOUCH_INT (1ULL << 0)
#define BOARD_349_EXIO_BL_EN     (1ULL << 1)
#define BOARD_349_EXIO_IMU_INT1  (1ULL << 2)
#define BOARD_349_EXIO_IMU_INT2  (1ULL << 3)
#define BOARD_349_EXIO_RTC_INT   (1ULL << 4)
#define BOARD_349_EXIO_LCD_RST   (1ULL << 5)
#define BOARD_349_EXIO_SYS_EN    (1ULL << 6)
#define BOARD_349_EXIO_NS_MODE   (1ULL << 7)

/* Buses, expander and backlight; does not touch the panel. */
esp_err_t board_349_init(void);

/* 0 = off, 1..100 = brightness. */
esp_err_t board_349_backlight(uint8_t percent);

/* LCD reset pulse through the TCA9554. */
esp_err_t board_349_lcd_reset(void);

i2c_master_bus_handle_t board_349_i2c_bus0(void);
i2c_master_bus_handle_t board_349_i2c_bus1(void);
i2c_master_dev_handle_t board_349_touch_dev(void);

/* Raw AXS15231B touch read; coordinates are in panel-native space. */
bool touch_349_read_raw(uint16_t *x, uint16_t *y);
