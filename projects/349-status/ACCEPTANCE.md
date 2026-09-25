# 349-status v1 device acceptance

## Short-gate closure — 2026-09-24–25 PDT

This run checks the reviewed v1 fixes on the physical ESP32-S3 3.49 V2.
The results below are observations from this run, not inherited from the
2026-09-23 prototype checks in [PLAN.md](PLAN.md). The physical short gates
passed. The user chose to defer the optional 24-hour connected soak; no
long-duration stability claim follows from this run.

### Build and environment

| Item | Evidence |
|---|---|
| Reviewed source | `3cc419e` (gap fixes), then `9ddb3b6` (device idle logging) |
| Firmware binary SHA-256 | `6b5ac68df8c35373c3dd4460bdc253d5abad3fb64d9f228075b8f144202fd33c` |
| Firmware ELF SHA-256 | `b249b211443c65cc32df54f131c12986f846495017440e869092acab5ceceec0` |
| Live device identity | `hello.build=9ddb3b6`, `hello.build_sha=b249b2114` |
| Host service | User `349d.service`, final restart at 2026-09-24 21:40:45 PDT; active, linked, not paused |
| Board path | `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_28:84:85:92:C2:20-if00` |
| User config | Original config restored byte for byte after isolated tests and reloaded |

The host suite passed 76 tests on `3cc419e`. The final firmware-only commit
passed a clean firmware rebuild before flashing. The device's hello ELF hash
prefix matches that build. These are build and integration checks; the physical
observations follow.

### Completed short gates

| Gate | Observation |
|---|---|
| Static bar and stale state | On the final firmware, `BAR 東京 が 🛸` stayed visible for over 15 seconds. The user reported no stale host overlay or layout artifacts. |
| Font fallback | The user saw `東京` and `が` on both bar and card, with the unsupported emoji shown as a visible box. |
| Injected card and touch | An isolated card appeared and disappeared immediately when tapped. A separate 1,200 ms injected card appeared once and expired at about 1.22 s by host status. |
| Desktop mirror | An isolated `MIRROR A` notification replaced with `MIRROR B` under one desktop ID, then disappeared after desktop close. The user confirmed the board behavior. |
| Local dismiss and replace | In isolation, tapping `ISOLATED LOCAL A` hid the board card while its desktop notification remained. Replacing the same desktop ID with `ISOLATED LOCAL B` made it visible in both places. A prior crowded run was inconclusive and is not counted as a pass. |
| 20-card burst | Twenty unique desktop IDs were sent in 0.168 s. The user saw two board cards and `+18 more`, with the display responsive. The test IDs were closed afterward. |
| Bar width rejection | Reload rejected a 628 px preset for the 624 px bar, then accepted the restored valid config. |
| Replug | The by-id port returned at 21:38:22.121 PDT; daemon link returned 0.302 s later; matching device hello arrived 1.646 s after port return. The user saw the normal state and no stale overlay. |
| Service restarts | Three consecutive restarts each reached matching device hello and full sync in about 1.4 s; the final service stayed active and linked. |
| Device CPU idle | Final-firmware 10-second alive logs reported 92–98% idle on core 0 and 99–100% on core 1 during observed normal/static-bar windows. The first sample after boot is `na` by design. |

After a full USB power loss during replug, the RTC reported `oscillator
stopped` at boot and was set from the host during sync. Subsequent warm
service restarts reported `PCF85063 ok`. This installation now assumes that
host sleep also removes USB power. A powered-off board cannot show a host
asleep overlay or keep a visible clock running; the replug result exercises
the corresponding cold-start recovery path.

### Mirrored popup expiry follow-up — 2026-09-24 PDT

Real desktop popups could disappear while their board cards remained in the
mirror's count. On this host, DankMaterialShell hides a timed-out popup but
retains its notification-center entry without emitting `NotificationClosed`.
Before the fix, a controlled 1,200 ms popup raised the mirror count from 16
to 17; the count stayed at 17 after popup timeout and returned to 16 only
after explicit desktop close.

The host now expires mirrored board cards at the requested positive timeout,
or at the configured fallback for a server-default (`-1`) timeout. A zero
timeout remains persistent until explicit close. This does not remove the
desktop notification-center entry. The host suite passed 82 tests, including
a live D-Bus mirror test that observes a device `close` without first closing
the desktop notification. No firmware change was required.

`349d.service` restarted at 22:34:34 PDT and reconnected to firmware build
`9ddb3b6`. The fresh sync cleared the stale board state (`notifs: 0`). A live
`-1` timeout probe then raised the mirror count to 1 and returned it to 0
after the five-second fallback, before the probe's desktop ID was explicitly
closed for cleanup. The service remained linked with no warning or error in
its journal. This is a headless count/protocol check; a separate visual claim
about the physical display is not made here.

### Symbol fallback follow-up — 2026-09-24 PDT

The user reported that a curly apostrophe in a real notification appeared as
a box. An isolated card confirmed the distinction: ASCII `we've` rendered in
the title, while `we’ve` (U+2019) showed a box in the body. The host preserved
both characters; the firmware's Montserrat/CJK fonts lacked U+2019.

Generated 14/16 px LVGL symbol subsets now follow Montserrat and Source Han
Sans in the fallback chain. The first build (`build_sha=679281b56`) was
flashed and visually checked. Its two font objects used 56,476 and 70,300
bytes of text data, with no static writable data. Post-flash alive samples
showed 92–94% core 0 idle and 99–100% core 1 idle during this observation
window.

On that first build, the user confirmed that both apostrophe forms rendered in both card
text sizes, together with `東京`, an em dash, an arrow, and a check mark. The
unsupported `🛸` still appeared as a box. The host service remained linked.
The 90-second injected test card then expired, returning the host to
`notifs: 0` with the link still active.

A subsequent static audit found missing `×`, `÷`, and several European letters
that the host does not transliterate. Extending the generated font to Latin-1
and Latin Extended-A raised the repertoire to 2,602 code points at each size;
`tools/check_font_coverage.py` passes its two-size coverage gate. The build
flashed for this audit was `build=cf56cea-dirty`, `build_sha=844085c67`,
matching ELF SHA-256
`844085c67c4db83a018231b127dee362495e76e668ccd8425f2a9f5476923efa`.
The app image is `0x1329f0` bytes in an `0x800000`-byte partition, with 85%
free. The two current font objects use 68,089 and 84,573 bytes of text data
and no static writable data. The host linked after the second flash. A
60-second card exercised `×`, `÷`, `ø`, `ß`, `ł`, and `œ`; it expired and the
host returned to `notifs: 0`. The user missed that card, so it was resent for
two minutes. On the second card, the user visually confirmed all six glyphs
and `東京`. Alive samples on the current build showed 93–94% core 0 idle and
99–100% core 1 idle. This remains a targeted check, not certification of
every glyph or script. The firmware image predates the commit of its source.

A read-only audit of the desktop shell's retained history examined only code
points after the host's normalization and byte limits; it did not print or
retain message contents. Its 33 non-test `ghostty` entries used 72 distinct
code points, all covered by the current font. No Chrome, Google Calendar, or
Slack entries were retained in that sample, so their coverage is unmeasured.

### Deferred checks and limits

- The 24-hour connected soak was stopped at the user's request because its
  expected value did not justify continuing it now. No soak pass is claimed.
- The recorder cannot see pixels, so unattended operation would not prove
  the absence of every brief stale card or false overlay.

Actual host suspend/wake was not exercised. The assumption that USB power is
removed during sleep has not been measured on this host, and the manual
replug timing cannot prove host-specific wake timing. If this host retains USB
power during sleep, the powered-board overlay, RTC, and resume behavior need
a separate test before making claims about them.

### Soak recorder readiness

The read-only [soak recorder](tools/soak_recorder.py) uses the daemon's Unix
socket, systemd properties, and a journal cursor. It records sanitized device
CPU heartbeats and fails the run on link loss, service or device reset, an
unexpected hello/resync, host suspension, or a collection gap. Its output is
private append-only JSONL. It does not expose notification text. A 12-second
and a 6-second live dry run passed with matching firmware and no failures;
a deliberately wrong build was rejected at baseline. A separate six-second
transient user-unit run exited successfully. These dry runs do not count
toward the 24-hour window.
The recorder also samples at the deadline; a six-second check with a two-second
interval recorded samples at 2, 4, and 6 seconds before reporting success.

If a future connected soak is useful, launch it with a unique UTC run ID in
both the unit and output names:

```sh
rtk systemd-run --user --unit=349-status-soak-RUN_ID \
  --property=Restart=no --property=RuntimeMaxSec=25h \
  /usr/bin/python3 /home/bryan/code/esp32/projects/349-status/tools/soak_recorder.py \
  --duration 86400 --interval 15 \
  --output /home/bryan/.local/state/349-status/soaks/RUN_ID.jsonl \
  --expected-build 9ddb3b6 --expected-sha-prefix b249b2114 \
  --firmware-elf /home/bryan/code/esp32/projects/349-status/build/349-status.elf \
  --device-path /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_28:84:85:92:C2:20-if00
```

The recorder cannot see pixels. A future soak would still need a human visual
check at its start and end for stale cards or false overlays.

### Deferred soak attempt

The user confirmed a normal bar with no stale card or false overlay at the
start. Transient user unit `349-status-soak-20260925T051016Z.service` began
recording at **2026-09-25 05:10:31.797 UTC** (2026-09-24 22:10:31.797 PDT)
to `/home/bryan/.local/state/349-status/soaks/20260925T051016Z.jsonl`.
Its baseline records host service PID `741952`, `hello.build=9ddb3b6`,
`hello.build_sha=b249b2114`, and the matching full ELF SHA-256 above. The
recorder unit started as PID `763720`. At the user's request it stopped at
**2026-09-25 05:14:43.795 UTC**, after 252.019 s and 16 samples. Its final
record is `outcome=interrupted`, with no sampled failures. The normal daemon
remained active and linked. This attempt is not a 24-hour soak pass.

No changes were pushed.
