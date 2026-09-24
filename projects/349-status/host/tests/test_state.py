from status349.state import StateModel


def test_snapshot_shape():
    model = StateModel()
    snapshot = model.snapshot()
    assert snapshot["t"] == "sync"
    assert snapshot["clock"] is None
    assert snapshot["media"] is None
    assert snapshot["notifs"] == []
    assert snapshot["bar"]["zones"] == []


def test_change_detection_bumps_rev_once():
    model = StateModel()
    zones = [{"id": "clock", "kind": "clock"}]
    assert model.set_zones(zones) is True
    rev = model.rev
    assert model.set_zones(list(zones)) is False
    assert model.rev == rev

    assert model.set_clock(100, 3600) is True
    assert model.set_clock(100, 3600) is False
    assert model.rev == rev + 1

    assert model.set_media({"t": "media", "state": "playing"}) is True
    assert model.set_media({"t": "media", "state": "playing"}) is False
    assert model.rev == rev + 2


def test_snapshot_carries_current_state():
    model = StateModel()
    model.set_zones([{"id": "cpu"}])
    model.set_clock(5, -18000)
    snapshot = model.snapshot()
    assert snapshot["rev"] == model.rev
    assert snapshot["bar"]["zones"] == [{"id": "cpu"}]
    assert snapshot["clock"] == {"epoch": 5, "offset": -18000}


def test_notifications():
    model = StateModel()
    assert model.add_notification({"t": "notify", "id": 1, "summary": "hi"}) is True
    assert model.add_notification({"t": "notify", "id": 1, "summary": "hi"}) is False
    assert [n["id"] for n in model.snapshot()["notifs"]] == [1]
    assert model.close_notification(1) is True
    assert model.close_notification(1) is False
    assert model.snapshot()["notifs"] == []


def test_notifications_capped_in_sync():
    model = StateModel(max_visible=2)
    for nid in range(4):
        model.add_notification({"t": "notify", "id": nid})
    snapshot = model.snapshot()
    assert [n["id"] for n in snapshot["notifs"]] == [2, 3]
    assert snapshot["notifs_overflow"] == 2


def test_notifications_capped_to_zero():
    model = StateModel(max_visible=0)
    model.add_notification({"t": "notify", "id": 1})
    snapshot = model.snapshot()
    assert snapshot["notifs"] == []
    assert snapshot["notifs_overflow"] == 1
