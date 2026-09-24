"""USB-Serial-JTAG transport for the 349-status display."""

from __future__ import annotations

import asyncio
import glob
import time
from collections.abc import Iterator

import serial

from .proto import classify, encode

PORT_PATTERNS = (
    "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*-if00",
    "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*",
)


class LinkError(RuntimeError):
    pass


def find_port() -> str:
    for pattern in PORT_PATTERNS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    raise LinkError("device not found: no Espressif USB-Serial-JTAG port")


def reset_to_normal_boot(port: serial.Serial) -> None:
    """Force a normal boot after the port-open reset.

    The kernel raises DTR/RTS together on open; depending on when GPIO0 is
    sampled the chip can land in download mode instead. Pulse EN with GPIO0
    high so it always boots the application.
    """
    try:
        port.dtr = False  # GPIO0 high
        port.rts = True  # EN low, chip in reset
        time.sleep(0.1)
        port.rts = False  # EN high, normal boot
        time.sleep(0.05)
        port.dtr = False
    except (OSError, serial.SerialException):
        pass


async def reset_to_normal_boot_async(port: serial.Serial) -> None:
    try:
        port.dtr = False
        port.rts = True
        await asyncio.sleep(0.1)
        port.rts = False
        await asyncio.sleep(0.05)
        port.dtr = False
    except (OSError, serial.SerialException):
        pass


def open_port(path: str | None = None) -> serial.Serial:
    # The kernel raises DTR/RTS on every tty open, which the ESP32-S3
    # USB-Serial-JTAG turns into a chip reset (cannot be disabled on the S3).
    # Pre-setting both lines low does not prevent that, but keeps them
    # de-asserted for the rest of the session.
    port = serial.Serial(port=None, baudrate=115200, timeout=0.2)
    port.dtr = False
    port.rts = False
    port.port = path or find_port()
    try:
        port.open()
    except serial.SerialException as exc:
        raise LinkError(str(exc)) from exc
    reset_to_normal_boot(port)
    return port


class Link:
    def __init__(self, port: serial.Serial):
        self.port = port
        self._buf = bytearray()

    def __enter__(self) -> Link:
        return self

    def __exit__(self, *exc: object) -> None:
        self.port.close()

    def send(self, obj: dict) -> None:
        self.port.write(encode(obj))

    def lines(self, timeout: float = 0.2) -> Iterator[str]:
        """Yield complete lines received within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.port.timeout = min(remaining, 0.2)
            chunk = self.port.read(256)
            if not chunk:
                continue
            self._buf.extend(chunk)
            while (idx := self._buf.find(b"\n")) >= 0:
                raw = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                yield raw.decode("utf-8", "replace").rstrip("\r")


__all__ = [
    "Link",
    "LinkError",
    "classify",
    "encode",
    "find_port",
    "open_port",
    "reset_to_normal_boot",
    "reset_to_normal_boot_async",
]
