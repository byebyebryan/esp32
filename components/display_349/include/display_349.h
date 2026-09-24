#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"
#include "lvgl.h"

/* LVGL UI dimensions (landscape). */
#define DISPLAY_349_H_RES 640
#define DISPLAY_349_V_RES 172

/* Bring up I2C, the TCA9554, the QSPI panel, LVGL and the LVGL task.
 * The backlight is left off; call display_349_backlight() when the UI is up. */
esp_err_t display_349_init(void);

/* LVGL display created by display_349_init(), or NULL. */
lv_display_t *display_349_lvgl(void);

/* Register the LVGL pointer indev backed by the AXS15231B touch controller. */
esp_err_t display_349_touch_init(void);

/* LVGL lock, held while mutating widgets from outside the LVGL task. */
bool display_349_lock(int timeout_ms);
void display_349_unlock(void);

/* 0 = off, 1..100 = brightness. */
esp_err_t display_349_backlight(uint8_t percent);

/* I2C0 handle (TCA9554, RTC, IMU) for board peripherals outside this component. */
i2c_master_bus_handle_t display_349_i2c_bus0(void);
