"""Reload applies live settings while preserving source ownership boundaries."""

import asyncio
import time

from dbus_next import Message

from status349.config import load_config
from status349.daemon import Daemon
from status349 import proto


def _wait_for(predicate, timeout=3.0):
    async def wait():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met in time")

    return wait()


def test_reload_changes_notification_mode_and_preserves_cli_port(tmp_path):
    cfg_path = tmp_path / "349d.toml"
    cfg_path.write_text(
        "[link]\nport = '/from/config'\n\n"
        "[notifications]\nmode = 'mirror'\nmax_visible = 3\n\n"
        "[daemon]\ntick_s = 0.05\nsync_interval_s = 0.5\n"
    )
    cfg = load_config(str(cfg_path))

    async def scenario():
        daemon = Daemon(cfg, asyncio.Event(), str(cfg_path), port_override="/from/cli")
        sent = []
        close_started = asyncio.Event()
        finish_close = asyncio.Event()

        async def capture(message):
            sent.append(message)
            if message == {"t": "close", "id": 1}:
                close_started.set()
                await finish_close.wait()
            return True

        async def park_monitor():
            await asyncio.Future()

        daemon.send = capture
        daemon.notifications._monitor_loop = park_monitor
        await daemon.notifications.start()
        daemon.model.add_notification(proto.notify(1, "desktop", "mirrored", "body", 1, 5000, 1))
        daemon.model.add_notification(proto.notify(100000, "349ctl", "injected", "body", 1, 5000, 1))
        daemon.notifications._mirrored_local_ids.add(1)

        cfg_path.write_text(
            "[link]\nport = '/replacement-from-config'\n\n"
            "[notifications]\nmode = 'off'\nmax_visible = 2\n\n"
            "[daemon]\ntick_s = 0.2\nsync_interval_s = 0.8\n\n"
            "[bar]\npreset = [{ id = 'greet', kind = 'text', w = 80, text = 'hi' }]\n"
        )
        reload_task = asyncio.create_task(daemon.reload())
        await asyncio.wait_for(close_started.wait(), 1)
        daemon.notifications._messages.put_nowait(
            Message(
                destination="org.freedesktop.Notifications",
                path="/org/freedesktop/Notifications",
                interface="org.freedesktop.Notifications",
                member="Notify",
                signature="susssasa{sv}i",
                sender=":1.90",
                serial=1,
                body=["desktop", 0, "", "arrives during off transition", "body", [], {}, 5000],
            )
        )
        await asyncio.sleep(0.02)
        assert set(daemon.model.notifs) == {100000}
        finish_close.set()
        assert await reload_task

        assert daemon.cfg.link.port == "/from/cli"
        assert daemon.model.max_visible == 2
        assert set(daemon.model.notifs) == {100000}
        assert {message["id"] for message in sent if message["t"] == "close"} == {1}
        assert daemon.notifications._process_task is None
        assert daemon.notifications._messages.empty()
        assert daemon.notifications.cfg is daemon.cfg.notifications

        # A later valid notification still works after the close was retired.
        assert daemon.model.add_notification(proto.notify(2, "desktop", "new", "body", 1, 5000, 1))
        daemon.notifications._mirrored_local_ids.add(2)

        # Arrange inert loops so this test verifies mode reconciliation without
        # requiring a real session bus.
        async def park():
            await asyncio.Future()

        daemon.notifications._process_loop = park
        daemon.notifications._send_loop = park
        daemon.notifications._monitor_loop = park
        cfg_path.write_text(
            "[link]\nport = '/another-config-port'\n\n"
            "[notifications]\nmode = 'mirror'\nmax_visible = 2\n\n"
            "[daemon]\ntick_s = 0.2\nsync_interval_s = 0.8\n"
        )
        assert await daemon.reload()
        assert daemon.notifications._process_task is not None
        assert daemon.cfg.link.port == "/from/cli"
        await daemon.notifications.stop()

    asyncio.run(scenario())


def test_reload_rejects_invalid_file_and_keeps_previous_config(tmp_path):
    cfg_path = tmp_path / "349d.toml"
    cfg_path.write_text("[daemon]\ntick_s = 0.1\nsync_interval_s = 0.5\n")
    cfg = load_config(str(cfg_path))

    async def scenario():
        daemon = Daemon(cfg, asyncio.Event(), str(cfg_path), port_override="/cli-port")
        before = (cfg.daemon.tick_s, cfg.daemon.sync_interval_s, cfg.notifications.mode, cfg.link.port)
        cfg_path.write_text("[notifications]\nmax_visible = 9\n")
        assert not await daemon.reload()
        assert (cfg.daemon.tick_s, cfg.daemon.sync_interval_s, cfg.notifications.mode, cfg.link.port) == before
        assert daemon.cfg.link.port == "/cli-port"

    asyncio.run(scenario())


def test_tick_and_sync_intervals_take_effect_after_reload(tmp_path):
    cfg_path = tmp_path / "349d.toml"
    cfg_path.write_text(
        "[notifications]\nmode = 'off'\n\n[daemon]\ntick_s = 0.05\nsync_interval_s = 0.2\n"
    )
    cfg = load_config(str(cfg_path))

    async def scenario():
        daemon = Daemon(cfg, asyncio.Event(), str(cfg_path))
        sample_times = []
        sync_times = []

        def sample():
            sample_times.append(time.monotonic())
            return {}

        async def send_sync():
            sync_times.append(time.monotonic())

        daemon._sample = sample
        daemon._send_sync = send_sync
        tick_task = asyncio.create_task(daemon._tick_loop())
        try:
            await _wait_for(lambda: len(sample_times) >= 3)
            cfg_path.write_text(
                "[notifications]\nmode = 'off'\n\n[daemon]\ntick_s = 0.2\nsync_interval_s = 0.8\n"
            )
            syncs_before_reload = len(sync_times)
            assert await daemon.reload()
            await _wait_for(lambda: len(sample_times) >= 6)
            post_reload_samples = sample_times[-3:]
            gaps = [b - a for a, b in zip(post_reload_samples, post_reload_samples[1:])]
            assert all(gap >= 0.15 for gap in gaps)

            await _wait_for(lambda: len(sync_times) >= syncs_before_reload + 2, timeout=2.0)
            assert 0.7 <= sync_times[-1] - sync_times[-2] <= 1.1
        finally:
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
