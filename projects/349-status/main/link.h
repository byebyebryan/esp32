#pragma once

#include <stdbool.h>

#include "esp_err.h"

#define LINK_PREFIX    "@349 "
#define LINK_LINE_MAX  8192
#define LINK_RX_BUFFER 4096
#define LINK_TX_BUFFER 4096

typedef void (*link_line_cb_t)(const char *json);
typedef void (*link_overflow_cb_t)(void);

/* Install the USB-Serial-JTAG driver, route stdio through it and start the
 * reader task. Every newline-terminated line carrying LINK_PREFIX is handed to
 * on_line with the prefix stripped; other lines are ignored. */
esp_err_t link_start(link_line_cb_t on_line);

/* Called from the link task when an over-long line is dropped. */
void link_set_overflow_cb(link_overflow_cb_t on_overflow);

/* Send one JSON message as a prefixed, newline-terminated line. */
esp_err_t link_send_json(const char *json);

/* True while the host is on the other end of the USB port (SOF packets seen). */
bool link_host_connected(void);
