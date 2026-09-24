idf_component_register(
    SRCS "main.c" "link.c" "proto.c" "state.c" "ui.c" "rtc.c"
    REQUIRES lvgl display_349 json esp_driver_usb_serial_jtag esp_driver_i2c
    INCLUDE_DIRS ".")
