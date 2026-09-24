import asyncio
import os
import time

import pytest
from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

from status349.config import default_config
from status349.daemon import Daemon
from status349.fake import FakeDevice
from status349.sources.notifications import (
    NOTIFICATIONS_NAME,
    NOTIFICATIONS_PATH,
    NotificationSource,
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


def _notify_call(sender: str, serial: int, replaces_id: int, summary: str) -> Message:
    return Message(
        destination=NOTIFICATIONS_NAME,
        path=NOTIFICATIONS_PATH,
        interface=NOTIFICATIONS_NAME,
        member="Notify",
        signature="susssasa{sv}i",
        sender=sender,
        serial=serial,
        body=["test-app", replaces_id, "", summary, "body", [], {}, 5000],
    )


def _notify_reply(destination: str, serial: int, daemon_id: int) -> Message:
    return Message(
        message_type=MessageType.METHOD_RETURN,
        sender=NOTIFICATIONS_NAME,
        destination=destination,
        reply_serial=serial,
        signature="u",
        body=[daemon_id],
    )


def _closed_signal(daemon_id: int) -> Message:
    return Message(
        message_type=MessageType.SIGNAL,
        sender=NOTIFICATIONS_NAME,
        path=NOTIFICATIONS_PATH,
        interface=NOTIFICATIONS_NAME,
        member="NotificationClosed",
        signature="uu",
        body=[daemon_id, 2],
    )


def test_notify_reply_correlation_uses_client_sender_and_returned_id():
    async def scenario():
        closed = []

        async def noop(_message):
            pass

        async def on_close(local_id):
            closed.append(local_id)

        source = NotificationSource(default_config().notifications, noop, on_close)
        await source._handle_notify(_notify_call(":1.40", 7, 0, "first"))
        await source._handle_notify(_notify_call(":1.41", 7, 0, "second"))
        await source._handle(_notify_reply(":1.41", 7, 222))
        await source._handle(_notify_reply(":1.40", 7, 111))

        assert source._daemon_to_local == {111: 1, 222: 2}
        await source._handle(_closed_signal(222))
        assert closed == [2]
        assert 111 in source._daemon_to_local

    asyncio.run(scenario())


def test_unknown_replaces_id_is_replaced_by_notify_reply_id():
    async def scenario():
        closed = []

        async def noop(_message):
            pass

        async def on_close(local_id):
            closed.append(local_id)

        source = NotificationSource(default_config().notifications, noop, on_close)
        await source._handle_notify(_notify_call(":1.50", 9, 777, "replacement"))
        assert source._daemon_to_local == {777: 1}
        await source._handle(_notify_reply(":1.50", 9, 888))
        assert source._daemon_to_local == {888: 1}
        assert source._local_to_daemon == {1: 888}
        await source._handle(_closed_signal(777))
        assert closed == []
        await source._handle(_closed_signal(888))
        assert closed == [1]

    asyncio.run(scenario())


def test_late_notify_reply_after_close_cannot_restore_mapping():
    async def scenario():
        closed = []

        async def noop(_message):
            pass

        async def on_close(local_id):
            closed.append(local_id)

        source = NotificationSource(default_config().notifications, noop, on_close)
        await source._handle_notify(_notify_call(":1.55", 10, 777, "closes before reply"))
        await source._handle(_closed_signal(777))
        await source._handle(_notify_reply(":1.55", 10, 888))

        assert closed == [1]
        assert source._daemon_to_local == {}
        assert source._local_to_daemon == {}
        assert source._by_serial == {}
        assert source._outbox == {}

        # A later request can still establish a fresh mapping for the same
        # daemon ID after the closed request's late reply has been discarded.
        await source._handle_notify(_notify_call(":1.55", 11, 888, "new card"))
        await source._handle(_notify_reply(":1.55", 11, 888))
        assert source._daemon_to_local == {888: 2}

    asyncio.run(scenario())


def test_close_cancels_a_rate_limited_queued_notify_and_later_card_is_delivered():
    async def scenario():
        delivered = []
        closed = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        later_delivered = asyncio.Event()

        async def on_notify(message):
            delivered.append(message["id"])
            if message["id"] == 1:
                first_started.set()
                await release_first.wait()
            elif message["id"] == 3:
                later_delivered.set()

        async def on_close(local_id):
            closed.append(local_id)

        source = NotificationSource(default_config().notifications, on_notify, on_close)
        send_task = asyncio.create_task(source._send_loop())
        try:
            await source._handle_notify(_notify_call(":1.60", 1, 0, "first"))
            await asyncio.wait_for(first_started.wait(), 1)

            await source._handle_notify(_notify_call(":1.60", 2, 0, "fast close"))
            await source._handle(_notify_reply(":1.60", 2, 900))
            await source._handle(_closed_signal(900))
            assert closed == [2]

            release_first.set()
            await source._handle_notify(_notify_call(":1.61", 3, 0, "later valid card"))
            await asyncio.wait_for(later_delivered.wait(), 1)
            assert delivered == [1, 3]
        finally:
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


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
