"""End-to-end host test: daemon against the pty fake device."""

import asyncio

from status349.config import default_config
from status349.daemon import Daemon
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

        sync = next(message for message in fake.received if message["t"] == "sync")
        assert sync["bar"]["zones"], "sync must carry the composed bar"
        assert sync["clock"] is not None
    finally:
        fake.stop()
