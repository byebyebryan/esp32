"""Notification mirroring via D-Bus monitoring.

The desktop notification daemon keeps ownership of notifications; we only
eavesdrop. A monitor connection cannot send messages, so a second connection is
kept for `CloseNotification` propagation.

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
    f"type='error',sender='{NOTIFICATIONS_NAME}'",
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


def effective_popup_timeout_ms(expire: int, urgency: int, cfg: NotificationsConfig) -> int:
    """A desktop popup may time out without closing its history entry."""
    if expire >= 0:
        return expire
    return cfg.critical_popup_timeout_ms if urgency >= 2 else cfg.popup_timeout_ms


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
        self._outbox: dict[int, dict] = {}
        self._outbox_ready = asyncio.Event()
        self._monitor_task: asyncio.Task | None = None
        self._process_task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None

        self._next_id = 1
        self._by_serial: dict[tuple[str, int], tuple[int, int]] = {}
        self._daemon_to_local: dict[int, int] = {}
        self._local_to_daemon: dict[int, int | None] = {}
        self._mirrored_local_ids: set[int] = set()
        self._expiry_deadlines: dict[int, float] = {}
        self.failed = asyncio.Event()
        self.failure: BaseException | None = None

    def _watch_task(self, task: asyncio.Task) -> asyncio.Task:
        def on_done(done: asyncio.Task) -> None:
            if done.cancelled():
                return
            self.failure = done.exception() or RuntimeError(f"{done.get_name()} stopped unexpectedly")
            self.failed.set()

        task.add_done_callback(on_done)
        return task

    async def start(self) -> None:
        await self.reconfigure()

    async def reconfigure(self) -> None:
        if self.cfg.mode == "off":
            await self.stop()
            await self._close_mirrored_notifications()
            self._discard_messages()
            log.info("notifications disabled")
            return
        if self.cfg.mode != "mirror":
            log.warning("notification mode %r not implemented, staying off", self.cfg.mode)
            await self.stop()
            return
        if self._process_task is not None:
            return
        self._process_task = self._watch_task(asyncio.create_task(self._process_loop(), name="notifications"))
        self._send_task = self._watch_task(asyncio.create_task(self._send_loop(), name="notify-send"))
        self._monitor_task = self._watch_task(asyncio.create_task(self._monitor_loop(), name="notify-monitor"))

    async def stop(self) -> None:
        tasks = tuple(task for task in (self._monitor_task, self._process_task, self._send_task) if task is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._monitor_task = self._process_task = self._send_task = None
        await self._teardown()

    async def _close_mirrored_notifications(self) -> None:
        for local_id in tuple(self._mirrored_local_ids):
            self._forget_local(local_id)
            await self._on_close(local_id)

    def _forget_local(self, local_id: int) -> None:
        self._mirrored_local_ids.discard(local_id)
        self._expiry_deadlines.pop(local_id, None)
        self._outbox.pop(local_id, None)
        self._local_to_daemon.pop(local_id, None)
        for daemon_id, mapped_id in tuple(self._daemon_to_local.items()):
            if mapped_id == local_id:
                self._daemon_to_local.pop(daemon_id, None)
        for key, (pending_id, _requested_id) in tuple(self._by_serial.items()):
            if pending_id == local_id:
                self._by_serial.pop(key, None)

    async def _expire_due(self) -> None:
        now = time.monotonic()
        for local_id, deadline in tuple(self._expiry_deadlines.items()):
            if deadline <= now and self._expiry_deadlines.get(local_id) == deadline:
                self._forget_local(local_id)
                await self._on_close(local_id)

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
            # Once monitoring was interrupted, desktop IDs can no longer be
            # trusted. Remove the cards before accepting a new monitor session.
            await self._close_mirrored_notifications()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _setup(self) -> None:
        self._control = await MessageBus(bus_type=BusType.SESSION).connect()
        self._monitor = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
        reply = await self._monitor.call(
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

        self._monitor.add_message_handler(self._enqueue)
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
        self._outbox.clear()
        self._outbox_ready.clear()
        self._expiry_deadlines.clear()
        self._discard_messages()

    def _discard_messages(self) -> None:
        while not self._messages.empty():
            try:
                self._messages.get_nowait()
            except asyncio.QueueEmpty:
                break

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
            await self._expire_due()
            timeout = None
            if self._expiry_deadlines:
                timeout = max(0.0, min(self._expiry_deadlines.values()) - time.monotonic())
            try:
                message = await self._messages.get() if timeout is None else await asyncio.wait_for(
                    self._messages.get(), timeout=timeout
                )
            except asyncio.TimeoutError:
                continue
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
        elif message.message_type in {MessageType.METHOD_RETURN, MessageType.ERROR}:
            key = (message.destination, message.reply_serial) if message.destination and message.reply_serial else None
            pending = self._by_serial.pop(key, None) if key is not None else None
            if pending is None:
                return
            local_id, requested_id = pending
            if message.message_type == MessageType.ERROR:
                if requested_id and self._daemon_to_local.get(requested_id) == local_id:
                    self._daemon_to_local.pop(requested_id, None)
                if self._local_to_daemon.get(local_id) == requested_id:
                    self._local_to_daemon[local_id] = None
                return
            daemon_id = int(message.body[0]) if message.body and isinstance(message.body[0], int) else None
            if daemon_id is not None:
                if requested_id and requested_id != daemon_id and self._daemon_to_local.get(requested_id) == local_id:
                    self._daemon_to_local.pop(requested_id, None)
                self._daemon_to_local[daemon_id] = local_id
                self._local_to_daemon[local_id] = daemon_id
            elif requested_id and self._daemon_to_local.get(requested_id) == local_id:
                self._daemon_to_local.pop(requested_id, None)
                self._local_to_daemon[local_id] = None
        elif (
            message.message_type == MessageType.SIGNAL
            and message.interface == NOTIFICATIONS_NAME
            and message.member == "NotificationClosed"
        ):
            parsed = parse_closed_body(message.body)
            if parsed is not None:
                daemon_id, _reason = parsed
                local_id = self._daemon_to_local.get(daemon_id)
                if local_id is not None:
                    self._forget_local(local_id)
                    await self._on_close(local_id)

    async def _handle_notify(self, message: Message) -> None:
        if self.cfg.mode != "mirror":
            return
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
            self._local_to_daemon[local_id] = None
            if replaces:
                # Keep a provisional association until Notify returns. Some
                # notification daemons allocate a fresh ID for an unknown
                # replaces_id, and that returned ID is authoritative.
                self._daemon_to_local[replaces] = local_id
        self._mirrored_local_ids.add(local_id)

        if message.serial and message.sender:
            self._by_serial[(message.sender, message.serial)] = (local_id, replaces)

        timeout_ms = effective_popup_timeout_ms(parsed["expire"], parsed["urgency"], self.cfg)
        if timeout_ms > 0:
            self._expiry_deadlines[local_id] = time.monotonic() + timeout_ms / 1000.0
        else:
            self._expiry_deadlines.pop(local_id, None)

        # Coalesce queued replacements and let NotificationClosed cancel a
        # card that has not reached the serial link yet.
        self._outbox.pop(local_id, None)
        self._outbox[local_id] = proto.notify(
            local_id,
            parsed["app"],
            parsed["summary"],
            parsed["body"],
            parsed["urgency"],
            parsed["expire"],
            int(time.time()),
        )
        self._outbox_ready.set()

    async def _send_loop(self) -> None:
        interval = 1.0 / NOTIFY_RATE_PER_S
        while True:
            await self._outbox_ready.wait()
            self._outbox_ready.clear()
            while self._outbox:
                local_id = next(iter(self._outbox))
                message = self._outbox.pop(local_id)
                await self._on_notify(message)
                await asyncio.sleep(interval)
