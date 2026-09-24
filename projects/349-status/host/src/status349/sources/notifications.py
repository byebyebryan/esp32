"""Notification mirroring via D-Bus monitoring.

The desktop notification daemon keeps ownership of notifications; we only
eavesdrop. A monitor connection cannot send messages, so a second connection is
kept for `CloseNotification` propagation (and later MPRIS calls).

Two bus quirks drive the implementation:

- The assigned notification ID only exists in the unicast method reply to
  `Notify`, which is why the monitor rules include a `method_return` rule and we
  correlate `reply_serial` with the original call serial. The rule is narrowed
  to the notification daemon's sender to keep unrelated traffic out.
- dbus-broker disconnects monitors that do not support unix file descriptors
  when an FD-carrying message matches, so the monitor connection negotiates FD
  support and we close the descriptors we never use.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

from .. import proto
from ..config import NotificationsConfig

log = logging.getLogger(__name__)

NOTIFICATIONS_NAME = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"

MONITOR_RULES = [
    "interface='org.freedesktop.Notifications'",
    f"type='method_return',sender='{NOTIFICATIONS_NAME}'",
]

# One message per interval keeps a notification burst from overflowing the
# device's 4 KB RX ring.
NOTIFY_RATE_PER_S = 20.0


def is_ignored(app: str, ignore_apps: list[str]) -> bool:
    app = app.strip().lower()
    return any(app == ignored.strip().lower() for ignored in ignore_apps)


def parse_notify_body(body: list) -> dict | None:
    """Map a `Notify` call body to the fields we forward."""
    if len(body) < 8:
        return None
    app_name, replaces_id, _app_icon, summary, body_text, _actions, hints, expire_timeout = body[:8]

    urgency = 1
    if isinstance(hints, dict):
        variant = hints.get("urgency")
        if isinstance(variant, Variant) and isinstance(variant.value, int):
            urgency = int(variant.value)

    return {
        "app": str(app_name),
        "replaces": int(replaces_id),
        "summary": str(summary),
        "body": str(body_text),
        "urgency": urgency,
        "expire": int(expire_timeout),
    }


def parse_closed_body(body: list) -> tuple[int, int] | None:
    if len(body) < 2:
        return None
    return int(body[0]), int(body[1])


class NotificationSource:
    def __init__(
        self,
        cfg: NotificationsConfig,
        on_notify: Callable[[dict], Awaitable[None]],
        on_close: Callable[[int], Awaitable[None]],
    ):
        self.cfg = cfg
        self._on_notify = on_notify
        self._on_close = on_close

        self._monitor: MessageBus | None = None
        self._control: MessageBus | None = None
        self._messages: asyncio.Queue[Message] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict] = asyncio.Queue()
        self._monitor_task: asyncio.Task | None = None
        self._process_task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None

        self._next_id = 1
        self._by_serial: dict[int, int] = {}
        self._daemon_to_local: dict[int, int] = {}
        self._local_to_daemon: dict[int, int | None] = {}

    async def start(self) -> None:
        if self.cfg.mode == "off":
            log.info("notifications disabled")
            return
        if self.cfg.mode != "mirror":
            log.warning("notification mode %r not implemented, staying off", self.cfg.mode)
            return

        self._process_task = asyncio.create_task(self._process_loop(), name="notifications")
        self._send_task = asyncio.create_task(self._send_loop(), name="notify-send")
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="notify-monitor")

    async def stop(self) -> None:
        for task in (self._monitor_task, self._process_task, self._send_task):
            if task is not None:
                task.cancel()
        for task in (self._monitor_task, self._process_task, self._send_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._monitor_task = self._process_task = self._send_task = None
        await self._teardown()

    async def dismiss(self, local_id: int) -> None:
        daemon_id = self._local_to_daemon.get(local_id)
        if self.cfg.device_dismiss != "propagate":
            log.debug("dismiss %d is local-only", local_id)
            return
        if daemon_id is None or self._control is None:
            log.debug("dismiss %d has no daemon id to propagate", local_id)
            return
        try:
            await self._control.call(
                Message(
                    destination=NOTIFICATIONS_NAME,
                    path=NOTIFICATIONS_PATH,
                    interface=NOTIFICATIONS_NAME,
                    member="CloseNotification",
                    signature="u",
                    body=[daemon_id],
                )
            )
        except Exception as exc:
            log.warning("CloseNotification(%d) failed: %s", daemon_id, exc)

    async def _monitor_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._setup()
                backoff = 1.0
                await self._monitor.wait_for_disconnect()
                log.warning("notification monitor disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("notifications unavailable: %s", exc)
            await self._teardown()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _setup(self) -> None:
        control = await MessageBus(bus_type=BusType.SESSION).connect()
        monitor = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
        reply = await monitor.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus.Monitoring",
                member="BecomeMonitor",
                signature="asu",
                body=[MONITOR_RULES, 0],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"BecomeMonitor failed: {reply.error_name} {reply.body}")

        monitor.add_message_handler(self._enqueue)
        self._control = control
        self._monitor = monitor
        log.info("notification mirror active")

    async def _teardown(self) -> None:
        for bus in (self._monitor, self._control):
            if bus is not None:
                try:
                    bus.disconnect()
                except Exception:
                    pass
        self._monitor = self._control = None
        # Ids from a previous daemon session are meaningless now.
        self._by_serial.clear()
        self._daemon_to_local.clear()
        self._local_to_daemon.clear()

    def _enqueue(self, message: Message) -> bool:
        # Returning True marks the message handled: a monitor must not send
        # anything, and dbus-next would otherwise auto-reply UNKNOWN_METHOD to
        # every eavesdropped method call. Received FDs are never used.
        for fd in message.unix_fds or []:
            try:
                os.close(fd)
            except OSError:
                pass
        self._messages.put_nowait(message)
        return True

    async def _process_loop(self) -> None:
        while True:
            message = await self._messages.get()
            try:
                await self._handle(message)
            except Exception:
                log.exception("notification handling failed")

    async def _handle(self, message: Message) -> None:
        if (
            message.message_type == MessageType.METHOD_CALL
            and message.interface == NOTIFICATIONS_NAME
            and message.member == "Notify"
        ):
            await self._handle_notify(message)
        elif message.message_type == MessageType.METHOD_RETURN and message.reply_serial in self._by_serial:
            local_id = self._by_serial.pop(message.reply_serial)
            daemon_id = int(message.body[0]) if message.body and isinstance(message.body[0], int) else None
            if daemon_id is not None:
                self._daemon_to_local[daemon_id] = local_id
                self._local_to_daemon[local_id] = daemon_id
        elif (
            message.message_type == MessageType.SIGNAL
            and message.interface == NOTIFICATIONS_NAME
            and message.member == "NotificationClosed"
        ):
            parsed = parse_closed_body(message.body)
            if parsed is not None:
                daemon_id, _reason = parsed
                local_id = self._daemon_to_local.pop(daemon_id, None)
                if local_id is not None:
                    self._local_to_daemon.pop(local_id, None)
                    await self._on_close(local_id)

    async def _handle_notify(self, message: Message) -> None:
        parsed = parse_notify_body(message.body)
        if parsed is None:
            return
        if is_ignored(parsed["app"], self.cfg.ignore_apps):
            log.debug("ignoring notification from %r", parsed["app"])
            return

        replaces = parsed.pop("replaces")
        local_id = self._daemon_to_local.get(replaces) if replaces else None
        if local_id is None:
            local_id = self._next_id
            self._next_id += 1
            self._local_to_daemon[local_id] = replaces or None
            if replaces:
                # The daemon reuses the id it was given.
                self._daemon_to_local[replaces] = local_id
        elif self._local_to_daemon.get(local_id) is None:
            self._local_to_daemon[local_id] = replaces

        if message.serial and self._local_to_daemon.get(local_id) is None:
            self._by_serial[message.serial] = local_id

        await self._outbox.put(
            proto.notify(
                local_id,
                parsed["app"],
                parsed["summary"],
                parsed["body"],
                parsed["urgency"],
                parsed["expire"],
                int(time.time()),
            )
        )

    async def _send_loop(self) -> None:
        interval = 1.0 / NOTIFY_RATE_PER_S
        while True:
            message = await self._outbox.get()
            await self._on_notify(message)
            await asyncio.sleep(interval)
