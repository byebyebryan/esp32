#ifndef USER_CONFIG_H
#define USER_CONFIG_H

//spi handle
#define LCD_HOST SPI3_HOST

// ESP32 I2C
#define ESP_SCL_NUM (GPIO_NUM_48)
#define ESP_SDA_NUM (GPIO_NUM_47)

// Touch I2C
#define Touch_SCL_NUM (GPIO_NUM_18)
#define Touch_SDA_NUM (GPIO_NUM_17)


//  DISP
#define EXAMPLE_LCD_H_RES              172   // panel native, portrait
#define EXAMPLE_LCD_V_RES              640
#define DISP_H_RES                     640   // LVGL UI, landscape (software rotated)
#define DISP_V_RES                     172
#define LVGL_DMA_BUFF_LEN    (EXAMPLE_LCD_H_RES * 64 * 2)
#define LVGL_SPIRAM_BUFF_LEN (DISP_H_RES * DISP_V_RES * 2)

/*bl test*/
#define Backlight_Testing 0

#define EXAMPLE_PIN_NUM_LCD_CS            (GPIO_NUM_9)
#define EXAMPLE_PIN_NUM_LCD_PCLK          (GPIO_NUM_10) 
#define EXAMPLE_PIN_NUM_LCD_DATA0         (GPIO_NUM_11)
#define EXAMPLE_PIN_NUM_LCD_DATA1         (GPIO_NUM_12)
#define EXAMPLE_PIN_NUM_LCD_DATA2         (GPIO_NUM_13)
#define EXAMPLE_PIN_NUM_LCD_DATA3         (GPIO_NUM_14)
#define EXAMPLE_PIN_NUM_LCD_TE            (GPIO_NUM_21)
#define EXAMPLE_PIN_NUM_LCD_RST           (-1)
#define EXAMPLE_PIN_NUM_BK_LIGHT          (GPIO_NUM_42)

#define EXAMPLE_PIN_NUM_EXIO_INT          (GPIO_NUM_8)

// TCA9554 I/O expander pins
#define EXAMPLE_EXIO_PIN_TOUCH_INT        (1ULL << 0)
#define EXAMPLE_EXIO_PIN_BL_EN            (1ULL << 1)
#define EXAMPLE_EXIO_PIN_IMU_INT1         (1ULL << 2)
#define EXAMPLE_EXIO_PIN_IMU_INT2         (1ULL << 3)
#define EXAMPLE_EXIO_PIN_RTC_INT          (1ULL << 4)
#define EXAMPLE_EXIO_PIN_LCD_RST          (1ULL << 5)
#define EXAMPLE_EXIO_PIN_SYS_EN           (1ULL << 6)
#define EXAMPLE_EXIO_PIN_NS_MODE          (1ULL << 7)

#define DISP_TOUCH_ADDR                   0x3B
#define EXAMPLE_PIN_NUM_TOUCH_RST         (-1)
#define EXAMPLE_PIN_NUM_TOUCH_INT         (-1)


#endif
