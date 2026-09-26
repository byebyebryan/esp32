# Active-card cache plan

This is the implementation contract for the first 349-status UI redesign
goal. It makes the active notification set available to the device so the
later side-peek deck can respond to touch without a host round trip. It does not add a desktop
notification history or change notification expiry ownership.

## Status

The host protocol, firmware cache, and safe two-card UI handoff are implemented.
The Snap board checks are recorded in [ACCEPTANCE.md](../ACCEPTANCE.md),
including overflow/refill, replug, local dismiss, and malformed-transfer
recovery. The side-peek layout, larger text, rail, and navigation remain the
next separate goal. Further physical checks target Starship's connected board,
where the user is now present.

## Contract

- The host owns the active set and sends new cards, replacements, and closes.
  The device owns which cached card is in front and which IDs are hidden by a
  local dismiss. A replacement of a hidden ID makes it visible again.
- Start with the **newest 32 active cards** as a provisional device capacity.
  The host retains the complete active set. The device reports a separate
  overflow count for active cards it cannot browse. Its position indicator
  counts only cached, nonhidden cards; it never suggests an overflow card is
  reachable by tapping the deck.
- Retain each card's ID, app, title, body, and urgency. Keep the current UTF-8
  byte limits until the larger-font layout has been tested on the board.
  A desktop close or host expiry removes a card from the device; a local
  dismiss does not close it on the desktop unless `device_dismiss=propagate`.
- Preserve the current legacy protocol while either host or firmware is old.
  The device advertises a new card-sync capability in `hello`; the host uses
  the new transfer only after seeing it. New firmware also accepts legacy
  `sync` messages. A per-boot ID in the new `hello` lets the host ignore the
  immediate duplicate hello caused by its own greeting while still syncing
  after a later device restart.

## Full-state transfer

The current 8192-byte line limit cannot carry 32 maximum-length cards in one
`sync`. Add a transaction with a per-connection transfer ID:

1. `sync_begin`: ID, host revision, bar/clock/media, effective cache limit,
   cached-card count, and active-card overflow count. The device validates
   the limit against its physical capacity and keeps it for later deltas.
2. `sync_cards`: ID, starting index, and a consecutive batch of card objects.
3. `sync_commit`: ID. The device validates the count, indices, and unique IDs,
   then publishes the staged state in one swap and reconciles hidden IDs.

Build batches using the *encoded UTF-8 length*, with a target of at most
2 KiB per `sync_cards` line; keep every other line below the existing 8192-byte
limit. The smaller chunks leave room in the 4 KiB USB RX buffer. A malformed,
missing, duplicate, timed-out, or out-of-order batch aborts staging, keeps
the last committed state, and requests a new sync once; queued tail chunks
from the failed transfer are discarded. A link reconnect discards any
unfinished transfer. The device does not show partially transferred cards.

The host must serialize **model mutation and its wire output**, not just calls
to `write()`: a new notification cannot be sent ahead of an older snapshot or
between its begin and commit. The existing model `rev` is useful for logs but
is not sufficient for ordering because a repeated replacement may need to
unhide a card without changing that revision. Periodic sync, device resync,
config reload, and reconnect all use the same transfer path.

Incremental `notify` and `close` remain single messages after the committed
transfer. Include the host's active total so the device can derive a truthful
overflow count after either event. A new-protocol `notify` also marks whether
its ID belongs to the newest-N selection: replacing an older omitted card must
not evict a cached one. When a close creates a cache vacancy while more host
cards exist, the host schedules a coalesced full transfer to refill it. That
transfer also repairs any missed delta.

## Implementation slices

1. **Host protocol and compatibility.** Add bounded snapshot batching and
   capability selection; serialize state changes with transfers. Keep legacy
   `sync` for old firmware. Introduce a new `cache_limit` setting (default 32,
   maximum the device advertises); retain `max_visible` for the legacy path
   until both installations are migrated. Update config and protocol docs.
2. **Firmware state.** Allocate a bounded committed cache and staging cache
   outside the LVGL task stack, preferably in PSRAM. Measure free and minimum
   heap on the actual board before fixing 32 as the production capacity. Add
   transaction validation, timeout/resync, delta handling, hidden-ID
   reconciliation, and an atomic commit under the state mutex. Expose a
   read-only count and ordered IDs of the committed card set for acceptance
   checks.
   Keep the eight-card legacy path available for old hosts.
3. **UI handoff.** Replace the current `visible[STATUS_MAX_NOTIFS]` stack copy
   with a selection routine that copies only the foreground card and next
   preview. Keep the existing card layout initially to validate the cache.
   The side-peek layout, larger fonts, rail, and touch navigation follow as a
   separate UI goal using the tested cache API.

## Acceptance gates

- Host tests cover 0, 1, 20, 32, and 33+ active cards; maximum escaped UTF-8
  text; encoded frame limits; transaction ordering during concurrent arrival
  and close; replacement of a locally hidden ID and an omitted ID; overflow
  refill; interrupted transfer; and legacy firmware selection.
- Firmware build and device checks show no stack or allocation failure and
  record the minimum heap through burst, repeated sync, and reconnect. A bad
  transfer leaves the last complete card set visible and recovers via resync.
- On Snap's second board, send a 20-card burst, then replace and close cards
  at the front and middle. Compare the device's count and ordered IDs with the
  host's selected active set before and after one replug, with no stale cards
  or freeze. Send 33+ cards to check truthful overflow, then close enough to
  trigger a refill. Verify local dismiss does not close the desktop card and
  replacement restores it.
- After the cache passes, use the same scenarios to accept the side-peek UI:
  browsing is immediate, its position count covers reachable cards, and a
  burst does not flicker through each arrival. This is a separate visual/touch
  gate, not a condition for landing the cache protocol.

No 24-hour soak or host-suspend test is part of this goal. The board is
expected to lose USB power when the host sleeps.
