#include "proto.h"

#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_random.h"
#include "link.h"
#include "rtc.h"
#include "state.h"

static const char *TAG = "proto";
static uint32_t s_boot_id;
/* After a failed transaction, discard its queued tail until a new begin.
 * One bad chunk should request one recovery transfer, not a resync storm. */
static bool s_drop_sync_tail;

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
    if (s_boot_id == 0) {
        s_boot_id = esp_random();
        if (s_boot_id == 0) {
            s_boot_id = 1;
        }
    }
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "t", "hello");
    cJSON_AddNumberToObject(obj, "proto", 1);
    cJSON_AddStringToObject(obj, "fw", "0.2.0");
    cJSON_AddStringToObject(obj, "build", esp_app_get_description()->version);
    char build_sha[17];
    esp_app_get_elf_sha256(build_sha, sizeof(build_sha));
    cJSON_AddStringToObject(obj, "build_sha", build_sha);
    cJSON_AddNumberToObject(obj, "boot_id", s_boot_id);
    cJSON *cap = cJSON_AddArrayToObject(obj, "cap");
    cJSON_AddItemToArray(cap, cJSON_CreateString("link"));
    cJSON_AddItemToArray(cap, cJSON_CreateString("bar"));
    cJSON_AddItemToArray(cap, cJSON_CreateString("rtc"));
    if (state_card_sync_capacity() > 0) {
        cJSON_AddItemToArray(cap, cJSON_CreateString("card-sync-v1"));
        cJSON_AddNumberToObject(obj, "cache_cards", state_card_sync_capacity());
    }
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
    state_sync_abort();
    if (!s_drop_sync_tail) {
        s_drop_sync_tail = true;
        send_resync("rx_overflow");
    }
}

static void send_cards_status(void)
{
    int ids[STATUS_MAX_NOTIFS];
    int count, overflow, capacity;
    state_cards_status(ids, &count, &overflow, &capacity);
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "t", "cards_status");
    cJSON_AddNumberToObject(obj, "count", count);
    cJSON_AddNumberToObject(obj, "overflow", overflow);
    cJSON_AddNumberToObject(obj, "capacity", capacity);
    cJSON *array = cJSON_AddArrayToObject(obj, "ids");
    for (int i = 0; i < count; i++) {
        cJSON_AddItemToArray(array, cJSON_CreateNumber(ids[i]));
    }
    send_object(obj);
    cJSON_Delete(obj);
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

static bool reject_interleaved(void)
{
    if (!state_sync_pending()) {
        return false;
    }
    state_sync_abort();
    if (!s_drop_sync_tail) {
        s_drop_sync_tail = true;
        send_resync("sync_interleaved");
    }
    return true;
}

void proto_handle_line(const char *json)
{
    if (state_sync_timeout()) {
        if (!s_drop_sync_tail) {
            s_drop_sync_tail = true;
            send_resync("sync_timeout");
        }
    }
    cJSON *obj = cJSON_Parse(json);
    if (obj == NULL) {
        ESP_LOGW(TAG, "bad json: %.64s", json);
        state_sync_abort();
        if (!s_drop_sync_tail) {
            s_drop_sync_tail = true;
            send_resync("parse_error");
        }
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
        state_sync_abort();
        s_drop_sync_tail = false;
        proto_send_hello();
    } else if (strcmp(kind, "text") == 0) {
        handle_text(cJSON_GetObjectItemCaseSensitive(obj, "v"));
    } else if (strcmp(kind, "ping") == 0) {
        handle_ping(cJSON_GetObjectItemCaseSensitive(obj, "ts"));
        state_note_rx();
    } else if (strcmp(kind, "sync") == 0) {
        state_sync_abort();
        s_drop_sync_tail = false;
        const cJSON *clock = cJSON_GetObjectItemCaseSensitive(obj, "clock");
        if (cJSON_IsObject(clock)) {
            handle_clock(clock);
        }
        state_note_rx();
        state_apply_sync(obj);
    } else if (strcmp(kind, "sync_begin") == 0) {
        s_drop_sync_tail = false;
        if (!state_sync_begin(obj)) {
            state_sync_abort();
            s_drop_sync_tail = true;
            send_resync("sync_begin_invalid");
        }
        state_note_rx();
    } else if (strcmp(kind, "sync_cards") == 0) {
        if (!s_drop_sync_tail && !state_sync_cards(obj)) {
            state_sync_abort();
            s_drop_sync_tail = true;
            send_resync("sync_cards_invalid");
        }
        state_note_rx();
    } else if (strcmp(kind, "sync_commit") == 0) {
        int64_t epoch = 0;
        int offset = 0;
        bool has_clock = false;
        if (s_drop_sync_tail) {
            /* A previous bad chunk already requested recovery. */
        } else if (state_sync_commit(obj, &epoch, &offset, &has_clock)) {
            if (has_clock) {
                rtc_pcf_set(epoch, offset);
            }
        } else {
            state_sync_abort();
            s_drop_sync_tail = true;
            send_resync("sync_commit_invalid");
        }
        state_note_rx();
    } else if (strcmp(kind, "bar") == 0) {
        if (!reject_interleaved()) {
            state_apply_bar(obj);
        }
        state_note_rx();
    } else if (strcmp(kind, "clock") == 0) {
        if (!reject_interleaved()) {
            handle_clock(obj);
        }
        state_note_rx();
    } else if (strcmp(kind, "media") == 0) {
        if (!reject_interleaved()) {
            state_apply_media(obj);
        }
        state_note_rx();
    } else if (strcmp(kind, "notify") == 0) {
        if (!reject_interleaved()) {
            state_apply_notify(obj);
        }
        state_note_rx();
    } else if (strcmp(kind, "close") == 0) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(obj, "id");
        const cJSON *total = cJSON_GetObjectItemCaseSensitive(obj, "total");
        if (!reject_interleaved() && cJSON_IsNumber(id)) {
            state_apply_close(id->valueint, cJSON_IsNumber(total) ? total->valueint : -1);
        }
        state_note_rx();
    } else if (strcmp(kind, "cards_query") == 0) {
        send_cards_status();
        state_note_rx();
    } else {
        ESP_LOGW(TAG, "unknown type: %s", kind);
    }

    cJSON_Delete(obj);
}
