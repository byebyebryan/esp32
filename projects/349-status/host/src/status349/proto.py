"""349-status wire protocol: line-delimited JSON behind a magic prefix.

The device echoes console output on the same port, so every data line carries
the prefix and everything else is ignored.
"""

from __future__ import annotations

import json

PREFIX = "@349 "
PROTO_VERSION = 1
LINE_MAX = 8192


def clip_utf8(value: str, max_bytes: int) -> str:
    """Fit a device string buffer without splitting a UTF-8 code point."""
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def encode(obj: dict) -> bytes:
    return (PREFIX + json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


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


def notify(nid: int, app: str, summary: str, body: str, urgency: int, expire: int, ts: int) -> dict:
    return {
        "t": "notify",
        "id": int(nid),
        "app": clip_utf8(app, 31),
        "summary": clip_utf8(summary, 63),
        "body": clip_utf8(body, 159),
        "urgency": int(urgency),
        "expire": int(expire),
        "ts": int(ts),
    }


def close(nid: int) -> dict:
    return {"t": "close", "id": int(nid)}
