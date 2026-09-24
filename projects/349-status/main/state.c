#include "state.h"

#include <stdlib.h>
#include <string.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static status_state_t s_state;
static SemaphoreHandle_t s_mutex;
static uint32_t s_dirty;

static void copy_str(char *dst, size_t size, const cJSON *item)
{
    const char *src = cJSON_IsString(item) ? item->valuestring : "";
    if (strlcpy(dst, src, size) >= size) {
        /* Keep a truncated UTF-8 string valid for LVGL. */
        size_t end = size - 1;
        size_t lead = end;
        while (lead > 0 && ((unsigned char)dst[lead - 1] & 0xC0) == 0x80) {
            lead--;
        }
        if (lead > 0) {
            const unsigned char first = (unsigned char)dst[lead - 1];
            const size_t expected = first < 0x80 ? 1 : first < 0xE0 ? 2 : first < 0xF0 ? 3 : 4;
            if (end - (lead - 1) < expected) {
                dst[lead - 1] = '\0';
            }
        }
    }
}

static uint32_t parse_color(const cJSON *item)
{
    if (!cJSON_IsString(item) || item->valuestring[0] != '#') {
        return 0;
    }
    unsigned long value = strtoul(item->valuestring + 1, NULL, 16);
    return (uint32_t)value & 0xFFFFFF;
}

void state_init(void)
{
    memset(&s_state, 0, sizeof(s_state));
    s_mutex = xSemaphoreCreateMutex();
    configASSERT(s_mutex);
}

void state_lock(void)
{
    xSemaphoreTake(s_mutex, portMAX_DELAY);
}

void state_unlock(void)
{
    xSemaphoreGive(s_mutex);
}

status_state_t *state_get(void)
{
    return &s_state;
}

uint32_t state_take_dirty(void)
{
    state_lock();
    const uint32_t dirty = s_dirty;
    s_dirty = 0;
    state_unlock();
    return dirty;
}

void state_mark_dirty(uint32_t bits)
{
    state_lock();
    s_dirty |= bits;
    state_unlock();
}

void state_note_rx(void)
{
    state_lock();
    s_state.last_rx_us = esp_timer_get_time();
    state_unlock();
}

void state_apply_bar(const cJSON *obj)
{
    const cJSON *zones = cJSON_GetObjectItemCaseSensitive(obj, "zones");
    int count = 0;

    state_lock();
    if (cJSON_IsArray(zones)) {
        const cJSON *item = NULL;
        cJSON_ArrayForEach(item, zones) {
            if (count >= STATUS_MAX_ZONES) {
                break;
            }
            status_zone_t *zone = &s_state.zones[count];
            memset(zone, 0, sizeof(*zone));

            copy_str(zone->id, sizeof(zone->id), cJSON_GetObjectItemCaseSensitive(item, "id"));
            copy_str(zone->kind, sizeof(zone->kind), cJSON_GetObjectItemCaseSensitive(item, "kind"));
            copy_str(zone->text, sizeof(zone->text), cJSON_GetObjectItemCaseSensitive(item, "text"));
            copy_str(zone->format, sizeof(zone->format), cJSON_GetObjectItemCaseSensitive(item, "format"));
            copy_str(zone->align, sizeof(zone->align), cJSON_GetObjectItemCaseSensitive(item, "align"));

            const cJSON *w = cJSON_GetObjectItemCaseSensitive(item, "w");
            zone->w = cJSON_IsNumber(w) ? w->valueint : 0;

            const cJSON *value = cJSON_GetObjectItemCaseSensitive(item, "value");
            if (cJSON_IsNumber(value)) {
                zone->value = (float)value->valuedouble;
                zone->has_value = true;
            }

            const cJSON *color = cJSON_GetObjectItemCaseSensitive(item, "color");
            if (cJSON_IsString(color)) {
                zone->color = parse_color(color);
                zone->has_color = true;
            }
            count++;
        }
    }
    s_state.zone_count = count;
    s_dirty |= STATE_DIRTY_BAR;
    state_unlock();
}

bool state_apply_clock(const cJSON *obj, int64_t *epoch, int *offset)
{
    const cJSON *e = cJSON_GetObjectItemCaseSensitive(obj, "epoch");
    const cJSON *o = cJSON_GetObjectItemCaseSensitive(obj, "offset");
    if (!cJSON_IsNumber(e)) {
        return false;
    }

    state_lock();
    s_state.clock.valid = true;
    s_state.clock.epoch = (int64_t)e->valuedouble;
    s_state.clock.offset = cJSON_IsNumber(o) ? o->valueint : 0;
    state_unlock();

    if (epoch) {
        *epoch = (int64_t)e->valuedouble;
    }
    if (offset) {
        *offset = cJSON_IsNumber(o) ? o->valueint : 0;
    }
    return true;
}

void state_apply_media(const cJSON *obj)
{
    state_lock();
    status_media_t *media = &s_state.media;
    memset(media, 0, sizeof(*media));
    if (cJSON_IsObject(obj)) {
        media->valid = true;
        copy_str(media->state, sizeof(media->state), cJSON_GetObjectItemCaseSensitive(obj, "state"));
        copy_str(media->title, sizeof(media->title), cJSON_GetObjectItemCaseSensitive(obj, "title"));
        copy_str(media->artist, sizeof(media->artist), cJSON_GetObjectItemCaseSensitive(obj, "artist"));
        copy_str(media->album, sizeof(media->album), cJSON_GetObjectItemCaseSensitive(obj, "album"));
        const cJSON *pos = cJSON_GetObjectItemCaseSensitive(obj, "pos");
        const cJSON *len = cJSON_GetObjectItemCaseSensitive(obj, "len");
        media->pos = cJSON_IsNumber(pos) ? (float)pos->valuedouble : 0;
        media->len = cJSON_IsNumber(len) ? (float)len->valuedouble : 0;
        media->updated_us = esp_timer_get_time();
    }
    s_dirty |= STATE_DIRTY_BAR;
    state_unlock();
}

static void apply_notify(const cJSON *obj, bool unhide)
{
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(obj, "id");
    if (!cJSON_IsNumber(id)) {
        return;
    }
    int nid = id->valueint;

    state_lock();
    /* A live notify can replace a locally hidden card. A periodic sync must
     * preserve the local dismiss for unchanged cards. */
    if (unhide) {
        for (int i = 0; i < s_state.hidden_count; i++) {
            if (s_state.hidden_ids[i] == nid) {
                memmove(&s_state.hidden_ids[i], &s_state.hidden_ids[i + 1],
                        sizeof(s_state.hidden_ids[0]) * (s_state.hidden_count - i - 1));
                s_state.hidden_count--;
                break;
            }
        }
    }
    int slot = -1;
    for (int i = 0; i < s_state.notif_count; i++) {
        if (s_state.notifs[i].id == nid) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        if (s_state.notif_count < STATUS_MAX_NOTIFS) {
            slot = s_state.notif_count++;
        } else {
            /* Drop the oldest to make room for the newest, but keep counting it
             * so the "+N more" indicator stays truthful between syncs. */
            memmove(&s_state.notifs[0], &s_state.notifs[1], sizeof(s_state.notifs[0]) * (STATUS_MAX_NOTIFS - 1));
            slot = STATUS_MAX_NOTIFS - 1;
            if (s_state.notif_overflow < 999) {
                s_state.notif_overflow++;
            }
        }
    }

    status_notif_t *notif = &s_state.notifs[slot];
    memset(notif, 0, sizeof(*notif));
    notif->valid = true;
    notif->id = nid;
    copy_str(notif->app, sizeof(notif->app), cJSON_GetObjectItemCaseSensitive(obj, "app"));
    copy_str(notif->summary, sizeof(notif->summary), cJSON_GetObjectItemCaseSensitive(obj, "summary"));
    copy_str(notif->body, sizeof(notif->body), cJSON_GetObjectItemCaseSensitive(obj, "body"));
    const cJSON *urgency = cJSON_GetObjectItemCaseSensitive(obj, "urgency");
    notif->urgency = cJSON_IsNumber(urgency) ? urgency->valueint : 1;

    s_dirty |= STATE_DIRTY_NOTIF;
    state_unlock();
}

void state_apply_notify(const cJSON *obj)
{
    apply_notify(obj, true);
}

void state_hide_notif(int id)
{
    state_lock();
    bool known = false;
    for (int i = 0; i < s_state.hidden_count; i++) {
        if (s_state.hidden_ids[i] == id) {
            known = true;
            break;
        }
    }
    if (!known && s_state.hidden_count < STATUS_MAX_NOTIFS) {
        s_state.hidden_ids[s_state.hidden_count++] = id;
    }
    s_dirty |= STATE_DIRTY_NOTIF;
    state_unlock();
}

void state_apply_close(int id)
{
    state_lock();
    for (int i = 0; i < s_state.hidden_count; i++) {
        if (s_state.hidden_ids[i] == id) {
            memmove(&s_state.hidden_ids[i], &s_state.hidden_ids[i + 1],
                    sizeof(s_state.hidden_ids[0]) * (s_state.hidden_count - i - 1));
            s_state.hidden_count--;
            break;
        }
    }
    for (int i = 0; i < s_state.notif_count; i++) {
        if (s_state.notifs[i].id == id) {
            memmove(&s_state.notifs[i], &s_state.notifs[i + 1], sizeof(s_state.notifs[0]) * (s_state.notif_count - i - 1));
            s_state.notif_count--;
            s_dirty |= STATE_DIRTY_NOTIF;
            state_unlock();
            return;
        }
    }
    /* Closing something we had already dropped shrinks the overflow. */
    if (s_state.notif_overflow > 0) {
        s_state.notif_overflow--;
        s_dirty |= STATE_DIRTY_NOTIF;
    }
    state_unlock();
}

void state_apply_sync(const cJSON *obj)
{
    const cJSON *bar = cJSON_GetObjectItemCaseSensitive(obj, "bar");
    state_apply_bar(bar);

    const cJSON *media = cJSON_GetObjectItemCaseSensitive(obj, "media");
    state_apply_media(cJSON_IsObject(media) ? media : NULL);

    const cJSON *notifs = cJSON_GetObjectItemCaseSensitive(obj, "notifs");
    state_lock();
    s_state.notif_count = 0;
    state_unlock();
    if (cJSON_IsArray(notifs)) {
        const cJSON *item = NULL;
        cJSON_ArrayForEach(item, notifs) {
            apply_notify(item, false);
        }
    }

    const cJSON *overflow = cJSON_GetObjectItemCaseSensitive(obj, "notifs_overflow");
    state_lock();
    s_state.notif_overflow = cJSON_IsNumber(overflow) ? overflow->valueint : 0;
    s_state.got_sync = true;
    s_dirty |= STATE_DIRTY_BAR | STATE_DIRTY_NOTIF;
    state_unlock();
}
