# 349-status UI redesign — working proposal

The 640 × 172 display is an ambient host companion. Its first job is to make
current notification text readable at a glance; its second is to show a small
set of useful host facts. The current 46 px bar and 46 px cards are a working
prototype, not a layout constraint.

## Information hierarchy

1. A current notification's body and title.
2. Time and a small set of host readings.
3. Source, urgency, and number of other active notifications.
4. Secondary details while no notification needs the reading area.

The Zsh right prompt provides candidate data: time, CPU, memory, Bluetooth,
network, and battery. The display should use that set selectively. Seconds,
CPU frequency, power draw, and battery ETA are too changeable or space-hungry
for the default rail. Starship has no system battery; Snap has `BAT0`.

## Proposed geometry

| Region | Pixels | Content |
|---|---:|---|
| Left rail | x=0–159, 160 wide | Host readings; compact clock when cards are active |
| Right content | x=160–639, 480 wide | Large idle clock/date or notification deck |

Start with the one-quarter rail. A one-third rail would be about 212 px wide
and leave 428 px for text; the current stats do not need those extra 52 px.
Keep 8–12 px internal padding and a restrained divider. Test the physical
board before fixing font sizes and exact baselines.

### Left rail and idle clock

- With no cards, the right region presents a large `HH:MM` and date. The left
  rail uses its full height for readings. When a card arrives, the time moves
  to the top of the left rail at about 38–40 px and the right region becomes
  the deck. Do not duplicate the time in both regions. Seconds stay off.
- CPU and memory show percentages with stable labels. Smooth CPU enough to
  avoid a changing digit every frame; do not add a permanent gauge beside the
  number.
- Network shows connected/offline state. Warn visibly on loss; normal state
  stays quiet. A host-side network source is needed.
- Battery appears on hosts with a battery, with charging or low state. Omit
  the row entirely on desktop hosts. Never render `--` for absent hardware.
- Bluetooth count and volume are lower-priority. Promote a change or fault
  briefly without displacing the large idle clock. A Bluetooth source is
  needed.

### Right content

- **One active card:** use nearly the full 480 × 172 region. Small source
  label, one-line title, then up to three body lines at a substantially larger
  size than the current 14 px body. Keep the complete card as a touch target.
- **Two or more active cards:** use a side-peek deck, not two equal-height
  strips. The foreground card gets about 392 × 156 px for readable text; a
  roughly 70 px edge of the next card remains visible at the right. This
  keeps the foreground less stretched than a bottom-peek card. Show `1 of N`
  only when the user can actually move through all N active cards.
- **Critical card:** use clear urgency color and allow its text to take focus.
  Its lifetime follows the existing critical-notification policy.
- **Idle:** the right region is a large clock and date. The left rail keeps
  CPU, memory, network, and conditional battery. A transient volume or
  Bluetooth change can appear below the date without competing with the
  time. Avoid repeating the rail's numbers to fill space.
- **Link stale:** keep the clock visible, mark host readings stale, and use
  the content area for the connection message instead of covering the whole
  screen. If USB power is cut during sleep, the board turns off as expected.

## Text and interaction

- Give body text more height than the source and title. Wrap long body text;
  ellipsize only after the available lines. A missing app name must not become
  a visible `?` label.
- The partial card should be a large enough touch target to bring it forward.
  Prefer a visible dismiss target on the foreground card so a tap to inspect
  or navigate does not accidentally remove it. Test whether swipe navigation
  is reliable on this panel before making it the only way to browse. A
  replacement of a locally dismissed desktop notification can restore the
  card. Do not imply that the overflow count is a button until it can reveal
  the counted notifications.
- New normal notifications enter at the front; tapping the peek advances to
  the next active card. Coalesce a burst before shifting focus so the main
  card does not flicker through every arrival. Desktop close or expiry removes
  a card wherever it is in the deck. Replacement updates that card in place.
  A local dismiss hides the foreground card on the board and brings the next
  one forward without closing the desktop notification.
- Use motion sparingly; changing stats should not flash or shift the card.
  Use color for urgency and faults, not for every reading.
- Keep the visual language quiet: near-black canvas, a slightly lighter rail
  and card surface, strong white text, muted secondary text, and one accent
  for ordinary cards. Amber and red are reserved for faults or urgency. The
  partial card should read as a real layer of the deck, not a decorative bar.
- The current firmware has Montserrat 12/14/16/28 plus 14/16 px CJK and
  symbol fallbacks. Larger body/title sizes need a deliberate font build and
  a physical readability check. Both hosts' current Ghostty configuration
  uses MesloLGS NF (not JetBrains Mono). A mono face is a candidate for
  numerals; notification paragraphs benefit from a proportional face. A full
  Nerd Font icon set is unnecessary for this layout.

## Implementation boundary

The existing host `bar.zones` schema describes a horizontal row using pixel
widths. A vertical rail with conditional readings needs a revised host/device
layout contract; silently reinterpreting those widths would make current
presets misleading. The initial prototype can use today's CPU, memory,
battery, clock, and notification sources. Network and Bluetooth require new
host probes. The interaction and text hierarchy should be checked on the
actual board before selecting the final font assets or committing a protocol.

### Active-card cache

The cache implementation contract is in
[card-cache-plan.md](card-cache-plan.md), with completed Snap board checks in
[ACCEPTANCE.md](../ACCEPTANCE.md). The side-peek layout and navigation remain
the next goal. Further physical checks now target Starship's connected board,
where the user is present.

Prefer a device-owned deck over a host-owned focus cursor. The host already
sends each new/replaced card and its close event, and remains authoritative
for expiry. The device can cache the active cards, choose its foreground and
next preview immediately on touch, and preserve today's local-dismiss rule.
This means active notifications, not notification-center history.

Capable firmware now keeps the newest 32 active cards by default in a PSRAM
cache, with a second staging buffer for complete transfers. The host retains
any additional active cards and reports a truthful overflow count. The legacy
snapshot still sends at most three cards by default and the legacy firmware
path retains eight. Each wire line remains limited to 8192 bytes. A card
record is about 268 bytes, so 32 records use about 8.4 KiB per buffer before
UI bookkeeping. The current UI copies only its two visible cards onto its
task stack.

A reconnect uses the capability-gated `sync_begin` / `sync_cards` /
`sync_commit` transfer and publishes the new set only after all chunks arrive.
Normal `notify` and `close` messages remain incremental. An invalid or missing
chunk requests a new full sync; stale IDs and local hidden IDs are reconciled
only on a complete one.
If a real workload exceeds 32 concurrent active cards, either add paging from
the host or increase the measured device budget before promising that every
`1 of N` card can be browsed.
