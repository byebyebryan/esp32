#include "board_349.h"

#include "driver/i2c_master.h"
#include "freertos/FreeRTOS.h"

/* AXS15231B touch: write the vendor read command, then 32 bytes of state.
 * buf[1] is the touch count; X/Y are 12-bit values split across two bytes.
 * Coordinates are in the controller's landscape space (640x172). */
static const uint8_t TOUCH_READ_CMD[11] = {0xb5, 0xab, 0xa5, 0x5a, 0x00, 0x00, 0x00, 0x0e, 0x00, 0x00, 0x00};

bool touch_349_read_raw(uint16_t *x, uint16_t *y)
{
    uint8_t buf[32] = {0};
    i2c_master_dev_handle_t dev = board_349_touch_dev();

    if (i2c_master_transmit_receive(dev, TOUCH_READ_CMD, sizeof(TOUCH_READ_CMD), buf, sizeof(buf), pdMS_TO_TICKS(100)) !=
        ESP_OK) {
        return false;
    }
    if (buf[1] == 0 || buf[1] >= 5) {
        return false;
    }

    *x = (((uint16_t)buf[2] & 0x0f) << 8) | (uint16_t)buf[3];
    *y = (((uint16_t)buf[4] & 0x0f) << 8) | (uint16_t)buf[5];
    return true;
}
