idf_component_register(
    SRCS "display_349.c" "board.c" "touch.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "."
    REQUIRES esp_driver_spi esp_driver_i2c esp_driver_ledc esp_driver_gpio esp_lcd esp_hw_support esp_timer)
