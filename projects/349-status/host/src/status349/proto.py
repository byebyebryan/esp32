"""349-status wire protocol: line-delimited JSON behind a magic prefix.

The device echoes console output on the same port, so every data line carries
the prefix and everything else is ignored.
"""

from __future__ import annotations

import json
import unicodedata

PREFIX = "@349 "
PROTO_VERSION = 1
LINE_MAX = 8192
CARD_SYNC_CAPABILITY = "card-sync-v1"
CARD_CHUNK_MAX = 2048


def display_text(value: str) -> str:
    """Keep Unicode for the device font fallback and simplify Latin accents."""
    chars: list[str] = []
    for char in unicodedata.normalize("NFC", value):
        if char in "\r\n\t":
            char = " "
        elif unicodedata.name(char, "").startswith("LATIN"):
            base = unicodedata.normalize("NFKD", char)
            if base and " " <= base[0] <= "~":
                char = base[0]
        chars.append(char)
    return "".join(chars)


def clip_utf8(value: str, max_bytes: int) -> str:
    """Fit a device string buffer without splitting a UTF-8 code point."""
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def encode(obj: dict) -> bytes:
    line = (PREFIX + json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(line) > LINE_MAX:
        raise ValueError(f"protocol line is {len(line)} bytes; device limit is {LINE_MAX}")
    return line


def classify(line: str) -> tuple[bool, dict | None]:
    """Split a line into (is_data, message); message is None if malformed."""
    if not line.startswith(PREFIX):
        return False, None
    try:
        obj = json.loads(line[len(PREFIX):])
    except json.JSONDecodeError:
        return True, None
    return True, obj if isinstance(obj, dict) else None


def hello() -> dict:
    return {"t": "hello"}


def card_sync_capacity(message: dict) -> int | None:
    """Return the advertised cache size when the device supports card sync."""
    capabilities = message.get("cap", [])
    if isinstance(capabilities, str):
        capabilities = [capabilities]
    if not isinstance(capabilities, list) or CARD_SYNC_CAPABILITY not in capabilities:
        return None
    capacity = message.get("cache_cards")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        return None
    return capacity


def card_status(message: dict) -> dict | None:
    """Extract a well-formed firmware cache readback response."""
    values = [message.get(name) for name in ("count", "overflow", "capacity")]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        return None
    ids = message.get("ids")
    if not isinstance(ids, list) or any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        return None
    count, _overflow, capacity = values
    if len(ids) != count or count > capacity or len(set(ids)) != count:
        return None
    return {"count": values[0], "overflow": values[1], "ids": list(ids), "capacity": values[2]}


def card_sync_messages(snapshot: dict, tx: int) -> list[dict]:
    """Build a bounded begin/cards/commit transfer for a card-cache snapshot."""
    cards = snapshot["notifs"]
    begin = {
        "t": "sync_begin",
        "tx": int(tx),
        "rev": int(snapshot["rev"]),
        "bar": snapshot["bar"],
        "clock": snapshot["clock"],
        "media": snapshot["media"],
        "limit": int(snapshot["limit"]),
        "count": len(cards),
        "overflow": int(snapshot["overflow"]),
    }
    encode(begin)

    messages = [begin]
    batch: list[dict] = []
    start = 0

    def finish_batch() -> None:
        nonlocal batch, start
        if not batch:
            return
        message = {"t": "sync_cards", "tx": int(tx), "start": start, "notifs": batch}
        # encode() measures the actual prefixed UTF-8 line, including JSON
        # escaping. The singleton fallback below handles a card that exceeds
        # the soft chunk target while still respecting the hard line limit.
        encode(message)
        messages.append(message)
        start += len(batch)
        batch = []

    for card in cards:
        candidate = {"t": "sync_cards", "tx": int(tx), "start": start, "notifs": [*batch, card]}
        try:
            encoded_size = len(encode(candidate))
        except ValueError:
            encoded_size = LINE_MAX + 1

        if encoded_size <= CARD_CHUNK_MAX:
            batch.append(card)
            continue

        if batch:
            finish_batch()
            candidate = {"t": "sync_cards", "tx": int(tx), "start": start, "notifs": [card]}
            encoded_size = len(encode(candidate))

        if encoded_size > CARD_CHUNK_MAX:
            # Maximum notification strings normally stay below 2 KiB even
            # after escaping, but allow an unusually large valid card as its
            # own frame up to the device's hard limit.
            if encoded_size > LINE_MAX:
                raise ValueError("notification card exceeds the device line limit")
            messages.append(candidate)
            start += 1
        else:
            batch.append(card)

    finish_batch()
    commit = {"t": "sync_commit", "tx": int(tx)}
    encode(commit)
    messages.append(commit)
    return messages


def bar(zones: list[dict], rev: int) -> dict:
    return {"t": "bar", "rev": rev, "zones": zones}


def clock(epoch: int, offset: int) -> dict:
    return {"t": "clock", "epoch": int(epoch), "offset": int(offset)}


def media(state: str, title: str, artist: str, album: str, pos: float, length: float) -> dict:
    return {
        "t": "media",
        "state": state,
        "title": title,
        "artist": artist,
        "album": album,
        "pos": round(float(pos), 3),
        "len": round(float(length), 3),
    }


def notify(
    nid: int,
    app: str,
    summary: str,
    body: str,
    urgency: int,
    expire: int,
    ts: int,
    total: int | None = None,
    cached: bool | None = None,
) -> dict:
    message = {
        "t": "notify",
        "id": int(nid),
        "app": clip_utf8(display_text(app), 31),
        "summary": clip_utf8(display_text(summary), 63),
        "body": clip_utf8(display_text(body), 159),
        "urgency": int(urgency),
        "expire": int(expire),
        "ts": int(ts),
    }
    if total is not None:
        message["total"] = int(total)
    if cached is not None:
        message["cached"] = bool(cached)
    return message


def close(nid: int, total: int | None = None) -> dict:
    message = {"t": "close", "id": int(nid)}
    if total is not None:
        message["total"] = int(total)
    return message
