"""Unix-socket control interface and sticky pause flag for 349d.

The socket lives in $XDG_RUNTIME_DIR (falling back to /tmp for the current
user), so `349ctl` can talk to the daemon without touching the serial port.
The pause flag is a file: `349ctl pause` creates it even when the daemon is
down, so a `Restart=always` unit cannot grab the tty mid-flash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger("349d.ipc")


def _runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) if runtime else Path(f"/tmp")


def socket_path() -> Path:
    return _runtime_dir() / "349d.sock"


def pause_path() -> Path:
    return _runtime_dir() / "349d.paused"


class IpcServer:
    def __init__(self, handler: Callable[[dict], Awaitable[dict]]):
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = socket_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._serve, path=str(path))
        os.chmod(path, 0o600)
        log.info("ipc listening on %s", path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            socket_path().unlink()
        except FileNotFoundError:
            pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(line.decode() or "{}")
            response = await self._handler(request)
        except Exception as exc:  # noqa: BLE001 - report every failure to the client
            response = {"ok": False, "error": str(exc)}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
