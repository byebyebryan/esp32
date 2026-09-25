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
