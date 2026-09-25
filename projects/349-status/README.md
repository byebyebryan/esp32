# 349-status

Always-on 640x172 desk display for the ESP32-S3-Touch-LCD-3.49 V2: mirrors a
host-composed status bar and desktop notifications over USB-Serial-JTAG; touch
sends actions back. Design, milestones and hardware findings live in
[PLAN.md](PLAN.md).

## Layout

```
main/               ESP-IDF app: link, proto, state, ui, rtc
host/               Python daemon (349d) and control CLI (349ctl)
components/display_349/   shared panel/LVGL/touch component (see ../../components)
```

## Build and flash

```sh
. ../../scripts/env.sh
idf.py build
idf.py -p /dev/ttyACM0 flash
```

Existing local `sdkconfig` files created before the CJK fallback need the
Source Han Sans 14/16 px font options enabled; `sdkconfig.defaults` selects
them for a fresh configuration.

## Host setup

```sh
cd host
uv sync                 # creates .venv, installs pyserial(-asyncio), dbus-next
```

## Running

```sh
uv run 349d -v                    # foreground, debug logging
uv run 349ctl status              # talk to the running daemon
```

As a user service:

```sh
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/host/349d.service" ~/.config/systemd/user/349d.service
systemctl --user daemon-reload
systemctl --user enable --now 349d
journalctl --user -u 349d -f
```

The unit resolves `uv` from the user manager's `PATH` and needs
`DBUS_SESSION_BUS_ADDRESS` there too (normal on a graphical login; check both
with `systemctl --user show-environment`).

## 349ctl

```
349ctl status                 daemon/link state, revision, notification count
349ctl text "hello"           send a text message to the device
349ctl notify "summary" [body]  inject a test notification
349ctl pause | resume         release/reconnect the serial port
349ctl reload                 reload ~/.config/349d/config.toml
349ctl log [-n N]             recent device log lines
```

Direct-to-device commands when the daemon is stopped: `349ctl --port /dev/ttyACM0
hello|ping|text|listen`.

## Config

`~/.config/349d/config.toml` (or `349d --config FILE`):

```toml
[link]
# port = "/dev/serial/by-id/..."   # default: auto-detect

[daemon]
tick_s = 1.0
sync_interval_s = 60.0

[notifications]
mode = "mirror"            # mirror | off (consume is not implemented)
device_dismiss = "local"   # local | propagate
max_visible = 3
popup_timeout_ms = 5000           # fallback when an app requests server default (-1)
critical_popup_timeout_ms = 0     # 0 keeps critical cards until closed
ignore_apps = ["KeePassXC", "Bitwarden", "1Password"]

[bar]
# Generic zones composed host-side; see PLAN.md for the zone schema.
preset = [
  { id = "clock", kind = "clock", w = 80, format = "%H:%M" },
  { id = "spacer", kind = "spacer", w = 0 },
  { id = "cpu", kind = "text", w = 60 },
  { id = "mem", kind = "progress", w = 70 },
  { id = "vol", kind = "progress", w = 70 },
  { id = "batt", kind = "text", w = 60 },
]
```

`max_visible` is limited to 0–8, and the bar preset can contain at most eight
zones. The configured widths must fit the 624 px content area including 8 px
gaps; a nonspacer with `w = 0` uses 60 px and a spacer uses flex space.
Invalid configuration is rejected rather than silently dropping zones or
sending a frame the device cannot accept. Reload after editing with
`349ctl reload` (or `systemctl --user reload 349d`).

V1 uses Montserrat with a bundled Source Han Sans CJK fallback and a generated
punctuation/symbol subset for bar and notification text. The subset covers
typographic quotes (including `’`), arrows, math signs, shapes, and dingbats;
see [font sources and licenses](main/fonts/README.md). Latin accents become
base letters, while nondecomposing Latin-1 and Latin Extended-A letters have
glyphs. Glyphs outside the font set show a visible placeholder. The
[coverage audit](PLAN.md) describes remaining script gaps. Nerd Font Private
Use icons and color emoji are not included.

`349ctl notify` cards expire after about five seconds by default. Mirrored
cards follow a positive app timeout; for the server-default timeout (`-1`),
normal/low cards use `popup_timeout_ms` and
critical cards use `critical_popup_timeout_ms`; an app timeout of `0` keeps
the card until explicit close. A timeout hides only the board card: the
desktop notification daemon keeps its notification-center history.
Desktop close and replacement still update the board immediately.

The daemon sends a ping every four seconds even when the bar does not change.
While the board stays powered, it shows `host asleep` when USB activity stops
and `host disconnected` when USB is active but host messages stop for ten
seconds. If the host cuts USB power during sleep, the board turns off instead;
a full sync restores the display when it powers up and reconnects.

## Flashing while the daemon runs

The daemon holds the serial port, and opening it resets the chip anyway. Use the
sticky pause so a `Restart=always` unit cannot grab the port mid-flash:

```sh
349ctl pause          # releases the tty (flag file survives daemon restarts)
idf.py -p /dev/ttyACM0 flash
349ctl resume         # reconnects; costs one device reset
```

## Tests

```sh
cd host && uv run pytest -q
```

Covers framing, protocol, state, composition, sources, notification handling,
and daemon/IPC integration against a pty fake device.
