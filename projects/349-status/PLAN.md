# 349-status — plan

Always-on 640x172 desk display for the ESP32-S3-Touch-LCD-3.49 V2: mirrors
clock, host-composed status zones and notifications from one PC over
USB-Serial-JTAG; touch sends actions back.

Decisions (2026-09-23):

- Extract the 349-hello display pipeline into `components/display_349` (M1).
- Host dependencies managed with `uv`.
- v1 is text-only. Montserrat remains the primary font, with the bundled
  Source Han Sans 14/16 px CJK subset and a generated punctuation/symbol
  subset as fallbacks. Latin accents are reduced to base letters; unsupported
  glyphs use LVGL's visible placeholder.
- The status area is **generic zones** composed by the host; notifications,
  clock and media stay typed. New content that fits an existing zone kind does
  not require a firmware change.

Non-goals for v1: app artwork, notification history/scrollback, `consume`
notification mode, WiFi, audio, battery tuning, now-playing/media controls
(M4 dropped), and bespoke per-content widgets (the status area is generic
zones).

## Current validation boundary (2026-09-25)

The hardware findings below describe the 2026-09-23 builds. The fresh
on-device run for the later review fixes is recorded in
[ACCEPTANCE.md](ACCEPTANCE.md). Host tests and firmware builds establish code
readiness; the recorded board observations establish only the short gates
that were exercised.

| Area | Current boundary |
|---|---|
| M0–M3, M5 implementation | Present in source; fresh short-gate results are in `ACCEPTANCE.md` |
| M4 media | Dropped from v1; protocol/rendering hooks remain dormant |
| V1 acceptance | The first board passed static-bar, touch/notification, and replug checks. Snap's second board has a separate bring-up smoke check in `ACCEPTANCE.md`, not the full short-gate run. The user deferred the optional 24-hour soak after a 252 s attempt; no long-duration stability claim is made. The first installation assumes USB power is removed during host sleep; actual host suspend/wake is unverified. |
| Active-card cache | Implemented with capability-gated chunked sync, up to 32 cached cards, overflow/refill, and legacy compatibility. Snap cache/recovery checks are recorded in `ACCEPTANCE.md`. |
| UI redesign | The [working UI brief](design/ui-brief.md) selects a quarter-width left rail, large idle clock/date, and a foreground notification with a side peek. Layout and navigation implementation are next; physical checks now target Starship's board. |

The acceptance run must record which host process and device firmware build
were used. The device `hello.build` and `hello.build_sha` report its app
descriptor version and an ELF hash prefix; a running service or the
`fw=0.2.0` label alone does not identify the revision.

### On-device v1 acceptance loop

1. Record the reviewed source revision, host process start time, board path,
   built firmware hash, and the device's `hello.build_sha`. Bring the service
   and board onto that build before treating live observations as evidence.
2. Check a static bar for more than 10 s with no false stale overlay; then
   exercise touch dismiss, desktop close, replacement, a 20-card burst,
   injected-card expiry, bar fit, and representative CJK text and missing
   glyph placeholders.
3. Measure replug discovery and full-state recovery against the M2 targets
   below, and device CPU headroom. For this installation, assume host sleep
   removes USB power: the board cannot show an asleep overlay or keep its clock
   visible while off. Record actual host suspend/wake as an untested host
   behavior rather than using a powered-board sleep gate.
4. The user deferred the optional 24-hour connected soak as low value for now.
   Keep the recorder available for a later stability question, but do not
   treat the interrupted attempt as a pass. A failed short gate is repaired
   and repeated before declaring v1 short-gate acceptance closed.

This loop is hardware acceptance work. The source fixes and offline builds do
not stand in for flashing, service rollout, or visual/touch observations.

## Architecture

```
PC                                          ESP32-S3 3.49 V2
┌────────────────────────────┐              ┌────────────────────────────┐
│ 349d (systemd --user)      │     USB      │ 349-status                 │
│  sources: clock, /proc,    │◄────────────►│  link task (USJ driver)    │
│  wpctl, power_supply,      │   NDJSON     │  proto (cJSON)             │
│  notifications (mirror)    │  @349 ...    │  state model               │
│  state + rev + snapshot    │              │  LVGL UI (dirty rects)     │
│  unix socket ← 349ctl      │              │  touch → input             │
└────────────────────────────┘              └────────────────────────────┘
```

Device task model (M2 onward):

- `link` task (prio 5): USB driver, line framing, cJSON parse, update state
  under a mutex, and set a dirty flag.
  Never touches LVGL.
- `LVGL` task (prio 2, core 0): `lv_timer_handler`; a 100 ms UI timer drains
  the dirty flag and applies widget updates under the LVGL lock. No unbounded
  device state queue: a storm sets the same flag, and an RX overflow requests
  a fresh full sync.
- `main`: logs a health line every 10 s. The UI timer checks link staleness.
- All UI mutation goes through `display_349_lock()`.

## Protocol

Line-delimited JSON, `@349 ` prefix; unknown fields ignored. Field freeze for
v1 (types in parentheses):

| dir | t | fields |
|---|---|---|
| d→h | `hello` | `proto` (int), `fw` (str), `build` (app version), `build_sha` (ELF hash prefix), `cap` (str[]) |
| h→d | `ping` | optional `ts`; daemon heartbeat every 4 s |
| d→h | `pong` | optional `ts` echoed from `ping` |
| h→d | `sync` | `rev` (int) + full `bar`, `clock`, `media`\|null, `notifs`[] (capped), `notifs_overflow` |
| h→d | `bar` | `zones`[]: `{id, kind: text\|progress\|clock\|media\|spacer, w, text?, value?, format?, color?, align?}` |
| h→d | `clock` | `epoch` (int, UTC seconds), `offset` (int, seconds east) |
| h→d | `media` | `state` (`playing\|paused\|stopped`), `title`, `artist`, `album`, `pos` (s, float), `len` (s, float) |
| h→d | `notify` | `id` (int), `app`, `summary`, `body`, `urgency` (0-2), `expire` (ms), `ts` |
| h→d | `close` | `id` |
| d→h | `input` | `action` (`dismiss\|playpause\|next\|prev`), `id?` |
| d→h | `resync` | `reason` (`rx_overflow\|parse_error`) |
| d→h | `ack` | `v` (debug echo; not required in v1 flow) |

The active-card cache extension is capability-gated and leaves these v1
messages available for older hosts and firmware. See
[the cache plan](design/card-cache-plan.md) for `sync_begin`, `sync_cards`,
`sync_commit`, and the device readback used for acceptance.

Zone kinds: `text` (label, optional `color`), `progress` (`value` 0..1,
optional `text`), `clock` (rendered from `clock` + `format`, ticks locally),
`media` (dormant rendering path retained after M4 was dropped),
`spacer`. A full `bar` message replaces all zones — no deltas, no partial
state. The host composes zones from whatever sources it wants (sysinfo, volume,
weather, CI, ...); the device never needs to know what they mean.

Rules:

- Host sends `hello` and `ping` after port open, then a full transfer after
  device `hello`, every 60 s, and on a device `resync` request. Capable firmware
  uses chunked card sync; older firmware uses the single legacy `sync`.
- `notify.urgency` comes from the `hints` dict, not a `Notify` argument;
  `expire` comes from `expire_timeout`. An absent zone `value`/`text` renders
  as unknown (`--`).
- Truncation: app ≤ 31 bytes, summary ≤ 63 bytes, body ≤ 159 bytes,
  UTF-8-safe cuts (matching device string buffers); zone text is ellipsized.
  The host trims cards from a `sync` until its encoded line fits 8 KB.
- Layout limits: max 8 zones; host config reserves 16 px outer padding and
  8 px between zones, so every configured zone fits. The device still clamps
  and drops trailing zones if an external sender sends an oversized bar.
- Rates: `bar` on sampled content change (normally ≤1 Hz), `clock` on `offset`
  change (the RTC ticks locally and periodic sync refreshes it), `ping` every
  4 s, notifications event-driven and
  rate-limited to 20/s; the dormant media path has no host source.
- Device interpolates `media.pos` between updates.
- The device never polls the host for content. While the board stays powered,
  lost USB SOF → "host asleep"; USB present with no host message for 10 s →
  "host disconnected". If USB power is removed, the board is off instead.
- A `replaces_id` notification unhides a locally hidden card.
- `hello` is also sent by the device on boot, and in reply to a host `hello`.

## Milestones

### M0 — Link (done 2026-09-23, S)

New project, USJ driver + `usb_serial_jtag_vfs_use_driver()`, line framing with
prefix filter and 8 KB cap, `hello` on boot, minimal `349ctl`. Result:
10000/10000 pings, 0 malformed, 0 retransmits, 2596 msg/s with logs
interleaved; replug reconnects on the same by-id path. Findings at the end of
this file.

### M1 — Display component (M)

Extract the 349-hello pipeline into `components/display_349/` (leading letter
avoids any tooling edge cases with digit-leading component names):

```
components/display_349/
  CMakeLists.txt
  idf_component.yml        # lvgl/lvgl 9.5.0, esp_lcd_axs15231b, esp_io_expander_tca9554
  include/display_349.h
  display_349.c            # panel init, LVGL port, shadow fb, GDMA staging, flush
  touch.c                  # AXS15231B I2C touch + coordinate mapping
  board.c                  # TCA9554 (BL_EN, LCD_RST), backlight PWM, I2C buses
  board_349.h              # pins, panel geometry, EXIO bit assignments
```

Public API:

```c
esp_err_t      display_349_init(void);            // buses, expander, panel, LVGL, backlight off
lv_display_t  *display_349_lvgl(void);            // for buffer/user-data access if needed
bool           display_349_lock(int timeout_ms);
void           display_349_unlock(void);
esp_err_t      display_349_backlight(uint8_t percent);   // 0 = off
```

Keep the 349-hello behaviours verbatim: QSPI 40 MHz, `LV_DISPLAY_ROTATION_90`,
DIRECT mode single full-screen PSRAM buffer, shadow transpose with the 16384 px
rebuild threshold, two DMA chunk buffers, async memcpy. The perf idle reader
(`my_idle_percent` + `LV_SYSMON_GET_IDLE` compile definition) stays in the app,
not the component.

Refactor `projects/349-hello/main/main.c` down to demo UI + perf reader, and
make `projects/349-status` show a static label using the component.

*Accept:* 349-hello frame period still ~22 ms, artifact-free; both projects
build; `git diff` shows the pipeline moved, not rewritten.

**Done on hardware (2026-09-23):** 349-hello runs at 22.4–23.4 ms period
(flush ~13.8 ms, min ~20.9 ms) with no panics — unchanged from before the
extraction; 349-status boots, shows the static label and answers
`ping`/`text` over the link.

**Version pinning (2026-09-23):** the component manager resolved *different*
LVGL 9.x versions per project from the same `^9` range (349-status got 9.2.2
while 349-hello had 9.5.0; a re-resolve pulled 9.6.0). 9.2.2 rendered scan
lines/artifacts on this panel with DIRECT-mode rotation, and a stale
349-status `sdkconfig` made it worse. `components/display_349` now pins
`lvgl/lvgl: "9.5.0"` exactly, both projects' `dependencies.lock` are committed,
and `sdkconfig` must be regenerated when the LVGL version changes. The
component also declares `esp_timer` explicitly (it previously compiled only via
a transitive include path that the version change broke).

### M2 — Status UI + sources (M)

The default zone preset is host-side config. V1 keeps Montserrat for common
glyphs and falls back to LVGL's bundled Source Han Sans 14/16 px CJK subset,
then a generated 14/16 px punctuation and symbol subset in bar and notification
text. Latin accents become base letters; glyphs outside those fonts show LVGL's
placeholder. The symbol subset includes typographic quotes, arrows, math signs,
shapes, and dingbats. Nerd Font Private Use icons and color emoji remain outside
the v1 text repertoire. On-device acceptance checks representative text and
the CPU/flash cost of the fallback.

**Text coverage audit (2026-09-24):** The generated repertoire has 2,602
distinct code points at each card text size. It covers all printable ASCII,
Latin-1 and Latin Extended-A, plus the selected punctuation/symbol smoke set
in `tools/check_font_coverage.py`. The host still reduces most decomposable
Latin accents to base letters. The bundled CJK subset contains 1,118 Han
characters and 77 Hiragana/73 Katakana; this is useful for short examples but
does not cover general Chinese or Japanese text. A small Chinese sample missed
`测`, `试`, and `败`. Greek, Cyrillic, Hangul, and supplementary emoji have no
glyphs in the current repertoire. Arabic/Indic text would also need shaping,
not just more glyphs. The 2,602 count measures stored code points, not the
fraction of natural-language text that will render.

For the intended mostly English workload, a read-only check of 33 retained
`ghostty` notification-history entries found 72 distinct code points after
applying the host's text normalization and byte limits; all were in the font.
That history had no retained Chrome, Calendar, or Slack entries, so it does not
establish coverage for those sources. No notification text was recorded in
the audit output.
The user identified Codex CLI, Claude Code, OpenCode, and Chrome notifications
from Google Calendar and Slack as the main sources. Their messages are
overwhelmingly English; Simplified Chinese is a secondary preference, not a
v1 gate. Any further font expansion should follow missing characters observed
in those sources, with a separate coverage decision for broader Chinese text
or emoji.

Device: `state.c/h` (model + mutex + dirty flag, no unbounded queue), `ui.c/h`
(generic zone renderer: flex row, kinds `text|progress|clock|media|spacer`,
width clamping, UTF-8-safe ellipsis, drop-trailing-on-overflow; notification
card area; asleep/waiting overlays), `rtc.c/h` (PCF85063 set from `clock`; the
RTC is what carries time through the port-open resets), `proto.c` dispatch,
`link.c` exposes `link_host_connected()`; state tracks the last host message.

Host: `config.py` (TOML, defaults + zone preset), `state.py` (merge + revision
+ snapshot), `composition.py` (builds `bar` zones from sources),
`sources/{clock,sysinfo,volume,power}.py`, `daemon.py` reconnect and send
loops, and `fake.py` — a pty fake device so host-side work can proceed without
flashing.

**Host side done (2026-09-23):** `proto.py`, `state.py`, `config.py`,
`composition.py`, `sources/`, asyncio `daemon.py` (reconnect, hello→sync,
1 Hz bar, periodic sync), `fake.py`, and 33 tests including a daemon↔fake-device
integration test. Device side (state/ui/rtc) still to do, then on-device
verification once the board is back.

**Device side done on hardware (2026-09-23):** `state.c` (mutex + dirty flag),
`ui.c` (generic zone renderer, overlays), `rtc.c` (PCF85063; reports ok), proto
dispatch and `resync` on RX overflow/parse error. Verified with the real
daemon: the bar renders cleanly (clock from the RTC, media placeholder,
cpu/mem/vol zones), a daemon restart re-syncs the device within ~2 s, and a
zone added purely in host config (`M2 OK`) appeared without reflashing.

**M2 findings:**

1. **Port-open can land the chip in download mode.** The kernel raises DTR and
   RTS together, so GPIO0 may be sampled low and the device boots into the ROM
   loader with no console and no app. The daemon now reaches the pyserial
   instance through `writer.transport.serial` and pulses EN with GPIO0 high
   (`DTR=0, RTS=1, 0.1 s, RTS=0`) after every open; `349ctl` does the same.
2. Preset `text` zones keep their static text when no dynamic value is supplied
   (otherwise host-side labels were replaced by `--`).

*Accept next:* missing-port discovery checks every 0.5 s; replug → correct
state within 3 s; device CPU headroom measured with the alive log;
a new zone added purely in host config shows up without reflashing; host can
run against `fake.py`. A powered-board sleep target (asleep ≤10 s, RTC clock
continuity, recovery ≤3 s) applies only when USB power is retained. Under
the power-off-on-sleep assumption, wake follows the cold replug path; actual
host suspend/wake timing remains unverified.

### M3 — Notifications mirror (M)

Host `sources/notifications.py`: `NotificationSource` modes `mirror|off`.
Monitor connection via
`org.freedesktop.DBus.Monitoring.BecomeMonitor` with rules
`interface='org.freedesktop.Notifications'` **plus**
`type='method_return',sender='org.freedesktop.Notifications'` (sender-narrowed
to keep unrelated FD-carrying traffic out). The assigned notification ID only
exists in the unicast method reply, so track `(client sender, call serial)` and
map the reply to its returned ID. Validated on this machine (2026-09-23,
dbus-broker + Quickshell): `Notify` call `serial=2` → reply `uint32 64`;
`CloseNotification(64)` → `NotificationClosed(id=64, reason=3)`. If correlation
ever fails, degrade to "desktop dismiss does not remove device cards" — never
desync silently. A second bus connection handles `CloseNotification`
propagation.

Parse `Notify` args (app_name, replaces_id, app_icon, summary, body, actions,
hints, expire_timeout) and `NotificationClosed` (id, reason 1/2/3). Config:
`mode`, `device_dismiss` (`local|propagate`), `ignore_apps`, `max_visible`,
`popup_timeout_ms`, and `critical_popup_timeout_ms`.
Ship a default `ignore_apps` list for password managers/authenticators:
notification text can contain OTPs and this display mirrors it. Rate-limit
bursts to one message per 50 ms.

When the monitor session is lost, mirrored cards are cleared because their
desktop IDs can no longer be correlated. Mirrored board cards expire locally
when the popup lifetime elapses: a nonnegative app timeout is honored, while
`-1` uses the configured fallback (5 s normal/low, persistent critical by
default). This leaves desktop notification-center history alone. `349ctl`
injected cards also expire locally when their positive `expire` value elapses.
A complete device sync prunes locally hidden IDs that are no longer active; a
capped sync preserves them.

Device: card stack, overflow count badge, touch dismiss → `input`, local
hidden-id set, unhide on `replaces_id`.

**Host side done (2026-09-23):** `NotificationSource` with reply correlation,
`replaces_id` handling, ignore list, local vs propagate dismiss, a monitor
supervisor that reconnects with backoff, and two end-to-end tests on the live
bus (`notify-send` path and device-dismiss propagation). Device side pending.

**Device side done on hardware (2026-09-23):** card stack (2 cards + `+N more`),
LVGL pointer indev over the AXS15231B touch with release debounce, tap →
`input` dismiss with local hide (and unhide on replace), overflow accounting so
the count is truthful between syncs. Verified live: card renders, tap hides it
(local mode leaves the desktop notification), `propagate` closes both, and a
20-notification burst shows `+18 more` immediately and stays responsive.

**M3 findings:**

1. **The touch controller drops samples mid-press** (6 press edges in 160 ms),
   so LVGL never saw a stable press/release pair and no click was generated.
   Three consecutive empty reads are now treated as still pressed.
2. **Card child containers swallow taps**: base `lv_obj` objects are clickable
   by default, so the tap hit the inner column/row instead of the card. Inner
   containers are created with `LV_OBJ_FLAG_CLICKABLE` removed.
3. Coordinate mapping confirmed on hardware: raw landscape `(x, y)` →
   native `(y, 639 - x)`; the card tap logged raw `(151,70)` → native `(70,488)`.
4. When the device drops the oldest notification at its 8-item cap it now
   increments the overflow count, so `+N more` is correct immediately instead
   of only after the next 60 s sync (which had looked like lag).
5. **A desktop popup timeout need not close the notification.** On this host,
   DankMaterialShell sets `popup = false` at timeout but retains the entry in
   its notification center, so no `NotificationClosed` arrives. A 1.2 s probe
   raised the mirror count from 16 to 17; it stayed 17 after the popup timeout
   and returned to 16 only on explicit close. The mirror now expires the board
   card independently and keeps the desktop entry untouched.

*Accept:* `notify-send` shows both places; desktop dismiss removes the card;
device dismiss is local by default and propagates when configured; replaced
notifications update in place; popup expiry clears the board card and overflow
count even if desktop history retains the notification; 20-notification burst
stays responsive.

**Findings (dbus-broker specifics):**

1. **Monitors that don't negotiate unix FDs get disconnected.** A broad
   `type='method_return'` rule matches FD-carrying messages (PipeWire/portal),
   and dbus-broker drops the monitor ("does not support receiving file
   descriptors it subscribed to"). Fix: `negotiate_unix_fd=True` on the monitor
   connection and close the received descriptors (dbus-next never does).
2. **Monitors that send anything get disconnected** ("attempted to send a
   message"). dbus-next auto-replies `UNKNOWN_METHOD` to every unhandled
   method call it sees, including eavesdropped ones; the monitor handler must
   return `True` to mark messages handled.
3. Narrowing the return rule with `sender='org.freedesktop.Notifications'`
   works on dbus-broker and keeps unrelated traffic (and FDs) out.
4. A dead monitor fails silently otherwise, so the source supervises its own
   connection and reconnects with backoff.

### M4 — Media + actions — dropped (2026-09-23)

Not useful for this setup; MPRIS/now-playing is out of scope. The protocol
keeps the `media` message and the device keeps the `media` zone kind (dormant,
no cost), so a host-side source could be added later without a firmware change.
The default preset no longer includes a media zone.

### M5 — Packaging + ops (S)

`349d`: asyncio daemon (`__main__.py`), reconnect with backoff, heartbeat,
`ipc.py` Unix socket, SIGHUP config reload, device logs mirrored into the
journal. `349ctl`: `status|text|notify|pause|resume|reload|log`, plus `--port`
direct mode for when the daemon is down. **Pause is sticky:** a flag file in
`$XDG_RUNTIME_DIR` that the daemon checks, so `Restart=always` cannot grab the
tty mid-flash; `resume` clears it (and costs one device reset, M0 finding 1).
`349d.service`: `Restart=always`, `WantedBy=default.target` (not
`graphical-session.target`), and `DBUS_SESSION_BUS_ADDRESS` must be present in
the user manager environment.

*Accept:* `systemctl --user enable --now 349d`; survives replug and daemon
restarts; `349ctl pause` frees the tty for `idf.py flash` even across daemon
restarts; crash-loop test (daemon restarts repeatedly, device returns to
correct state). The user deferred the optional 24 h soak; actual host
suspend/wake is a separate integration limit under the power-off-on-sleep
assumption.

**Done on hardware (2026-09-23):** `349d.service` installed and enabled;
`349ctl status|text|notify|pause|resume|reload|log` over the Unix socket plus
`--port` direct mode; sticky pause (flag file) frees the tty and survives
daemon restarts; SIGHUP/`systemctl --user reload` applies a config change live;
three consecutive service restarts each recovered (hello → sync, bar restored).
The service was left running for a 24 h soak. The current validation boundary
above supersedes this historical status; the later connected soak was
explicitly deferred by the user, without a pass claim.

**M5 findings:**

1. `systemctl --user reload` needs `ExecReload=/bin/kill -HUP $MAINPID` in the
   unit; SIGHUP alone is not enough.
2. The unit runs `uv run --frozen 349d` with `WorkingDirectory` set to
   `host/`, so the venv is used as-is and no dependency resolution happens at
   service start.
3. `DBUS_SESSION_BUS_ADDRESS` is present in the user manager environment on
   this setup (`systemctl --user show-environment`), which the notification
   mirror needs; the README documents the check.
4. `~/.config/349d/config.toml` is picked up automatically when it exists;
   `349ctl reload` re-applies it in place so the notification source and link
   loop keep their references.

### Cross-cutting

- Device watchdog: keep the task WDT enabled; UI work must never run in the
  link task.
- Host unit tests: framing, proto round-trip, state merge, notification parsing
  from recorded D-Bus messages; pty-based reconnect test.
- Commit per milestone (user commits; no commits from tooling).

## Risks

- Console and data share the USB-Serial-JTAG port; parser tolerates log
  interleaving, writes go through the driver's TX mutex.
- The daemon holding the tty blocks `idf.py flash`; sticky `349ctl pause` (M5)
  is the answer, not `systemctl stop`.
- If host sleep retains USB power, stopped SOF packets make
  `usb_serial_jtag_is_connected()` the primary asleep detector; read timeout
  is the fallback. If USB power is removed, the device turns off.
- Notification storms and text-heavy LVGL layouts: rate-limit, cap visible
  cards, measure CPU in M2.
- Host-composed zones can overflow or look bad; the device clamps widths, drops
  trailing zones and ellipsizes, and the zone budget stays small (≤8).
- Fonts: Source Han Sans covers a bundled CJK subset, not all Unicode; LVGL's
  placeholder exposes remaining gaps. Check legibility and performance on the
  board before expanding coverage.
- Privacy: notification text (OTPs) is mirrored; default `ignore_apps` and a
  README note.
- Bus implementation: this machine runs **dbus-broker** with Quickshell as the
  notification daemon. dbus-broker is strict about monitors (FD negotiation,
  no sending); the monitor rules and reconnect supervisor in M3 encode those
  requirements, and the graceful-degradation path stays.
- Transport: port-open resets the chip (M0 finding 1); accepted, and the
  TinyUSB migration is localized to `link.c`/`link.py` if it ever matters.

## M0 findings

1. **Opening the port resets the chip, and this cannot be avoided on the S3.**
   Linux raises DTR/RTS on every tty open (`tty_port_block_til_ready()`, while
   `C_BAUD != 0`) and USB-Serial-JTAG treats that as a reset pulse. Only C6 and
   later can disable it; on the S3 the escape hatch is USB-OTG/TinyUSB, at the
   cost of the ROM auto-flash path. Mitigation: the daemon holds the port open
   for its whole lifetime, and `349ctl` will talk to the daemon over a socket
   instead of opening the tty (M5). Every daemon (re)start costs one ~1 s
   device reboot.
2. **Never assemble frames on the link task stack.** An 8 KB `char` buffer on a
   4 KB task stack crashed the device under load (it looked like a throughput
   problem: 116 msg/s with ~100 silent reboots). Static TX buffer + mutex fixes
   it; throughput went to ~2600 msg/s.
3. `usb_serial_jtag_is_connected()` stays true while no process has the port
   open; it follows SOF packets, which makes it the right host-asleep signal.
4. `usb_serial_jtag_vfs_use_driver()` keeps console output and framed data on
   one port; 10k data lines interleaved with logs produced zero framing
   errors.
