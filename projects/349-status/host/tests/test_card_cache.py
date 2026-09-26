"""Host-side active-card transfer, ordering, refill, and readback tests."""

import asyncio
import json
from types import SimpleNamespace

from status349 import proto
from status349.config import default_config
from status349.daemon import Daemon


def _card(nid: int) -> dict:
    return proto.notify(nid, "test", f"card {nid}", "body", 1, 5000, nid)


def test_hello_capability_selects_chunked_transfer_and_old_hello_keeps_legacy_sync():
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        sent = []

        async def capture(message):
            sent.append(message)
            return True

        daemon._write_message = capture
        for nid in range(35):
            daemon.model.add_notification(_card(nid))

        hello = {"t": "hello", "proto": 1, "cap": ["link", "bar", "card-sync-v1"], "cache_cards": 32}
        await daemon._on_line("@349 " + json.dumps(hello))
        assert sent[0]["t"] == "sync_begin"
        assert sent[0]["count"] == 32
        assert sent[0]["overflow"] == 3
        assert sent[-1] == {"t": "sync_commit", "tx": 1}
        assert [card["id"] for message in sent if message["t"] == "sync_cards" for card in message["notifs"]] == list(range(3, 35))

        sent.clear()
        old_hello = {"t": "hello", "proto": 1, "cap": ["link", "bar"]}
        await daemon._on_line("@349 " + json.dumps(old_hello))
        await daemon._on_line("@349 " + json.dumps(old_hello))
        assert [message["t"] for message in sent] == ["sync", "sync"]
        assert [message["id"] for message in sent[0]["notifs"]] == list(range(32, 35))

    asyncio.run(scenario())


def test_hello_deduplicates_same_boot_and_syncs_after_firmware_restart():
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        sent = []

        async def capture(message):
            sent.append(message)
            return True

        daemon._write_message = capture
        hello = {
            "t": "hello",
            "proto": 1,
            "cap": ["link", "bar", "card-sync-v1"],
            "cache_cards": 32,
            "boot_id": 1042,
        }

        await daemon._on_line("@349 " + json.dumps(hello))
        await daemon._on_line("@349 " + json.dumps(hello))
        assert [message["tx"] for message in sent if message["t"] == "sync_begin"] == [1]
        assert [message["tx"] for message in sent if message["t"] == "sync_commit"] == [1]

        restarted = {**hello, "boot_id": 1043}
        await daemon._on_line("@349 " + json.dumps(restarted))
        assert [message["tx"] for message in sent if message["t"] == "sync_begin"] == [1, 2]
        assert [message["tx"] for message in sent if message["t"] == "sync_commit"] == [1, 2]

    asyncio.run(scenario())


def test_new_serial_connection_forgets_previous_boot_id_and_tx(monkeypatch):
    class Writer:
        def __init__(self):
            self.transport = SimpleNamespace(serial=None)
            self.closed = False
            self.payloads = []

        def write(self, payload):
            self.payloads.append(payload)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    async def scenario():
        cfg = default_config()
        cfg.link.port = "/dev/fake-349"
        stop = asyncio.Event()
        daemon = Daemon(cfg, stop)
        daemon._device_boot_id = 1042
        daemon._sync_tx = 99
        daemon._card_sync_capacity = 32
        writer = Writer()

        async def open_connection(**_kwargs):
            return object(), writer

        async def read_connection(_reader):
            assert daemon._device_boot_id is None
            assert daemon._sync_tx == 0
            assert daemon._card_sync_capacity is None
            stop.set()

        monkeypatch.setattr("status349.daemon.serial_asyncio.open_serial_connection", open_connection)
        daemon._read_loop = read_connection
        await daemon._link_loop()

        assert writer.closed
        assert daemon._device_boot_id is None

    asyncio.run(scenario())


def test_state_changes_wait_for_snapshot_commit_and_keep_arrival_close_order():
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        daemon._card_sync_capacity = 32
        daemon.model.add_notification(_card(1))
        entered_begin = asyncio.Event()
        release_begin = asyncio.Event()
        sent = []

        async def capture(message):
            sent.append(message)
            if message["t"] == "sync_begin":
                entered_begin.set()
                await release_begin.wait()
            return True

        daemon._write_message = capture
        sync = asyncio.create_task(daemon._send_sync())
        await asyncio.wait_for(entered_begin.wait(), 1)

        arrival = asyncio.create_task(daemon._device_notify(_card(2)))
        await asyncio.sleep(0)
        close = asyncio.create_task(daemon._device_close(1))
        await asyncio.sleep(0)
        assert set(daemon.model.notifs) == {1}

        release_begin.set()
        await asyncio.gather(sync, arrival, close)

        commit_index = next(index for index, message in enumerate(sent) if message["t"] == "sync_commit")
        assert [message["t"] for message in sent[: commit_index + 1]][0] == "sync_begin"
        assert all(message["t"] in {"sync_begin", "sync_cards", "sync_commit"} for message in sent[: commit_index + 1])
        assert sent[commit_index + 1 :] == [
            {**_card(2), "total": 2, "cached": True},
            {"t": "close", "id": 1, "total": 1},
        ]
        assert set(daemon.model.notifs) == {2}

    asyncio.run(scenario())


def test_replacing_an_omitted_old_card_does_not_enter_the_device_cache():
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        daemon._card_sync_capacity = 32
        for nid in range(33):
            daemon.model.add_notification(_card(nid))

        sent = []

        async def capture(message):
            sent.append(message)
            return True

        daemon.send = capture
        replacement = {**_card(0), "summary": "updated oldest"}
        await daemon._device_notify(replacement)

        assert sent == [{**replacement, "total": 33, "cached": False}]
        assert [message["id"] for message in daemon.model.card_snapshot(32)["notifs"]] == list(range(1, 33))

    asyncio.run(scenario())


def test_overflow_close_coalesces_a_full_refill_sync():
    async def scenario():
        cfg = default_config()
        cfg.daemon.tick_s = 0.05
        cfg.daemon.sync_interval_s = 30.0
        daemon = Daemon(cfg, asyncio.Event())
        daemon._card_sync_capacity = 32
        for nid in range(33):
            daemon.model.add_notification(_card(nid))

        sent = []
        refill_committed = asyncio.Event()

        async def capture(message):
            sent.append(message)
            if message["t"] == "sync_commit":
                refill_committed.set()
            return True

        daemon._write_message = capture
        await daemon._device_close(32)
        await daemon._device_close(31)
        assert sent == [
            {"t": "close", "id": 32, "total": 32},
            {"t": "close", "id": 31, "total": 31},
        ]
        assert daemon._needs_sync

        tick = asyncio.create_task(daemon._tick_loop())
        try:
            await asyncio.wait_for(refill_committed.wait(), 1)
        finally:
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)

        begins = [message for message in sent if message["t"] == "sync_begin"]
        assert len(begins) == 1
        cards = [card for message in sent if message["t"] == "sync_cards" for card in message["notifs"]]
        assert [card["id"] for card in cards] == list(range(31))
        assert not daemon._needs_sync

    asyncio.run(scenario())


def test_device_cards_ipc_returns_readback_ids_and_times_out_cleanly(monkeypatch):
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        daemon._card_sync_capacity = 32
        sent = []
        status = {"t": "cards_status", "count": 3, "overflow": 4, "ids": [9, 8, 7], "capacity": 32}

        async def reply_to_query(message):
            sent.append(message)
            if message["t"] == "cards_query":
                await daemon._on_line("@349 " + json.dumps(status))
            return True

        daemon.send = reply_to_query
        result = await daemon._ipc_handler({"cmd": "device_cards"})
        assert sent == [{"t": "cards_query"}]
        assert result == {
            "ok": True,
            "device_cards": {"count": 3, "overflow": 4, "ids": [9, 8, 7], "capacity": 32},
        }
        assert daemon._cards_status_waiter is None

        async def no_reply(_message):
            return True

        daemon.send = no_reply
        monkeypatch.setattr("status349.daemon.CARD_STATUS_TIMEOUT_S", 0.01)
        timeout = await daemon._ipc_handler({"cmd": "device_cards"})
        assert timeout == {"ok": False, "error": "timed out waiting for cards_status"}
        assert daemon._cards_status_waiter is None

    asyncio.run(scenario())
