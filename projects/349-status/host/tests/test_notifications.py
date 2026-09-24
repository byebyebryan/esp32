import asyncio
import os
import time

import pytest
from dbus_next import Message, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

from status349.config import default_config
from status349.daemon import Daemon
from status349.fake import FakeDevice
from status349.sources.notifications import (
    NOTIFICATIONS_NAME,
    NOTIFICATIONS_PATH,
    is_ignored,
    parse_closed_body,
    parse_notify_body,
)

HAVE_SESSION_BUS = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def test_parse_notify_body():
    body = ["app", 0, "", "sum", "body", [], {"urgency": Variant("y", 2)}, 5000]
    assert parse_notify_body(body) == {
        "app": "app",
        "replaces": 0,
        "summary": "sum",
        "body": "body",
        "urgency": 2,
        "expire": 5000,
    }


def test_parse_notify_body_short():
    assert parse_notify_body(["app"]) is None


def test_parse_notify_body_defaults_urgency():
    body = ["app", 0, "", "sum", "body", [], {}, -1]
    assert parse_notify_body(body)["urgency"] == 1


def test_parse_closed_body():
    assert parse_closed_body([7, 3]) == (7, 3)
    assert parse_closed_body([]) is None


def test_is_ignored():
    assert is_ignored("KeePassXC", ["keepassxc"])
    assert not is_ignored("Firefox", ["keepassxc"])


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _notify(summary: str) -> int:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        reply = await bus.call(
            Message(
                destination=NOTIFICATIONS_NAME,
                path=NOTIFICATIONS_PATH,
                interface=NOTIFICATIONS_NAME,
                member="Notify",
                signature="susssasa{sv}i",
                body=["349-test", 0, "", summary, "body", [], {}, 5000],
            )
        )
        return int(reply.body[0])
    finally:
        bus.disconnect()


async def _close(nid: int) -> None:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        await bus.call(
            Message(
                destination=NOTIFICATIONS_NAME,
                path=NOTIFICATIONS_PATH,
                interface=NOTIFICATIONS_NAME,
                member="CloseNotification",
                signature="u",
                body=[nid],
            )
        )
    finally:
        bus.disconnect()


@pytest.mark.skipif(not HAVE_SESSION_BUS, reason="no session bus")
def test_notification_mirror_and_desktop_close():
    fake = FakeDevice().start()
    try:
        cfg = default_config()
        cfg.link.port = fake.path
        cfg.daemon.tick_s = 0.05
        cfg.daemon.sync_interval_s = 0.5
        summary = f"349-mirror-{os.getpid()}-{int(time.time() * 1000)}"

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(Daemon(cfg, stop).run())
            daemon_id = None
            try:
                await _wait_for(lambda: any(m["t"] == "sync" for m in fake.received))
                daemon_id = await _notify(summary)
                await _wait_for(lambda: any(m["t"] == "notify" and m["summary"] == summary for m in fake.received))

                await _close(daemon_id)
                daemon_id = None
                await _wait_for(lambda: any(m["t"] == "close" for m in fake.received))
            finally:
                if daemon_id is not None:
                    await _close(daemon_id)
                stop.set()
                await asyncio.wait_for(task, 5)

        asyncio.run(scenario())
    finally:
        fake.stop()


@pytest.mark.skipif(not HAVE_SESSION_BUS, reason="no session bus")
def test_device_dismiss_propagates():
    fake = FakeDevice().start()
    try:
        cfg = default_config()
        cfg.link.port = fake.path
        cfg.daemon.tick_s = 0.05
        cfg.daemon.sync_interval_s = 0.5
        cfg.notifications.device_dismiss = "propagate"
        summary = f"349-propagate-{os.getpid()}-{int(time.time() * 1000)}"

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(Daemon(cfg, stop).run())
            daemon_id = None
            try:
                await _wait_for(lambda: any(m["t"] == "sync" for m in fake.received))
                daemon_id = await _notify(summary)
                await _wait_for(lambda: any(m["t"] == "notify" and m["summary"] == summary for m in fake.received))
                local_id = next(m["id"] for m in fake.received if m["t"] == "notify" and m["summary"] == summary)

                fake.send({"t": "input", "action": "dismiss", "id": local_id})
                await _wait_for(lambda: any(m["t"] == "close" and m["id"] == local_id for m in fake.received))
                daemon_id = None  # the daemon closed it on the desktop
            finally:
                if daemon_id is not None:
                    await _close(daemon_id)
                stop.set()
                await asyncio.wait_for(task, 5)

        asyncio.run(scenario())
    finally:
        fake.stop()
