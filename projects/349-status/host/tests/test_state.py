import pytest

from status349 import proto
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


def test_close_removes_a_card_and_a_later_new_id_can_appear():
    model = StateModel()
    assert model.add_notification({"t": "notify", "id": 1, "summary": "old"})
    assert model.close_notification(1)
    assert model.add_notification({"t": "notify", "id": 2, "summary": "new"})
    assert [notice["id"] for notice in model.snapshot()["notifs"]] == [2]


def test_snapshot_stays_within_device_line_limit_for_escaped_notification_text():
    model = StateModel(max_visible=8)
    for nid in range(8):
        model.add_notification({"t": "notify", "id": nid, "body": "\0" * 159})
    snapshot = model.snapshot()
    assert len(proto.encode(snapshot)) <= proto.LINE_MAX
    assert snapshot["notifs_overflow"] == 8 - len(snapshot["notifs"])


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


@pytest.mark.parametrize(
    ("active", "expected_ids", "overflow"),
    [
        (0, [], 0),
        (1, [0], 0),
        (20, list(range(20)), 0),
        (32, list(range(32)), 0),
        (35, list(range(3, 35)), 3),
    ],
)
def test_card_snapshot_selects_newest_cache_and_reports_overflow(active, expected_ids, overflow):
    model = StateModel(cache_limit=32)
    for nid in range(active):
        model.add_notification({"t": "notify", "id": nid})

    snapshot = model.card_snapshot(device_capacity=32)

    assert [message["id"] for message in snapshot["notifs"]] == expected_ids
    assert snapshot["limit"] == 32
    assert snapshot["overflow"] == overflow


def test_card_snapshot_respects_smaller_device_capacity_and_zero_host_limit():
    model = StateModel(cache_limit=20)
    for nid in range(25):
        model.add_notification({"t": "notify", "id": nid})
    limited = model.card_snapshot(4)
    assert [message["id"] for message in limited["notifs"]] == [21, 22, 23, 24]
    assert limited["limit"] == 4

    model.cache_limit = 0
    snapshot = model.card_snapshot(32)
    assert snapshot["notifs"] == []
    assert snapshot["limit"] == 0
    assert snapshot["overflow"] == 25


@pytest.mark.parametrize(
    ("cache_limit", "active", "expected_ids", "overflow"),
    [
        (0, 3, [], 3),
        (8, 12, list(range(4, 12)), 4),
    ],
)
def test_card_snapshot_exposes_configured_limit(cache_limit, active, expected_ids, overflow):
    model = StateModel(cache_limit=cache_limit)
    for nid in range(active):
        model.add_notification({"t": "notify", "id": nid})

    snapshot = model.card_snapshot(device_capacity=32)

    assert snapshot["limit"] == cache_limit
    assert [message["id"] for message in snapshot["notifs"]] == expected_ids
    assert snapshot["overflow"] == overflow
