#pragma once

/* Handle one JSON message from the host (prefix already stripped). */
void proto_handle_line(const char *json);

/* Announce the device; also sent in reply to a host hello request. */
void proto_send_hello(void);

/* Ask the host for a full sync (link RX overflow). */
void proto_handle_overflow(void);

/* Device-initiated actions (touch). */
void proto_send_input_dismiss(int id);
