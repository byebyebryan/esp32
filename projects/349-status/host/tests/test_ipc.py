"""End-to-end IPC tests: daemon + fake device + Unix socket control."""

import asyncio
import json
import time

from status349 import ipc
from status349.config import default_config
from status349.daemon import Daemon
from status349.fake import FakeDevice


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _wait_status(path, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await _ipc({"cmd": "status"}, path)
        if last.get("ok") and predicate(last):
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"status condition not met: {last}")


async def _ipc(request: dict, path) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        return json.loads(line.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def _cfg(tmp_path, fake):
    cfg_path = tmp_path / "349d.toml"
    cfg_path.write_text("[bar]\npreset = [{ id = 'clock', kind = 'clock', w = 80 }]\n")
    cfg = default_config()
    cfg.daemon.tick_s = 0.05
    cfg.daemon.sync_interval_s = 0.5
    return cfg, cfg_path


def test_ipc_commands_and_sticky_pause(tmp_path):
    fake = FakeDevice().start()
    try:
        cfg, cfg_path = _cfg(tmp_path, fake)

        async def scenario():
            stop = asyncio.Event()
            daemon = Daemon(cfg, stop, str(cfg_path), port_override=fake.path)
            task = asyncio.create_task(daemon.run())
            path = ipc.socket_path()
            await _wait_for(path.exists)
            await _wait_for(lambda: any(m["t"] == "sync" for m in fake.received))

            status = await _wait_status(path, lambda s: s["link"])
            assert status["config"] == str(cfg_path)

            assert (await _ipc({"cmd": "text", "value": "hi"}, path))["ok"]
            await _wait_for(lambda: any(m["t"] == "text" and m.get("v") == "hi" for m in fake.received))

            note = await _ipc({"cmd": "notify", "summary": "ipc test", "body": "b"}, path)
            assert note["ok"]
            await _wait_for(lambda: any(m["t"] == "notify" and m["summary"] == "ipc test" for m in fake.received))

            log = await _ipc({"cmd": "log", "lines": 10}, path)
            assert log["ok"] and isinstance(log["lines"], list)

            cfg_path.write_text(
                "[bar]\npreset = [{ id = 'greet', kind = 'text', w = 80, text = 'hi' }]\n"
            )
            assert (await _ipc({"cmd": "reload"}, path))["ok"]
            assert (await _ipc({"cmd": "status"}, path))["port"] == fake.path
            await _wait_for(
                lambda: any(
                    m["t"] == "sync" and any(z.get("id") == "greet" for z in m["bar"]["zones"])
                    for m in fake.received
                )
            )

            assert (await _ipc({"cmd": "pause"}, path))["ok"]
            await _wait_status(path, lambda s: s["paused"] and not s["link"])

            stop.set()
            await asyncio.wait_for(task, 5)

        asyncio.run(scenario())

        # Pause is sticky: a fresh daemon must stay off the port.
        async def restarted():
            stop = asyncio.Event()
            daemon = Daemon(cfg, stop, str(cfg_path), port_override=fake.path)
            task = asyncio.create_task(daemon.run())
            path = ipc.socket_path()
            await _wait_for(path.exists)
            await _wait_status(path, lambda s: s["paused"] and not s["link"])

            assert (await _ipc({"cmd": "resume"}, path))["ok"]
            await _wait_status(path, lambda s: not s["paused"] and s["link"])

            stop.set()
            await asyncio.wait_for(task, 5)

        asyncio.run(restarted())
    finally:
        fake.stop()
