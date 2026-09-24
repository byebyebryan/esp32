from status349.proto import classify, encode


def test_encode_prefix_and_newline():
    assert encode({"t": "ping", "ts": 1}) == b'@349 {"t":"ping","ts":1}\n'


def test_classify_data_line():
    assert classify('@349 {"t":"pong","ts":1}') == (True, {"t": "pong", "ts": 1})


def test_classify_log_line():
    assert classify("I (123) boot: hello") == (False, None)


def test_classify_malformed_json():
    assert classify("@349 nope") == (True, None)


def test_classify_non_object():
    assert classify("@349 [1,2]") == (True, None)
