"""349d - host daemon: sources -> state model -> USB-Serial-JTAG.

The device never polls. The daemon pushes a full `sync` on connect and every
`sync_interval_s`, and deltas (`clock` on offset change, `bar` on change)
otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from collections import deque

import serial_asyncio

from . import proto
from .composition import build_zones
from .config import Config, apply_config, load_config, validate_config
from .ipc import IpcServer, pause_path
from .link import LinkError, find_port, reset_to_normal_boot_async
from .sources.clock import ClockSource
from .sources.notifications import NotificationSource
from .sources.power import PowerSource
from .sources.sysinfo import SysinfoSource
from .sources.volume import VolumeSource
from .state import StateModel

log = logging.getLogger("349d")

BAUDRATE = 115200
PING_INTERVAL_S = 4.0
PORT_SCAN_INTERVAL_S = 0.5
CARD_STATUS_TIMEOUT_S = 2.0


class Daemon:
    def __init__(
        self, cfg: Config, stop: asyncio.Event, cfg_path: str | None = None, port_override: str | None = None
    ):
        validate_config(cfg)
        self.cfg = cfg
        self.cfg_path = cfg_path
        self._port_override = port_override
        if port_override is not None:
            self.cfg.link.port = port_override
        self.stop = stop
        self.model = StateModel(max_visible=cfg.notifications.max_visible, cache_limit=cfg.notifications.cache_limit)
        self.clock = ClockSource()
        self.sysinfo = SysinfoSource()
        self.volume = VolumeSource()
        self.power = PowerSource()
        self.notifications = NotificationSource(cfg.notifications, self._device_notify, self._device_close)
        self._writer: asyncio.StreamWriter | None = None
        self._state_lock = asyncio.Lock()
        self._wire_lock = asyncio.Lock()
        self._card_sync_capacity: int | None = None
        self._sync_tx = 0
        self._device_boot_id: int | None = None
        self._cards_query_lock = asyncio.Lock()
        self._cards_status_waiter: asyncio.Future[dict] | None = None
        self._devlog: deque[str] = deque(maxlen=200)
        self._last_rx_mono: float | None = None
        self._needs_sync = False
        self._tick_wakeup = asyncio.Event()
        self._next_notify_id = 100000
        self._injected_expiry: dict[int, float] = {}
        self._ipc = IpcServer(self._ipc_handler)

    async def run(self) -> None:
        try:
            await self.notifications.start()
            await self._ipc.start()
            workers = {
                asyncio.create_task(self._link_loop(), name="link"),
                asyncio.create_task(self._tick_loop(), name="tick"),
                asyncio.create_task(self._ping_loop(), name="ping"),
            }
            stop_waiter = asyncio.create_task(self.stop.wait(), name="stop")
            notification_failure = asyncio.create_task(self.notifications.failed.wait(), name="notification-failure")
            try:
                done, _ = await asyncio.wait(
                    workers | {stop_waiter, notification_failure}, return_when=asyncio.FIRST_COMPLETED
                )
                for worker in workers & done:
                    worker.result()  # Re-raise a failed worker's exception.
                    raise RuntimeError(f"{worker.get_name()} stopped unexpectedly")
                if notification_failure in done:
                    raise RuntimeError("notification source stopped unexpectedly") from self.notifications.failure
            finally:
                for task in workers | {stop_waiter, notification_failure}:
                    task.cancel()
                await asyncio.gather(*workers, stop_waiter, notification_failure, return_exceptions=True)
        finally:
            if self._writer is not None:
                self._writer.close()
            await self.notifications.stop()
            await self._ipc.stop()

    async def _device_notify(self, message: dict) -> None:
        async with self._state_lock:
            self.model.add_notification(message)
            # A live replacement must reach the device even when the payload
            # is unchanged: it unhides a card that was dismissed locally.
            update = dict(message)
            update["total"] = len(self.model.notifs)
            if self._card_sync_capacity is not None:
                update["cached"] = int(message["id"]) in self.model.cached_notification_ids(
                    self._card_sync_capacity
                )
            await self.send(update)

    async def _device_close(self, local_id: int) -> None:
        async with self._state_lock:
            cached_ids = self.model.cached_notification_ids(self._card_sync_capacity)
            overflowed = len(self.model.notifs) > len(cached_ids)
            if self.model.close_notification(local_id):
                await self.send(proto.close(local_id, total=len(self.model.notifs)))
                if self._card_sync_capacity is not None and overflowed and local_id in cached_ids:
                    # Closing a cached card exposes a vacancy when older active
                    # cards were omitted. The tick loop coalesces close bursts.
                    self._needs_sync = True
                    self._tick_wakeup.set()

    async def send(self, message: dict) -> bool:
        async with self._wire_lock:
            return await self._write_message(message)

    async def _write_message(self, message: dict) -> bool:
        if self._writer is None:
            return False
        try:
            payload = proto.encode(message)
        except ValueError as exc:
            log.error("refusing invalid protocol message: %s", exc)
            return False
        try:
            self._writer.write(payload)
            await self._writer.drain()
            return True
        except (ConnectionError, OSError) as exc:
            log.debug("send failed: %s", exc)
            return False

    def _sample(self) -> dict:
        values: dict = {}
        values.update(self.sysinfo.read())
        values.update(self.volume.read())
        values.update(self.power.read())
        return values

    async def _send_sync(self) -> None:
        async with self._state_lock:
            self._needs_sync = False
            await self._send_sync_locked()

    async def _send_sync_locked(self) -> None:
        epoch, offset = self.clock.read()
        self.model.set_clock(epoch, offset)
        self.model.set_zones(build_zones(self.cfg.bar.preset, self._sample()))
        if self._card_sync_capacity is None:
            messages = [self.model.snapshot()]
        else:
            self._sync_tx += 1
            snapshot = self.model.card_snapshot(self._card_sync_capacity)
            messages = proto.card_sync_messages(snapshot, self._sync_tx)

        # Hold the wire lock across the full transaction, including begin and
        # commit, so pings and IPC output cannot split its staging sequence.
        async with self._wire_lock:
            for message in messages:
                if not await self._write_message(message):
                    break

    async def _link_loop(self) -> None:
        min_backoff = max(0.05, float(self.cfg.link.reconnect_min_s))
        max_backoff = max(min_backoff, float(self.cfg.link.reconnect_max_s))
        backoff = min_backoff

        while not self.stop.is_set():
            if pause_path().exists():
                if self._writer is not None:
                    self._writer.close()
                backoff = min_backoff
                await asyncio.sleep(0.5)
                continue

            path = self.cfg.link.port
            if path is None:
                try:
                    path = find_port()
                except LinkError as exc:
                    log.debug("no device: %s", exc)
                    # Device discovery is cheap; do not let open-error backoff
                    # delay a replug after the by-id path reappears.
                    backoff = min_backoff
                    await asyncio.sleep(PORT_SCAN_INTERVAL_S)
                    continue

            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=path, baudrate=BAUDRATE)
            except Exception as exc:  # serial.SerialException and friends
                log.warning("open %s failed: %s", path, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            log.info("link up on %s", path)
            backoff = min_backoff
            async with self._state_lock:
                self._writer = writer
                self._card_sync_capacity = None
                self._sync_tx = 0
                self._device_boot_id = None
            # Opening the port resets the chip, but the kernel's DTR/RTS raise
            # can land it in download mode; force a normal boot, then the
            # device announces itself.
            serial_port = getattr(writer.transport, "serial", None)
            if serial_port is not None:
                await reset_to_normal_boot_async(serial_port)
            await self.send(proto.hello())
            await self.send({"t": "ping", "ts": int(time.time())})

            try:
                await self._read_loop(reader)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("link error: %s", exc)
            finally:
                self._writer = None
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

            if not self.stop.is_set():
                log.info("link down, retrying")
                await asyncio.sleep(backoff)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buf = b""
        async for chunk in reader:
            buf += chunk
            if len(buf) > proto.LINE_MAX * 2:
                log.warning("dropping oversized line buffer")
                buf = b""
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                await self._on_line(raw.decode("utf-8", "replace").rstrip("\r"))
        if buf:
            await self._on_line(buf.decode("utf-8", "replace"))

    async def _on_line(self, line: str) -> None:
        self._last_rx_mono = time.monotonic()
        is_data, message = proto.classify(line)
        if not is_data:
            if line:
                self._devlog.append(line)
                log.debug("dev: %s", line)
            return
        if message is None:
            log.warning("malformed data line: %.80s", line)
            return

        kind = message.get("t")
        if kind == "hello":
            boot_id = message.get("boot_id")
            if isinstance(boot_id, bool) or not isinstance(boot_id, int):
                boot_id = None
            log.info(
                "device hello: fw=%s build=%s sha=%s proto=%s cap=%s",
                message.get("fw"),
                message.get("build"),
                message.get("build_sha"),
                message.get("proto"),
                message.get("cap"),
            )
            async with self._state_lock:
                self._card_sync_capacity = proto.card_sync_capacity(message)
                if boot_id is not None and boot_id == self._device_boot_id:
                    log.debug("ignoring repeated hello for device boot_id=%s", boot_id)
                else:
                    # A missing ID keeps the pre-dedup legacy behavior: every
                    # hello requests a fresh full sync.
                    self._device_boot_id = boot_id
                    await self._send_sync_locked()
        elif kind == "input":
            await self._handle_input(message)
        elif kind == "resync":
            log.info("device requested resync (%s)", message.get("reason"))
            await self._send_sync()
        elif kind == "ack":
            log.debug("device ack: %s", message.get("v"))
        elif kind == "cards_status":
            status = proto.card_status(message)
            if status is None:
                log.warning("malformed cards_status response")
                return
            waiter = self._cards_status_waiter
            if waiter is not None and not waiter.done():
                waiter.set_result(status)
            else:
                log.debug("unsolicited cards_status: %s", status)
        else:
            log.debug("unknown device message: %s", message)

    async def _handle_input(self, message: dict) -> None:
        action = message.get("action")
        if action == "dismiss" and "id" in message:
            await self.notifications.dismiss(int(message["id"]))
        else:
            log.info("device input: %s", message)

    async def _query_device_cards(self) -> dict:
        async with self._cards_query_lock:
            if self._card_sync_capacity is None:
                return {"ok": False, "error": "device does not advertise card-sync-v1"}

            waiter: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            self._cards_status_waiter = waiter
            try:
                if not await self.send({"t": "cards_query"}):
                    return {"ok": False, "error": "device is disconnected"}
                try:
                    status = await asyncio.wait_for(waiter, timeout=CARD_STATUS_TIMEOUT_S)
                except asyncio.TimeoutError:
                    return {"ok": False, "error": "timed out waiting for cards_status"}
                return {"ok": True, "device_cards": status}
            finally:
                if self._cards_status_waiter is waiter:
                    self._cards_status_waiter = None

    async def reload(self) -> bool:
        if self.cfg_path is None:
            log.info("no config file to reload")
            return False
        try:
            new = load_config(self.cfg_path)
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the daemon
            log.error("config reload failed: %s", exc)
            return False
        async with self._state_lock:
            apply_config(self.cfg, new)
            if self._port_override is not None:
                self.cfg.link.port = self._port_override
            self.model.max_visible = self.cfg.notifications.max_visible
            self.model.cache_limit = self.cfg.notifications.cache_limit
            self._needs_sync = True
            self._tick_wakeup.set()
        await self.notifications.reconfigure()
        log.info("config reloaded from %s", self.cfg_path)
        return True

    def pause(self) -> None:
        pause_path().touch()
        if self._writer is not None:
            self._writer.close()
        log.info("paused: serial port released for flashing")

    def resume(self) -> None:
        try:
            pause_path().unlink()
        except FileNotFoundError:
            pass
        log.info("resumed")

    def _status(self) -> dict:
        age = None if self._last_rx_mono is None else round(time.monotonic() - self._last_rx_mono, 1)
        return {
            "ok": True,
            "paused": pause_path().exists(),
            "link": self._writer is not None,
            "port": self.cfg.link.port,
            "rev": self.model.rev,
            "notifs": len(self.model.notifs),
            "last_rx_s": age,
            "config": self.cfg_path,
        }

    async def _ipc_handler(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "status":
            return self._status()
        if cmd == "device_cards":
            return await self._query_device_cards()
        if cmd == "text":
            await self.send({"t": "text", "v": str(request.get("value", ""))})
            return {"ok": True}
        if cmd == "notify":
            nid = self._next_notify_id
            self._next_notify_id += 1
            message = proto.notify(
                nid,
                str(request.get("app", "349ctl")),
                str(request.get("summary", "")),
                str(request.get("body", "")),
                int(request.get("urgency", 1)),
                int(request.get("expire", 5000)),
                int(time.time()),
            )
            await self._device_notify(message)
            if message["expire"] > 0:
                self._injected_expiry[nid] = time.monotonic() + message["expire"] / 1000.0
                self._tick_wakeup.set()
            return {"ok": True, "id": nid}
        if cmd == "pause":
            self.pause()
            return {"ok": True}
        if cmd == "resume":
            self.resume()
            return {"ok": True}
        if cmd == "reload":
            return {"ok": await self.reload()}
        if cmd == "log":
            count = max(1, min(200, int(request.get("lines", 50))))
            return {"ok": True, "lines": list(self._devlog)[-count:]}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    async def _tick_loop(self) -> None:
        last_offset: int | None = None
        interval_settings: tuple[float, float] | None = None
        next_sync = time.monotonic()

        while True:
            tick = float(self.cfg.daemon.tick_s)
            sync_interval = float(self.cfg.daemon.sync_interval_s)
            settings = (tick, sync_interval)
            if settings != interval_settings:
                next_sync = time.monotonic() + sync_interval
                interval_settings = settings

            wait_s = tick
            if self._injected_expiry:
                wait_s = min(wait_s, max(0.0, min(self._injected_expiry.values()) - time.monotonic()))
            try:
                await asyncio.wait_for(self._tick_wakeup.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass
            else:
                self._tick_wakeup.clear()

            # A reload may have changed both intervals while this wait was
            # active. Reset the sync deadline before processing this tick.
            tick = float(self.cfg.daemon.tick_s)
            sync_interval = float(self.cfg.daemon.sync_interval_s)
            settings = (tick, sync_interval)
            if settings != interval_settings:
                next_sync = time.monotonic() + sync_interval
                interval_settings = settings

            now = time.monotonic()
            for nid, deadline in tuple(self._injected_expiry.items()):
                if deadline <= now:
                    self._injected_expiry.pop(nid, None)
                    await self._device_close(nid)

            if self._needs_sync:
                await self._send_sync()

            values = self._sample()

            epoch, offset = self.clock.read()
            async with self._state_lock:
                if offset != last_offset:
                    self.model.set_clock(epoch, offset)
                    await self.send(proto.clock(epoch, offset))
                    last_offset = offset

                if self.model.set_zones(build_zones(self.cfg.bar.preset, values)):
                    await self.send(proto.bar(self.model.zones, self.model.rev))

            if time.monotonic() >= next_sync:
                await self._send_sync()
                next_sync = time.monotonic() + float(self.cfg.daemon.sync_interval_s)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if self._writer is not None:
                await self.send({"t": "ping", "ts": int(time.time())})


async def _run(cfg: Config, stop: asyncio.Event, cfg_path: str | None = None, port_override: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    daemon = Daemon(cfg, stop, cfg_path, port_override)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass
    try:
        loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(daemon.reload()))
    except (NotImplementedError, AttributeError):  # pragma: no cover
        pass
    await daemon.run()


def default_config_path() -> str | None:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "349d", "config.toml")
    return path if os.path.exists(path) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="349d", description=__doc__)
    parser.add_argument("--config", help="TOML config file (default: ~/.config/349d/config.toml)")
    parser.add_argument("--port", help="serial port (default: auto-detect by-id)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg_path = args.config or default_config_path()
    try:
        cfg = load_config(cfg_path)
    except (OSError, ValueError) as exc:
        parser.error(f"invalid configuration: {exc}")

    stop = asyncio.Event()
    try:
        asyncio.run(_run(cfg, stop, cfg_path, args.port))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
