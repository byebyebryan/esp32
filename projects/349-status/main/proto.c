#include "proto.h"

#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "link.h"
#include "rtc.h"
#include "state.h"

static const char *TAG = "proto";

static void send_object(cJSON *obj)
{
    char *json = cJSON_PrintUnformatted(obj);
    if (json != NULL) {
        link_send_json(json);
        cJSON_free(json);
    }
}

void proto_send_hello(void)
{
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "t", "hello");
    cJSON_AddNumberToObject(obj, "proto", 1);
    cJSON_AddStringToObject(obj, "fw", "0.2.0");
    cJSON_AddStringToObject(obj, "build", esp_app_get_description()->version);
    char build_sha[17];
    esp_app_get_elf_sha256(build_sha, sizeof(build_sha));
    cJSON_AddStringToObject(obj, "build_sha", build_sha);
    cJSON *cap = cJSON_AddArrayToObject(obj, "cap");
    cJSON_AddItemToArray(cap, cJSON_CreateString("link"));
    cJSON_AddItemToArray(cap, cJSON_CreateString("bar"));
    cJSON_AddItemToArray(cap, cJSON_CreateString("rtc"));
    send_object(obj);
    cJSON_Delete(obj);
}

static void send_resync(const char *reason)
{
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "t", "resync");
    cJSON_AddStringToObject(obj, "reason", reason);
    send_object(obj);
    cJSON_Delete(obj);
}

void proto_handle_overflow(void)
{
    send_resync("rx_overflow");
}

void proto_send_input_dismiss(int id)
{
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "t", "input");
    cJSON_AddStringToObject(obj, "action", "dismiss");
    cJSON_AddNumberToObject(obj, "id", id);
    send_object(obj);
    cJSON_Delete(obj);
}

static void handle_text(const cJSON *value)
{
    ESP_LOGI(TAG, "text: %s", cJSON_IsString(value) ? value->valuestring : "");

    cJSON *ack = cJSON_CreateObject();
    cJSON_AddStringToObject(ack, "t", "ack");
    if (cJSON_IsString(value)) {
        cJSON_AddStringToObject(ack, "v", value->valuestring);
    }
    send_object(ack);
    cJSON_Delete(ack);
}

static void handle_ping(const cJSON *ts)
{
    cJSON *pong = cJSON_CreateObject();
    cJSON_AddStringToObject(pong, "t", "pong");
    if (cJSON_IsNumber(ts)) {
        cJSON_AddNumberToObject(pong, "ts", ts->valuedouble);
    }
    send_object(pong);
    cJSON_Delete(pong);
}

static void handle_clock(const cJSON *obj)
{
    int64_t epoch = 0;
    int offset = 0;
    if (state_apply_clock(obj, &epoch, &offset)) {
        rtc_pcf_set(epoch, offset);
    }
}

void proto_handle_line(const char *json)
{
    cJSON *obj = cJSON_Parse(json);
    if (obj == NULL) {
        ESP_LOGW(TAG, "bad json: %.64s", json);
        send_resync("parse_error");
        return;
    }

    const cJSON *type = cJSON_GetObjectItemCaseSensitive(obj, "t");
    if (!cJSON_IsString(type)) {
        ESP_LOGW(TAG, "message without type: %.64s", json);
        cJSON_Delete(obj);
        return;
    }

    const char *kind = type->valuestring;
    if (strcmp(kind, "hello") == 0) {
        proto_send_hello();
    } else if (strcmp(kind, "text") == 0) {
        handle_text(cJSON_GetObjectItemCaseSensitive(obj, "v"));
    } else if (strcmp(kind, "ping") == 0) {
        handle_ping(cJSON_GetObjectItemCaseSensitive(obj, "ts"));
        state_note_rx();
    } else if (strcmp(kind, "sync") == 0) {
        const cJSON *clock = cJSON_GetObjectItemCaseSensitive(obj, "clock");
        if (cJSON_IsObject(clock)) {
            handle_clock(clock);
        }
        state_note_rx();
        state_apply_sync(obj);
    } else if (strcmp(kind, "bar") == 0) {
        state_apply_bar(obj);
        state_note_rx();
    } else if (strcmp(kind, "clock") == 0) {
        handle_clock(obj);
        state_note_rx();
    } else if (strcmp(kind, "media") == 0) {
        state_apply_media(obj);
        state_note_rx();
    } else if (strcmp(kind, "notify") == 0) {
        state_apply_notify(obj);
        state_note_rx();
    } else if (strcmp(kind, "close") == 0) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(obj, "id");
        if (cJSON_IsNumber(id)) {
            state_apply_close(id->valueint);
        }
        state_note_rx();
    } else {
        ESP_LOGW(TAG, "unknown type: %s", kind);
    }

    cJSON_Delete(obj);
}
