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
from .config import Config, apply_config, load_config
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


class Daemon:
    def __init__(
        self, cfg: Config, stop: asyncio.Event, cfg_path: str | None = None, port_override: str | None = None
    ):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self._port_override = port_override
        if port_override is not None:
            self.cfg.link.port = port_override
        self.stop = stop
        self.model = StateModel(max_visible=cfg.notifications.max_visible)
        self.clock = ClockSource()
        self.sysinfo = SysinfoSource()
        self.volume = VolumeSource()
        self.power = PowerSource()
        self.notifications = NotificationSource(cfg.notifications, self._device_notify, self._device_close)
        self._writer: asyncio.StreamWriter | None = None
        self._devlog: deque[str] = deque(maxlen=200)
        self._last_rx_mono: float | None = None
        self._needs_sync = False
        self._next_notify_id = 100000
        self._ipc = IpcServer(self._ipc_handler)

    async def run(self) -> None:
        await self.notifications.start()
        await self._ipc.start()
        link = asyncio.create_task(self._link_loop(), name="link")
        tick = asyncio.create_task(self._tick_loop(), name="tick")
        try:
            await self.stop.wait()
        finally:
            if self._writer is not None:
                self._writer.close()
            link.cancel()
            tick.cancel()
            await asyncio.gather(link, tick, return_exceptions=True)
            await self.notifications.stop()
            await self._ipc.stop()

    async def _device_notify(self, message: dict) -> None:
        self.model.add_notification(message)
        await self.send(message)

    async def _device_close(self, local_id: int) -> None:
        self.model.close_notification(local_id)
        await self.send(proto.close(local_id))

    async def send(self, message: dict) -> bool:
        if self._writer is None:
            return False
        try:
            self._writer.write(proto.encode(message))
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
        epoch, offset = self.clock.read()
        self.model.set_clock(epoch, offset)
        self.model.set_zones(build_zones(self.cfg.bar.preset, self._sample()))
        await self.send(self.model.snapshot())

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
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
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
            self._writer = writer
            # Opening the port resets the chip, but the kernel's DTR/RTS raise
            # can land it in download mode; force a normal boot, then the
            # device announces itself.
            serial_port = getattr(writer.transport, "serial", None)
            if serial_port is not None:
                await reset_to_normal_boot_async(serial_port)
            await self.send(proto.hello())

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
            log.info(
                "device hello: fw=%s proto=%s cap=%s",
                message.get("fw"),
                message.get("proto"),
                message.get("cap"),
            )
            await self._send_sync()
        elif kind == "input":
            await self._handle_input(message)
        elif kind == "resync":
            log.info("device requested resync (%s)", message.get("reason"))
            await self._send_sync()
        elif kind == "ack":
            log.debug("device ack: %s", message.get("v"))
        else:
            log.debug("unknown device message: %s", message)

    async def _handle_input(self, message: dict) -> None:
        action = message.get("action")
        if action == "dismiss" and "id" in message:
            await self.notifications.dismiss(int(message["id"]))
        else:
            log.info("device input: %s", message)

    def reload(self) -> None:
        if self.cfg_path is None:
            log.info("no config file to reload")
            return
        try:
            new = load_config(self.cfg_path)
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the daemon
            log.error("config reload failed: %s", exc)
            return
        apply_config(self.cfg, new)
        if self._port_override is not None:
            self.cfg.link.port = self._port_override
        self.model.max_visible = self.cfg.notifications.max_visible
        self._needs_sync = True
        log.info("config reloaded from %s", self.cfg_path)

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
            return {"ok": True, "id": nid}
        if cmd == "pause":
            self.pause()
            return {"ok": True}
        if cmd == "resume":
            self.resume()
            return {"ok": True}
        if cmd == "reload":
            self.reload()
            return {"ok": True}
        if cmd == "log":
            count = max(1, min(200, int(request.get("lines", 50))))
            return {"ok": True, "lines": list(self._devlog)[-count:]}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    async def _tick_loop(self) -> None:
        tick = max(0.05, float(self.cfg.daemon.tick_s))
        sync_interval = max(tick, float(self.cfg.daemon.sync_interval_s))
        last_offset: int | None = None
        next_sync = time.monotonic() + sync_interval

        while True:
            await asyncio.sleep(tick)

            if self._needs_sync:
                self._needs_sync = False
                await self._send_sync()

            values = self._sample()

            epoch, offset = self.clock.read()
            if offset != last_offset:
                self.model.set_clock(epoch, offset)
                await self.send(proto.clock(epoch, offset))
                last_offset = offset

            if self.model.set_zones(build_zones(self.cfg.bar.preset, values)):
                await self.send(proto.bar(self.model.zones, self.model.rev))

            if time.monotonic() >= next_sync:
                await self._send_sync()
                next_sync = time.monotonic() + sync_interval


async def _run(cfg: Config, stop: asyncio.Event, cfg_path: str | None = None, port_override: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    daemon = Daemon(cfg, stop, cfg_path, port_override)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass
    try:
        loop.add_signal_handler(signal.SIGHUP, daemon.reload)
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
    cfg = load_config(cfg_path)

    stop = asyncio.Event()
    try:
        asyncio.run(_run(cfg, stop, cfg_path, args.port))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
