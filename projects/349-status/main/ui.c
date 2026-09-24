#include "ui.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "display_349.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "link.h"
#include "lvgl.h"
#include "proto.h"
#include "rtc.h"
#include "state.h"

#define BAR_HEIGHT 46
#define ZONE_FONT  (&lv_font_montserrat_16)
#define BODY_FONT  (&lv_font_montserrat_14)
#define SMALL_FONT (&lv_font_montserrat_12)
#define OVERLAY_FONT (&lv_font_montserrat_28)
#define TEXT_COLOR 0xE6E6E6
#define MUTED_COLOR 0x9FB3C8
#define ACCENT_COLOR 0x33FF99
#define TRACK_COLOR 0x2B3648
#define CARD_BG 0x141A24
#define MAX_CARDS 2

static lv_obj_t *s_bar;
static lv_obj_t *s_notif_area;
static lv_obj_t *s_overlay;
static lv_obj_t *s_overlay_label;
static int s_overlay_state = -1; /* -1 unset, 0 hidden, 1 waiting, 2 asleep, 3 link stale */

static lv_obj_t *s_clock_label;
static char s_clock_format[STATUS_ZONE_FORMAT_MAX];

static lv_obj_t *s_media_label;
static lv_obj_t *s_media_bar;

static int s_last_second = -1;

static lv_text_align_t text_align(const char *align)
{
    if (strcmp(align, "center") == 0) {
        return LV_TEXT_ALIGN_CENTER;
    }
    if (strcmp(align, "right") == 0) {
        return LV_TEXT_ALIGN_RIGHT;
    }
    return LV_TEXT_ALIGN_LEFT;
}

static lv_obj_t *make_label(lv_obj_t *parent, const char *text, const status_zone_t *zone)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, ZONE_FONT, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(zone->has_color ? zone->color : TEXT_COLOR), 0);
    lv_label_set_long_mode(label, LV_LABEL_LONG_DOT);
    if (zone->w > 0) {
        lv_obj_set_width(label, zone->w);
    }
    lv_obj_set_style_text_align(label, text_align(zone->align), 0);
    return label;
}

static lv_obj_t *make_bar(lv_obj_t *parent, int width, int height)
{
    lv_obj_t *bar = lv_bar_create(parent);
    lv_obj_set_size(bar, width > 0 ? width : LV_PCT(100), height);
    lv_bar_set_range(bar, 0, 100);
    lv_bar_set_value(bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(bar, lv_color_hex(TRACK_COLOR), LV_PART_MAIN);
    lv_obj_set_style_bg_color(bar, lv_color_hex(ACCENT_COLOR), LV_PART_INDICATOR);
    lv_obj_set_style_radius(bar, 2, LV_PART_MAIN);
    lv_obj_set_style_radius(bar, 2, LV_PART_INDICATOR);
    return bar;
}

static void make_progress_zone(lv_obj_t *parent, const status_zone_t *zone)
{
    lv_obj_t *cont = lv_obj_create(parent);
    lv_obj_remove_style_all(cont);
    lv_obj_set_size(cont, zone->w > 0 ? zone->w : 60, 20);
    lv_obj_set_flex_flow(cont, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(cont, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(cont, 2, 0);

    if (zone->text[0]) {
        lv_obj_t *label = lv_label_create(cont);
        lv_label_set_text(label, zone->text);
        lv_obj_set_style_text_font(label, ZONE_FONT, 0);
        lv_obj_set_style_text_color(label, lv_color_hex(zone->has_color ? zone->color : MUTED_COLOR), 0);
        lv_label_set_long_mode(label, LV_LABEL_LONG_DOT);
        lv_obj_set_width(label, LV_PCT(100));
    }

    lv_obj_t *bar = make_bar(cont, 0, 6);
    lv_bar_set_value(bar, zone->has_value ? (int)(zone->value * 100.0f) : 0, LV_ANIM_OFF);
    if (zone->has_color) {
        lv_obj_set_style_bg_color(bar, lv_color_hex(zone->color), LV_PART_INDICATOR);
    }
}

static void make_media_zone(lv_obj_t *parent, const status_zone_t *zone)
{
    lv_obj_t *cont = lv_obj_create(parent);
    lv_obj_remove_style_all(cont);
    lv_obj_set_size(cont, zone->w > 0 ? zone->w : 200, 34);
    lv_obj_set_flex_flow(cont, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(cont, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(cont, 2, 0);

    s_media_label = lv_label_create(cont);
    lv_label_set_text(s_media_label, "--");
    lv_obj_set_style_text_font(s_media_label, ZONE_FONT, 0);
    lv_obj_set_style_text_color(s_media_label, lv_color_hex(MUTED_COLOR), 0);
    lv_label_set_long_mode(s_media_label, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s_media_label, LV_PCT(100));

    s_media_bar = make_bar(cont, 0, 6);
}

static void ui_update_clock(void)
{
    if (s_clock_label == NULL) {
        return;
    }

    struct tm tm;
    if (rtc_pcf_get_local(&tm) == RTC_SOURCE_NONE) {
        lv_label_set_text(s_clock_label, "--:--");
        return;
    }

    char text[32];
    strftime(text, sizeof(text), s_clock_format[0] ? s_clock_format : "%H:%M", &tm);
    lv_label_set_text(s_clock_label, text);
}

static void ui_update_media(void)
{
    if (s_media_label == NULL || s_media_bar == NULL) {
        return;
    }

    state_lock();
    status_media_t media = state_get()->media;
    state_unlock();

    if (!media.valid || media.title[0] == '\0') {
        lv_label_set_text(s_media_label, "--");
        lv_bar_set_value(s_media_bar, 0, LV_ANIM_OFF);
        return;
    }

    char text[160];
    if (media.artist[0]) {
        snprintf(text, sizeof(text), "%s - %s", media.title, media.artist);
    } else {
        strlcpy(text, media.title, sizeof(text));
    }
    lv_label_set_text(s_media_label, text);

    float pos = media.pos;
    if (strcmp(media.state, "playing") == 0 && media.updated_us > 0) {
        pos += (float)((esp_timer_get_time() - media.updated_us) / 1000000.0);
    }
    int percent = (media.len > 0) ? (int)(pos / media.len * 100.0f) : 0;
    if (percent < 0) {
        percent = 0;
    } else if (percent > 100) {
        percent = 100;
    }
    lv_bar_set_value(s_media_bar, percent, LV_ANIM_OFF);
}

static void ui_build_bar(void)
{
    status_zone_t zones[STATUS_MAX_ZONES];
    int count;

    state_lock();
    count = state_get()->zone_count;
    memcpy(zones, state_get()->zones, sizeof(zones[0]) * (size_t)count);
    state_unlock();

    lv_obj_clean(s_bar);
    s_clock_label = NULL;
    s_media_label = NULL;
    s_media_bar = NULL;
    s_clock_format[0] = '\0';

    /* Drop trailing zones that would overflow the row. */
    const int available = DISPLAY_349_H_RES - 16; /* 8 px padding at each edge */
    int total = 0;
    int fit = 0;
    for (; fit < count; fit++) {
        const bool spacer = strcmp(zones[fit].kind, "spacer") == 0;
        int w = zones[fit].w > 0 ? zones[fit].w : (spacer ? 0 : 60);
        if (w > available) {
            w = available;
        }
        const int gap = fit > 0 ? 8 : 0;
        if (total + gap + w > available) {
            break;
        }
        zones[fit].w = w;
        total += gap + w;
    }

    for (int i = 0; i < fit; i++) {
        status_zone_t *zone = &zones[i];
        if (strcmp(zone->kind, "spacer") == 0) {
            lv_obj_t *spacer = lv_obj_create(s_bar);
            lv_obj_remove_style_all(spacer);
            lv_obj_set_height(spacer, 1);
            lv_obj_set_flex_grow(spacer, 1);
        } else if (strcmp(zone->kind, "progress") == 0) {
            make_progress_zone(s_bar, zone);
        } else if (strcmp(zone->kind, "clock") == 0) {
            s_clock_label = make_label(s_bar, "--:--", zone);
            strlcpy(s_clock_format, zone->format[0] ? zone->format : "%H:%M", sizeof(s_clock_format));
        } else if (strcmp(zone->kind, "media") == 0) {
            make_media_zone(s_bar, zone);
        } else {
            make_label(s_bar, zone->text[0] ? zone->text : "--", zone);
        }
    }

    ui_update_clock();
    ui_update_media();
}

static uint32_t urgency_color(int urgency)
{
    if (urgency >= 2) {
        return 0xFF5566;
    }
    if (urgency == 0) {
        return 0x4A5568;
    }
    return ACCENT_COLOR;
}

static void card_click_cb(lv_event_t *event)
{
    const int id = (int)(intptr_t)lv_event_get_user_data(event);
    ESP_LOGD("ui349", "card %d clicked", id);
    state_hide_notif(id);
    proto_send_input_dismiss(id);
}

static void make_card(lv_obj_t *parent, const status_notif_t *notif)
{
    lv_obj_t *card = lv_obj_create(parent);
    lv_obj_remove_style_all(card);
    lv_obj_set_size(card, LV_PCT(100), 44);
    lv_obj_set_style_bg_color(card, lv_color_hex(CARD_BG), 0);
    lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(card, 4, 0);
    lv_obj_set_style_pad_all(card, 4, 0);
    lv_obj_set_style_pad_column(card, 6, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(card, card_click_cb, LV_EVENT_CLICKED, (void *)(intptr_t)notif->id);

    lv_obj_t *accent = lv_obj_create(card);
    lv_obj_remove_style_all(accent);
    lv_obj_remove_flag(accent, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(accent, 3, LV_PCT(100));
    lv_obj_set_style_bg_color(accent, lv_color_hex(urgency_color(notif->urgency)), 0);
    lv_obj_set_style_bg_opa(accent, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(accent, 2, 0);

    lv_obj_t *col = lv_obj_create(card);
    lv_obj_remove_style_all(col);
    lv_obj_remove_flag(col, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_flex_grow(col, 1);
    lv_obj_set_height(col, LV_PCT(100));
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(col, 0, 0);

    lv_obj_t *top = lv_obj_create(col);
    lv_obj_remove_style_all(top);
    lv_obj_remove_flag(top, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_width(top, LV_PCT(100));
    lv_obj_set_height(top, 18);
    lv_obj_set_flex_flow(top, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(top, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(top, 6, 0);

    lv_obj_t *app = lv_label_create(top);
    lv_label_set_text(app, notif->app[0] ? notif->app : "?");
    lv_obj_set_style_text_font(app, SMALL_FONT, 0);
    lv_obj_set_style_text_color(app, lv_color_hex(MUTED_COLOR), 0);
    lv_label_set_long_mode(app, LV_LABEL_LONG_DOT);
    lv_obj_set_width(app, 80);

    lv_obj_t *summary = lv_label_create(top);
    lv_label_set_text(summary, notif->summary[0] ? notif->summary : "(no summary)");
    lv_obj_set_style_text_font(summary, ZONE_FONT, 0);
    lv_obj_set_style_text_color(summary, lv_color_hex(TEXT_COLOR), 0);
    lv_label_set_long_mode(summary, LV_LABEL_LONG_DOT);
    lv_obj_set_flex_grow(summary, 1);

    lv_obj_t *body = lv_label_create(col);
    lv_label_set_text(body, notif->body);
    lv_obj_set_style_text_font(body, BODY_FONT, 0);
    lv_obj_set_style_text_color(body, lv_color_hex(MUTED_COLOR), 0);
    lv_label_set_long_mode(body, LV_LABEL_LONG_DOT);
    lv_obj_set_width(body, LV_PCT(100));
    lv_obj_set_height(body, 16);
}

static void ui_build_cards(void)
{
    status_notif_t visible[STATUS_MAX_NOTIFS];
    int visible_count = 0;
    int overflow = 0;

    state_lock();
    const status_state_t *st = state_get();
    for (int i = 0; i < st->notif_count; i++) {
        bool hidden = false;
        for (int h = 0; h < st->hidden_count; h++) {
            if (st->hidden_ids[h] == st->notifs[i].id) {
                hidden = true;
                break;
            }
        }
        if (!hidden) {
            visible[visible_count++] = st->notifs[i];
        }
    }
    overflow = st->notif_overflow;
    state_unlock();

    lv_obj_clean(s_notif_area);

    int shown = 0;
    for (int i = visible_count - 1; i >= 0 && shown < MAX_CARDS; i--, shown++) {
        make_card(s_notif_area, &visible[i]);
    }

    const int more = (visible_count - shown) + overflow;
    if (more > 0) {
        lv_obj_t *label = lv_label_create(s_notif_area);
        lv_label_set_text_fmt(label, "+%d more", more);
        lv_obj_set_style_text_font(label, SMALL_FONT, 0);
        lv_obj_set_style_text_color(label, lv_color_hex(MUTED_COLOR), 0);
    }
}

static void ui_update_overlay(void)
{
    state_lock();
    const bool got_sync = state_get()->got_sync;
    const int64_t last_rx = state_get()->last_rx_us;
    state_unlock();

    int wanted = 0;
    const char *text = NULL;
    if (!got_sync) {
        wanted = 1;
        text = "waiting for host";
    } else if (!link_host_connected()) {
        wanted = 2;
        text = "host asleep";
    } else if (last_rx == 0 || esp_timer_get_time() - last_rx > STATUS_STALE_TIMEOUT_US) {
        wanted = 3;
        text = "host disconnected";
    }

    if (wanted == s_overlay_state) {
        return;
    }
    s_overlay_state = wanted;
    if (wanted == 0) {
        lv_obj_add_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_label_set_text(s_overlay_label, text);
        lv_obj_remove_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    }
}

static void ui_tick_cb(lv_timer_t *timer)
{
    const uint32_t dirty = state_take_dirty();
    if (dirty & STATE_DIRTY_BAR) {
        ui_build_bar();
    }
    if (dirty & STATE_DIRTY_NOTIF) {
        ui_build_cards();
    }

    struct tm tm;
    if (rtc_pcf_get_local(&tm) != RTC_SOURCE_NONE && tm.tm_sec != s_last_second) {
        s_last_second = tm.tm_sec;
        ui_update_clock();
        ui_update_media();
    }

    ui_update_overlay();
}

void ui_init(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0A0E14), LV_PART_MAIN);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    s_bar = lv_obj_create(scr);
    lv_obj_remove_style_all(s_bar);
    lv_obj_set_size(s_bar, LV_PCT(100), BAR_HEIGHT);
    lv_obj_align(s_bar, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_flex_flow(s_bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(s_bar, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(s_bar, 8, 0);
    lv_obj_set_style_pad_hor(s_bar, 8, 0);

    s_notif_area = lv_obj_create(scr);
    lv_obj_remove_style_all(s_notif_area);
    lv_obj_set_size(s_notif_area, LV_PCT(100), DISPLAY_349_V_RES - BAR_HEIGHT);
    lv_obj_align(s_notif_area, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_flex_flow(s_notif_area, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(s_notif_area, 4, 0);
    lv_obj_set_style_pad_row(s_notif_area, 4, 0);
    lv_obj_remove_flag(s_notif_area, LV_OBJ_FLAG_SCROLLABLE);

    s_overlay = lv_obj_create(scr);
    lv_obj_remove_style_all(s_overlay);
    lv_obj_set_size(s_overlay, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(s_overlay, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(s_overlay, LV_OPA_80, 0);
    lv_obj_remove_flag(s_overlay, LV_OBJ_FLAG_SCROLLABLE);

    s_overlay_label = lv_label_create(s_overlay);
    lv_obj_set_style_text_font(s_overlay_label, OVERLAY_FONT, 0);
    lv_obj_set_style_text_color(s_overlay_label, lv_color_hex(TEXT_COLOR), 0);
    lv_label_set_text(s_overlay_label, "waiting for host");
    lv_obj_center(s_overlay_label);

    ui_build_bar();
    ui_build_cards();
    ui_update_overlay();
    lv_timer_create(ui_tick_cb, 100, NULL);
}
