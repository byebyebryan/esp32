#include "state.h"

#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static status_state_t s_state;
static status_notif_t s_legacy_notifs[STATUS_LEGACY_NOTIFS];
static status_zone_t s_zone_scratch[STATUS_MAX_ZONES];
static struct {
    status_notif_t *cards;
    status_zone_t zones[STATUS_MAX_ZONES];
    int zone_count;
    status_clock_t clock;
    status_media_t media;
    int tx;
    int count;
    int limit;
    int next_index;
    int overflow;
    int64_t started_us;
    bool active;
} s_stage;
static SemaphoreHandle_t s_mutex;
static uint32_t s_dirty;
static const char *TAG = "state";

#define SYNC_TIMEOUT_US (5 * 1000 * 1000)

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

static int parse_zones(const cJSON *zones, status_zone_t *out, bool strict)
{
    if (!cJSON_IsArray(zones)) {
        return -1;
    }
    const int supplied = cJSON_GetArraySize(zones);
    if (strict && supplied > STATUS_MAX_ZONES) {
        return -1;
    }
    const int size = supplied < STATUS_MAX_ZONES ? supplied : STATUS_MAX_ZONES;
    for (int count = 0; count < size; count++) {
        const cJSON *item = cJSON_GetArrayItem(zones, count);
        if (!cJSON_IsObject(item)) {
            return -1;
        }
        status_zone_t *zone = &out[count];
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
    }
    return size;
}

static status_clock_t parse_clock(const cJSON *obj)
{
    status_clock_t clock = {0};
    const cJSON *e = cJSON_GetObjectItemCaseSensitive(obj, "epoch");
    const cJSON *o = cJSON_GetObjectItemCaseSensitive(obj, "offset");
    if (cJSON_IsNumber(e)) {
        clock.valid = true;
        clock.epoch = (int64_t)e->valuedouble;
        clock.offset = cJSON_IsNumber(o) ? o->valueint : 0;
    }
    return clock;
}

static status_media_t parse_media(const cJSON *obj)
{
    status_media_t media = {0};
    if (cJSON_IsObject(obj)) {
        media.valid = true;
        copy_str(media.state, sizeof(media.state), cJSON_GetObjectItemCaseSensitive(obj, "state"));
        copy_str(media.title, sizeof(media.title), cJSON_GetObjectItemCaseSensitive(obj, "title"));
        copy_str(media.artist, sizeof(media.artist), cJSON_GetObjectItemCaseSensitive(obj, "artist"));
        copy_str(media.album, sizeof(media.album), cJSON_GetObjectItemCaseSensitive(obj, "album"));
        const cJSON *pos = cJSON_GetObjectItemCaseSensitive(obj, "pos");
        const cJSON *len = cJSON_GetObjectItemCaseSensitive(obj, "len");
        media.pos = cJSON_IsNumber(pos) ? (float)pos->valuedouble : 0;
        media.len = cJSON_IsNumber(len) ? (float)len->valuedouble : 0;
        media.updated_us = esp_timer_get_time();
    }
    return media;
}

static bool parse_notif(const cJSON *obj, status_notif_t *notif)
{
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(obj, "id");
    if (!cJSON_IsObject(obj) || !cJSON_IsNumber(id) || id->valuedouble != (double)id->valueint) {
        return false;
    }
    memset(notif, 0, sizeof(*notif));
    notif->valid = true;
    notif->id = id->valueint;
    copy_str(notif->app, sizeof(notif->app), cJSON_GetObjectItemCaseSensitive(obj, "app"));
    copy_str(notif->summary, sizeof(notif->summary), cJSON_GetObjectItemCaseSensitive(obj, "summary"));
    copy_str(notif->body, sizeof(notif->body), cJSON_GetObjectItemCaseSensitive(obj, "body"));
    const cJSON *urgency = cJSON_GetObjectItemCaseSensitive(obj, "urgency");
    notif->urgency = cJSON_IsNumber(urgency) ? urgency->valueint : 1;
    return true;
}

void state_init(void)
{
    memset(&s_state, 0, sizeof(s_state));
    memset(&s_stage, 0, sizeof(s_stage));
    s_state.notifs = heap_caps_calloc(STATUS_MAX_NOTIFS, sizeof(status_notif_t),
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_stage.cards = heap_caps_calloc(STATUS_MAX_NOTIFS, sizeof(status_notif_t),
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_state.notifs == NULL || s_stage.cards == NULL) {
        heap_caps_free(s_state.notifs);
        heap_caps_free(s_stage.cards);
        s_state.notifs = s_legacy_notifs;
        s_stage.cards = NULL;
        s_state.notif_capacity = STATUS_LEGACY_NOTIFS;
        ESP_LOGW(TAG, "PSRAM card cache unavailable; using %d legacy slots", STATUS_LEGACY_NOTIFS);
    } else {
        s_state.notif_capacity = STATUS_MAX_NOTIFS;
        ESP_LOGI(TAG, "card cache ready: %d committed + %d staging slots in PSRAM",
                 STATUS_MAX_NOTIFS, STATUS_MAX_NOTIFS);
    }
    s_state.cache_limit = STATUS_LEGACY_NOTIFS;
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
    int count = parse_zones(zones, s_zone_scratch, false);
    if (count < 0) {
        count = 0;
    }

    state_lock();
    memcpy(s_state.zones, s_zone_scratch, sizeof(s_zone_scratch[0]) * (size_t)count);
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
    status_media_t media = parse_media(obj);
    state_lock();
    s_state.media = media;
    s_dirty |= STATE_DIRTY_BAR;
    state_unlock();
}

static void apply_notify(const cJSON *obj, bool unhide)
{
    status_notif_t parsed;
    if (!parse_notif(obj, &parsed)) {
        return;
    }
    const int nid = parsed.id;

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
    const cJSON *cached = cJSON_GetObjectItemCaseSensitive(obj, "cached");
    const cJSON *total = cJSON_GetObjectItemCaseSensitive(obj, "total");
    if (unhide && cJSON_IsFalse(cached)) {
        /* A replacement of an older active ID can be outside the host's
         * newest-N selection. It still unhides the ID, but cannot displace a
         * selected card from this bounded cache. */
        for (int i = 0; i < s_state.notif_count; i++) {
            if (s_state.notifs[i].id == nid) {
                memmove(&s_state.notifs[i], &s_state.notifs[i + 1],
                        sizeof(s_state.notifs[0]) * (size_t)(s_state.notif_count - i - 1));
                s_state.notif_count--;
                break;
            }
        }
        if (cJSON_IsNumber(total) && total->valueint >= s_state.notif_count) {
            s_state.notif_overflow = total->valueint - s_state.notif_count;
        }
        s_dirty |= STATE_DIRTY_NOTIF;
        state_unlock();
        return;
    }
    if (s_state.cache_limit == 0) {
        if (cJSON_IsNumber(total) && total->valueint >= 0) {
            s_state.notif_overflow = total->valueint;
        }
        s_dirty |= STATE_DIRTY_NOTIF;
        state_unlock();
        return;
    }
    int slot = -1;
    for (int i = 0; i < s_state.notif_count; i++) {
        if (s_state.notifs[i].id == nid) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        if (s_state.notif_count < s_state.cache_limit) {
            slot = s_state.notif_count++;
        } else {
            /* Drop the oldest to make room for the newest, but keep counting it
             * so the "+N more" indicator stays truthful between syncs. */
            memmove(&s_state.notifs[0], &s_state.notifs[1],
                    sizeof(s_state.notifs[0]) * (size_t)(s_state.cache_limit - 1));
            slot = s_state.cache_limit - 1;
            if (s_state.notif_overflow < 999) {
                s_state.notif_overflow++;
            }
        }
    }

    s_state.notifs[slot] = parsed;

    if (cJSON_IsNumber(total) && total->valueint >= s_state.notif_count) {
        s_state.notif_overflow = total->valueint - s_state.notif_count;
    }

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
    if (!known) {
        if (s_state.hidden_count == STATUS_MAX_NOTIFS) {
            /* Preserve the newest local dismiss when active cards exceed our
             * bounded hidden-ID capacity. */
            memmove(&s_state.hidden_ids[0], &s_state.hidden_ids[1],
                    sizeof(s_state.hidden_ids[0]) * (STATUS_MAX_NOTIFS - 1));
            s_state.hidden_count--;
        }
        s_state.hidden_ids[s_state.hidden_count++] = id;
    }
    s_dirty |= STATE_DIRTY_NOTIF;
    state_unlock();
}

void state_apply_close(int id, int total)
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
    bool found = false;
    for (int i = 0; i < s_state.notif_count; i++) {
        if (s_state.notifs[i].id == id) {
            memmove(&s_state.notifs[i], &s_state.notifs[i + 1], sizeof(s_state.notifs[0]) * (s_state.notif_count - i - 1));
            s_state.notif_count--;
            found = true;
            break;
        }
    }
    if (total >= s_state.notif_count) {
        s_state.notif_overflow = total - s_state.notif_count;
    } else if (!found && s_state.notif_overflow > 0) {
        /* Legacy close of a card that was already dropped. */
        s_state.notif_overflow--;
    }
    s_dirty |= STATE_DIRTY_NOTIF;
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
    s_state.cache_limit = STATUS_LEGACY_NOTIFS;
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
    /* Only a complete notification snapshot can prove a hidden ID is gone.
     * A capped snapshot may omit a still-active, locally dismissed card. */
    if (cJSON_IsArray(notifs) && cJSON_IsNumber(overflow)
            && overflow->valueint == 0 && cJSON_GetArraySize(notifs) <= s_state.cache_limit) {
        int retained = 0;
        for (int h = 0; h < s_state.hidden_count; h++) {
            for (int i = 0; i < s_state.notif_count; i++) {
                if (s_state.hidden_ids[h] == s_state.notifs[i].id) {
                    s_state.hidden_ids[retained++] = s_state.hidden_ids[h];
                    break;
                }
            }
        }
        s_state.hidden_count = retained;
    }
    s_state.got_sync = true;
    s_dirty |= STATE_DIRTY_BAR | STATE_DIRTY_NOTIF;
    state_unlock();
}

static bool int_field(const cJSON *obj, const char *name, int min, int max, int *out)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!cJSON_IsNumber(item) || item->valuedouble != (double)item->valueint
            || item->valueint < min || item->valueint > max) {
        return false;
    }
    *out = item->valueint;
    return true;
}

int state_card_sync_capacity(void)
{
    return s_stage.cards != NULL ? STATUS_MAX_NOTIFS : 0;
}

void state_sync_abort(void)
{
    s_stage.active = false;
}

bool state_sync_pending(void)
{
    return s_stage.active;
}

bool state_sync_timeout(void)
{
    if (s_stage.active && esp_timer_get_time() - s_stage.started_us > SYNC_TIMEOUT_US) {
        state_sync_abort();
        return true;
    }
    return false;
}

bool state_sync_begin(const cJSON *obj)
{
    int tx, count, limit, overflow;
    const cJSON *bar = cJSON_GetObjectItemCaseSensitive(obj, "bar");
    if (s_stage.cards == NULL || s_stage.active
            || !int_field(obj, "tx", 0, INT32_MAX, &tx)
            || !int_field(obj, "count", 0, STATUS_MAX_NOTIFS, &count)
            || !int_field(obj, "limit", 0, STATUS_MAX_NOTIFS, &limit)
            || !int_field(obj, "overflow", 0, INT32_MAX, &overflow)
            || count > limit || limit > s_state.notif_capacity || !cJSON_IsObject(bar)) {
        return false;
    }
    const int zone_count = parse_zones(cJSON_GetObjectItemCaseSensitive(bar, "zones"), s_stage.zones, true);
    if (zone_count < 0) {
        return false;
    }
    s_stage.zone_count = zone_count;
    s_stage.clock = parse_clock(cJSON_GetObjectItemCaseSensitive(obj, "clock"));
    s_stage.media = parse_media(cJSON_GetObjectItemCaseSensitive(obj, "media"));
    s_stage.tx = tx;
    s_stage.count = count;
    s_stage.limit = limit;
    s_stage.next_index = 0;
    s_stage.overflow = overflow;
    s_stage.started_us = esp_timer_get_time();
    s_stage.active = true;
    return true;
}

bool state_sync_cards(const cJSON *obj)
{
    int tx, start;
    const cJSON *notifs = cJSON_GetObjectItemCaseSensitive(obj, "notifs");
    if (!s_stage.active || !int_field(obj, "tx", 0, INT32_MAX, &tx)
            || !int_field(obj, "start", 0, STATUS_MAX_NOTIFS, &start)
            || tx != s_stage.tx || start != s_stage.next_index || !cJSON_IsArray(notifs)) {
        return false;
    }
    const int size = cJSON_GetArraySize(notifs);
    if (size <= 0 || size > s_stage.count - s_stage.next_index) {
        return false;
    }
    for (int index = 0; index < size; index++) {
        const cJSON *item = cJSON_GetArrayItem(notifs, index);
        status_notif_t parsed;
        if (!parse_notif(item, &parsed)) {
            return false;
        }
        for (int previous = 0; previous < s_stage.next_index; previous++) {
            if (s_stage.cards[previous].id == parsed.id) {
                return false;
            }
        }
        s_stage.cards[s_stage.next_index++] = parsed;
    }
    s_stage.started_us = esp_timer_get_time();
    return true;
}

bool state_sync_commit(const cJSON *obj, int64_t *epoch, int *offset, bool *has_clock)
{
    int tx;
    if (!s_stage.active || !int_field(obj, "tx", 0, INT32_MAX, &tx)
            || tx != s_stage.tx || s_stage.next_index != s_stage.count
            || state_sync_timeout()) {
        return false;
    }

    state_lock();
    status_notif_t *old_cards = s_state.notifs;
    s_state.notifs = s_stage.cards;
    s_stage.cards = old_cards;
    memcpy(s_state.zones, s_stage.zones, sizeof(s_state.zones[0]) * (size_t)s_stage.zone_count);
    s_state.zone_count = s_stage.zone_count;
    s_state.clock = s_stage.clock;
    s_state.media = s_stage.media;
    s_state.notif_count = s_stage.count;
    s_state.cache_limit = s_stage.limit;
    s_state.notif_overflow = s_stage.overflow;
    if (s_stage.overflow == 0) {
        int retained = 0;
        for (int h = 0; h < s_state.hidden_count; h++) {
            for (int i = 0; i < s_state.notif_count; i++) {
                if (s_state.hidden_ids[h] == s_state.notifs[i].id) {
                    s_state.hidden_ids[retained++] = s_state.hidden_ids[h];
                    break;
                }
            }
        }
        s_state.hidden_count = retained;
    }
    s_state.got_sync = true;
    s_dirty |= STATE_DIRTY_BAR | STATE_DIRTY_NOTIF;
    state_unlock();

    if (epoch != NULL) {
        *epoch = s_stage.clock.epoch;
    }
    if (offset != NULL) {
        *offset = s_stage.clock.offset;
    }
    if (has_clock != NULL) {
        *has_clock = s_stage.clock.valid;
    }
    s_stage.active = false;
    ESP_LOGI(TAG, "card sync committed: tx=%d cached=%d overflow=%d",
             tx, s_stage.count, s_stage.overflow);
    return true;
}

void state_cards_status(int *ids, int *count, int *overflow, int *capacity)
{
    state_lock();
    *count = s_state.notif_count;
    *overflow = s_state.notif_overflow;
    *capacity = s_state.notif_capacity;
    for (int i = 0; i < s_state.notif_count; i++) {
        ids[i] = s_state.notifs[i].id;
    }
    state_unlock();
}
