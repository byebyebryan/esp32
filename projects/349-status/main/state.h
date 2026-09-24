#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "cJSON.h"

#define STATUS_MAX_ZONES        8
#define STATUS_ZONE_ID_MAX      16
#define STATUS_ZONE_KIND_MAX    12
#define STATUS_ZONE_TEXT_MAX    96
#define STATUS_ZONE_FORMAT_MAX  16
#define STATUS_MAX_NOTIFS       8
#define STATUS_NOTIF_APP_MAX    32
#define STATUS_NOTIF_SUMMARY_MAX 64
#define STATUS_NOTIF_BODY_MAX   160
#define STATUS_STALE_TIMEOUT_US (10 * 1000 * 1000)

typedef struct {
    char id[STATUS_ZONE_ID_MAX];
    char kind[STATUS_ZONE_KIND_MAX];
    int w;
    char text[STATUS_ZONE_TEXT_MAX];
    float value;
    bool has_value;
    char format[STATUS_ZONE_FORMAT_MAX];
    char align[8];
    uint32_t color;
    bool has_color;
} status_zone_t;

typedef struct {
    bool valid;
    int64_t epoch; /* UTC seconds */
    int offset;    /* seconds east of UTC */
} status_clock_t;

typedef struct {
    bool valid;
    char state[12];
    char title[64];
    char artist[64];
    char album[64];
    float pos;
    float len;
    int64_t updated_us;
} status_media_t;

typedef struct {
    bool valid;
    int id;
    char app[STATUS_NOTIF_APP_MAX];
    char summary[STATUS_NOTIF_SUMMARY_MAX];
    char body[STATUS_NOTIF_BODY_MAX];
    int urgency;
} status_notif_t;

typedef struct {
    status_zone_t zones[STATUS_MAX_ZONES];
    int zone_count;
    status_clock_t clock;
    status_media_t media;
    status_notif_t notifs[STATUS_MAX_NOTIFS];
    int notif_count;
    int notif_overflow;
    int hidden_ids[STATUS_MAX_NOTIFS];
    int hidden_count;
    int64_t last_rx_us;
    bool got_sync;
} status_state_t;

#define STATE_DIRTY_BAR   0x1
#define STATE_DIRTY_NOTIF 0x2

void state_init(void);

/* The state mutex guards every access; state_get() returns the live struct and
 * the caller must hold the lock. */
void state_lock(void);
void state_unlock(void);
status_state_t *state_get(void);

/* Dirty flags: set by any change, returned and cleared by the UI. */
uint32_t state_take_dirty(void);
void state_mark_dirty(uint32_t bits);

/* Any recognized host message counts as liveness. */
void state_note_rx(void);

void state_apply_bar(const cJSON *obj);
bool state_apply_clock(const cJSON *obj, int64_t *epoch, int *offset);
void state_apply_media(const cJSON *obj);
void state_apply_notify(const cJSON *obj);
void state_apply_close(int id);
void state_apply_sync(const cJSON *obj);

/* Locally hidden after a device dismiss; unhidden by a replace. */
void state_hide_notif(int id);
