"""Fake 349-status device on a pty, for host-side development without hardware.

Run `python -m status349.fake` to get a pty path, then point the daemon at it:
`349d --port /dev/pts/N`.
"""

from __future__ import annotations

import argparse
import logging
import os
import pty
import threading
import time
import tty

from . import proto

log = logging.getLogger("349-fake")


class FakeDevice:
    def __init__(self) -> None:
        master, slave = pty.openpty()
        tty.setraw(master)
        tty.setraw(slave)
        self.master = master
        self.path = os.ttyname(slave)
        self.received: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeDevice":
        self._thread = threading.Thread(target=self._run, name="fake349", daemon=True)
        self._thread.start()
        return self

    def send(self, message: dict) -> None:
        try:
            os.write(self.master, proto.encode(message))
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass

    def _run(self) -> None:
        self.send(proto.hello())  # device announces itself on boot
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                time.sleep(0.05)
                continue

            buf += chunk
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = rest
                line = raw.decode("utf-8", "replace").rstrip("\r")
                is_data, message = proto.classify(line)
                if not is_data or message is None:
                    continue
                self.received.append(message)
                if message.get("t") == "hello":
                    self.send(proto.hello())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="349-fake", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    device = FakeDevice().start()
    print(device.path, flush=True)
    log.info("fake device up on %s", device.path)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        device.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
