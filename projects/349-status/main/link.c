#include "link.h"

#include <stdio.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "link";
static const size_t PREFIX_LEN = sizeof(LINK_PREFIX) - 1;

static link_line_cb_t s_on_line;
static link_overflow_cb_t s_on_overflow;
static char s_line[LINK_LINE_MAX];
static size_t s_line_len;
static bool s_last_connected;
static char s_tx_line[LINK_LINE_MAX];
static SemaphoreHandle_t s_tx_mux;

static void dispatch_line(void)
{
    if (s_line_len > 0 && s_line[s_line_len - 1] == '\r') {
        s_line_len--;
    }
    s_line[s_line_len] = '\0';
    if (s_line_len > PREFIX_LEN && strncmp(s_line, LINK_PREFIX, PREFIX_LEN) == 0) {
        s_on_line(s_line + PREFIX_LEN);
    }
    s_line_len = 0;
}

static void link_task(void *arg)
{
    uint8_t buf[256];

    while (true) {
        int n = usb_serial_jtag_read_bytes(buf, sizeof(buf), pdMS_TO_TICKS(500));
        for (int i = 0; i < n; i++) {
            char c = (char)buf[i];
            if (c == '\n') {
                dispatch_line();
            } else if (s_line_len < LINK_LINE_MAX - 1) {
                s_line[s_line_len++] = c;
            } else {
                ESP_LOGW(TAG, "line longer than %d bytes, dropped", LINK_LINE_MAX);
                s_line_len = 0;
                if (s_on_overflow) {
                    s_on_overflow();
                }
            }
        }

        bool connected = usb_serial_jtag_is_connected();
        if (connected != s_last_connected) {
            ESP_LOGI(TAG, "host %s", connected ? "connected" : "disconnected");
            s_last_connected = connected;
        }
    }
}

esp_err_t link_start(link_line_cb_t on_line)
{
    s_on_line = on_line;

    usb_serial_jtag_driver_config_t config = {
        .rx_buffer_size = LINK_RX_BUFFER,
        .tx_buffer_size = LINK_TX_BUFFER,
    };
    ESP_RETURN_ON_ERROR(usb_serial_jtag_driver_install(&config), TAG, "driver install");

    usb_serial_jtag_vfs_use_driver();
    s_last_connected = usb_serial_jtag_is_connected();

    s_tx_mux = xSemaphoreCreateMutex();
    if (s_tx_mux == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(link_task, "link", 4096, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void link_set_overflow_cb(link_overflow_cb_t on_overflow)
{
    s_on_overflow = on_overflow;
}

esp_err_t link_send_json(const char *json)
{
    if (xSemaphoreTake(s_tx_mux, pdMS_TO_TICKS(100)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    int len = snprintf(s_tx_line, sizeof(s_tx_line), "%s%s\n", LINK_PREFIX, json);
    esp_err_t err = ESP_OK;
    if (len < 0 || len >= (int)sizeof(s_tx_line)) {
        err = ESP_ERR_INVALID_SIZE;
    } else {
        int written = usb_serial_jtag_write_bytes(s_tx_line, len, pdMS_TO_TICKS(100));
        if (written != len) {
            err = ESP_ERR_TIMEOUT;
        }
    }

    xSemaphoreGive(s_tx_mux);
    return err;
}

bool link_host_connected(void)
{
    return usb_serial_jtag_is_connected();
}
