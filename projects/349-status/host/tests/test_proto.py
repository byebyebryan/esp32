import json

import pytest

from status349 import proto


def test_encode_prefix_and_newline():
    assert proto.encode({"t": "ping", "ts": 1}) == b'@349 {"t":"ping","ts":1}\n'


def test_classify_data_line():
    assert proto.classify('@349 {"t":"pong","ts":1}') == (True, {"t": "pong", "ts": 1})


def test_classify_log_line():
    assert proto.classify("I (123) boot: hello") == (False, None)


def test_classify_malformed_json():
    assert proto.classify("@349 nope") == (True, None)


def test_classify_non_object():
    assert proto.classify("@349 [1,2]") == (True, None)


def test_bar_builder():
    assert proto.bar([{"id": "clock"}], 3) == {"t": "bar", "rev": 3, "zones": [{"id": "clock"}]}


def test_clock_builder():
    assert proto.clock(1.9, -3600.7) == {"t": "clock", "epoch": 1, "offset": -3600}


def test_media_builder():
    message = proto.media("playing", "title", "artist", "album", 1.23456, 180.0)
    assert message["t"] == "media"
    assert message["pos"] == 1.235
    assert message["len"] == 180.0


def test_notify_and_close_builders():
    message = proto.notify(7, "app", "sum", "body", 1, 5000, 42)
    assert message == {
        "t": "notify",
        "id": 7,
        "app": "app",
        "summary": "sum",
        "body": "body",
        "urgency": 1,
        "expire": 5000,
        "ts": 42,
    }
    assert proto.close(7) == {"t": "close", "id": 7}
    assert proto.notify(7, "app", "sum", "body", 1, 5000, 42, total=3)["total"] == 3
    assert proto.notify(7, "app", "sum", "body", 1, 5000, 42, total=3, cached=False)["cached"] is False
    assert proto.close(7, total=2) == {"t": "close", "id": 7, "total": 2}


def test_card_sync_capability_requires_an_advertised_integer_capacity():
    assert proto.card_sync_capacity({"cap": ["link", "card-sync-v1"], "cache_cards": 32}) == 32
    assert proto.card_sync_capacity({"cap": "card-sync-v1", "cache_cards": 4}) == 4
    assert proto.card_sync_capacity({"cap": ["link"], "cache_cards": 32}) is None
    assert proto.card_sync_capacity({"cap": ["card-sync-v1"], "cache_cards": True}) is None


def test_cards_status_requires_nonnegative_counts_and_integer_ids():
    assert proto.card_status({"count": 2, "overflow": 1, "ids": [8, 7], "capacity": 32}) == {
        "count": 2,
        "overflow": 1,
        "ids": [8, 7],
        "capacity": 32,
    }
    assert proto.card_status({"count": True, "overflow": 1, "ids": [], "capacity": 32}) is None
    assert proto.card_status({"count": 1, "overflow": -1, "ids": [1], "capacity": 32}) is None
    assert proto.card_status({"count": 1, "overflow": 0, "ids": ["1"], "capacity": 32}) is None
    assert proto.card_status({"count": 2, "overflow": 0, "ids": [1], "capacity": 32}) is None
    assert proto.card_status({"count": 2, "overflow": 0, "ids": [1, 2], "capacity": 1}) is None
    assert proto.card_status({"count": 2, "overflow": 0, "ids": [1, 1], "capacity": 32}) is None


def test_card_sync_chunks_measure_escaped_utf8_bytes():
    cards = [
        proto.notify(
            nid,
            "\0" * 31,
            "\0" * 63,
            "\0" * 100 + "東京" * 30,
            1,
            5000,
            42,
        )
        for nid in range(32)
    ]
    snapshot = {
        "rev": 9,
        "bar": {"t": "bar", "rev": 9, "zones": []},
        "clock": None,
        "media": None,
        "notifs": cards,
        "limit": 32,
        "overflow": 4,
    }

    messages = proto.card_sync_messages(snapshot, tx=7)
    chunks = [message for message in messages if message["t"] == "sync_cards"]

    assert messages[0] == {
        "t": "sync_begin",
        "tx": 7,
        "rev": 9,
        "bar": snapshot["bar"],
        "clock": None,
        "media": None,
        "limit": 32,
        "count": 32,
        "overflow": 4,
    }
    assert messages[-1] == {"t": "sync_commit", "tx": 7}
    assert [card for chunk in chunks for card in chunk["notifs"]] == cards
    assert [chunk["start"] for chunk in chunks] == [
        sum(len(previous["notifs"]) for previous in chunks[:index]) for index in range(len(chunks))
    ]
    assert all(len(proto.encode(message)) <= proto.LINE_MAX for message in messages)
    assert all(len(proto.encode(chunk)) <= proto.CARD_CHUNK_MAX for chunk in chunks)


@pytest.mark.parametrize(
    ("limit", "notifs", "overflow"),
    [
        (0, [], 3),
        (8, [{"t": "notify", "id": nid} for nid in range(8)], 4),
    ],
)
def test_sync_begin_encodes_effective_cache_limit(limit, notifs, overflow):
    snapshot = {
        "rev": 4,
        "bar": {"t": "bar", "rev": 4, "zones": []},
        "clock": None,
        "media": None,
        "notifs": notifs,
        "limit": limit,
        "overflow": overflow,
    }

    messages = proto.card_sync_messages(snapshot, tx=11)
    begin_line = proto.encode(messages[0])
    begin = json.loads(begin_line[len(proto.PREFIX):])

    assert begin["limit"] == limit
    assert begin["count"] == len(notifs)
    assert begin["overflow"] == overflow
    assert all(len(proto.encode(message)) <= proto.LINE_MAX for message in messages)
    assert all(
        len(proto.encode(message)) <= proto.CARD_CHUNK_MAX
        for message in messages
        if message["t"] == "sync_cards"
    )


def test_notify_uses_supported_glyphs_and_fits_device_buffers():
    message = proto.notify(1, "a" * 30 + "é", "s" * 62 + "é", "b" * 158 + "é", 1, 5000, 42)
    assert message["app"] == "a" * 30 + "e"
    assert message["summary"] == "s" * 62 + "e"
    assert message["body"] == "b" * 158 + "e"
    assert len(proto.encode(message)) < proto.LINE_MAX


def test_non_latin_text_reaches_device_font_fallback():
    assert proto.display_text("Café 東京 🔋 が") == "Cafe 東京 🔋 が"
    assert proto.display_text("Cafe\u0301") == "Cafe"
    assert proto.display_text("first\nsecond") == "first second"


def test_encode_rejects_a_line_over_device_limit():
    with pytest.raises(ValueError, match="device limit"):
        proto.encode({"t": "text", "v": "x" * proto.LINE_MAX})
