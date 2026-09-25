"""The service must fail visibly and injected cards must expire."""

import asyncio
import time

import pytest

from status349.config import default_config
from status349.daemon import Daemon
from status349.link import LinkError


def test_failed_tick_exits_daemon_and_cleans_up():
    async def scenario():
        cfg = default_config()
        cfg.notifications.mode = "off"
        daemon = Daemon(cfg, asyncio.Event())
        cleaned = []

        async def ready():
            pass

        async def stop_notifications():
            cleaned.append("notifications")

        async def stop_ipc():
            cleaned.append("ipc")

        async def park():
            await asyncio.Future()

        async def fail():
            raise RuntimeError("tick failed")

        daemon.notifications.start = ready
        daemon.notifications.stop = stop_notifications
        daemon._ipc.start = ready
        daemon._ipc.stop = stop_ipc
        daemon._link_loop = park
        daemon._tick_loop = fail
        daemon._ping_loop = park

        with pytest.raises(RuntimeError, match="tick failed"):
            await asyncio.wait_for(daemon.run(), 1)
        assert cleaned == ["notifications", "ipc"]

    asyncio.run(scenario())


def test_failed_notification_worker_exits_daemon():
    async def scenario():
        daemon = Daemon(default_config(), asyncio.Event())

        async def ready():
            pass

        async def park():
            await asyncio.Future()

        async def fail_monitor():
            raise RuntimeError("monitor failed")

        daemon._ipc.start = ready
        daemon._ipc.stop = ready
        daemon._link_loop = park
        daemon._tick_loop = park
        daemon._ping_loop = park
        daemon.notifications._monitor_loop = fail_monitor
        daemon.notifications._process_loop = park
        daemon.notifications._send_loop = park

        with pytest.raises(RuntimeError, match="notification source stopped unexpectedly"):
            await asyncio.wait_for(daemon.run(), 1)
        assert isinstance(daemon.notifications.failure, RuntimeError)
        assert str(daemon.notifications.failure) == "monitor failed"

    asyncio.run(scenario())


def test_injected_notification_expires_and_sends_close():
    async def scenario():
        cfg = default_config()
        cfg.notifications.mode = "off"
        daemon = Daemon(cfg, asyncio.Event())
        sent = []

        async def capture(message):
            sent.append(message)
            return True

        daemon.send = capture
        daemon._sample = lambda: {}
        task = asyncio.create_task(daemon._tick_loop())
        try:
            reply = await daemon._ipc_handler({"cmd": "notify", "summary": "test", "expire": 20})
            nid = reply["id"]
            assert nid in daemon.model.notifs
            for _ in range(20):
                if nid not in daemon.model.notifs:
                    break
                await asyncio.sleep(0.02)
            assert nid not in daemon.model.notifs
            assert {"t": "close", "id": nid} in sent
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_missing_port_uses_fast_discovery_even_with_long_open_backoff(monkeypatch):
    async def scenario():
        cfg = default_config()
        cfg.link.reconnect_min_s = 5.0
        cfg.link.reconnect_max_s = 10.0
        stop = asyncio.Event()
        daemon = Daemon(cfg, stop)
        attempts = 0

        def missing_port():
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                stop.set()
            raise LinkError("absent")

        monkeypatch.setattr("status349.daemon.find_port", missing_port)
        started = time.monotonic()
        await asyncio.wait_for(daemon._link_loop(), 2)
        assert attempts == 2
        assert time.monotonic() - started < 1.5

    asyncio.run(scenario())
