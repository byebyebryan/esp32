"""End-to-end host test: daemon against the pty fake device."""

import asyncio
import time

from status349.config import default_config
from status349.daemon import Daemon, PING_INTERVAL_S
from status349.fake import FakeDevice


def test_daemon_talks_to_fake_device():
    fake = FakeDevice().start()
    try:
        cfg = default_config()
        cfg.link.port = fake.path
        cfg.daemon.tick_s = 0.05
        cfg.daemon.sync_interval_s = 0.3

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(Daemon(cfg, stop).run())
            await asyncio.sleep(1.2)
            stop.set()
            await asyncio.wait_for(task, 5.0)

        asyncio.run(scenario())

        types = [message["t"] for message in fake.received]
        assert "hello" in types
        assert "clock" in types
        assert "bar" in types
        assert "sync" in types
        assert "ping" in types

        sync = next(message for message in fake.received if message["t"] == "sync")
        assert sync["bar"]["zones"], "sync must carry the composed bar"
        assert sync["clock"] is not None
    finally:
        fake.stop()


def test_ping_loop_sends_periodically_only_while_connected(monkeypatch):
    monkeypatch.setattr("status349.daemon.PING_INTERVAL_S", 0.02)

    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        daemon._writer = object()
        sent = []

        async def capture(message):
            sent.append((time.monotonic(), message))
            return True

        daemon.send = capture
        task = asyncio.create_task(daemon._ping_loop())
        try:
            await asyncio.sleep(0.085)
            connected_count = len(sent)
            daemon._writer = None
            await asyncio.sleep(0.06)
            assert connected_count >= 3
            assert len(sent) == connected_count
            assert all(message["t"] == "ping" for _when, message in sent)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert PING_INTERVAL_S <= 5.0
    asyncio.run(scenario())


def test_unchanged_live_replacement_still_reaches_device():
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())
        sent = []

        async def capture(message):
            sent.append(message)
            return True

        daemon.send = capture
        message = {"t": "notify", "id": 1, "summary": "same"}
        await daemon._device_notify(message)
        await daemon._device_notify(message)
        assert sent == [message, message]
        assert daemon.model.rev == 1

    asyncio.run(scenario())
