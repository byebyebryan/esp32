"""/proc based CPU and memory load."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class SysinfoSource:
    def __init__(self) -> None:
        self._prev: tuple[int, int] | None = None

    def read(self) -> dict:
        return {"cpu": self._cpu(), "mem": self._mem()}

    def _cpu(self) -> float | None:
        try:
            with open("/proc/stat", encoding="ascii") as fh:
                parts = fh.readline().split()
        except OSError:
            return None
        if not parts or parts[0] != "cpu":
            return None

        values = [int(x) for x in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        busy = total - idle

        if self._prev is None:
            self._prev = (busy, total)
            return None

        d_busy = busy - self._prev[0]
        d_total = total - self._prev[1]
        self._prev = (busy, total)
        if d_total <= 0:
            return None
        return max(0.0, min(1.0, d_busy / d_total))

    def _mem(self) -> float | None:
        info: dict[str, int] = {}
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.split()[0])
        except (OSError, ValueError):
            return None

        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        if not total or available is None:
            return None
        return max(0.0, min(1.0, 1.0 - available / total))
