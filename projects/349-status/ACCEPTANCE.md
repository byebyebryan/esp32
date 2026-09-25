# 349-status v1 device acceptance

## Run in progress — 2026-09-24 PDT

This run checks the reviewed v1 fixes on the physical ESP32-S3 3.49 V2.
The results below are observations from this run, not inherited from the
2026-09-23 prototype checks in [PLAN.md](PLAN.md). V1 remains open until the
suspend/resume gate and a fresh 24-hour connected soak pass.

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
service restarts reported `PCF85063 ok`. This does not establish clock
continuity during host suspend; that still needs the powered-board check.

### Open gates

- Host suspend: verify an asleep overlay within 10 s, advancing RTC clock
  while the board remains powered, and restored normal state within 3 s of
  resume. The user could not suspend the host during this session.
- Fresh 24-hour connected soak after the short gates: record service and
  device identity, unexpected resets, link errors, stale cards, and false
  overlays. No clean 24-hour result has been claimed.

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

After suspend/resume passes, launch a fresh run with a unique UTC run ID in
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

The recorder cannot see pixels. The board still needs a human visual check
at the soak start and end for stale cards or false overlays.

No changes were pushed.
