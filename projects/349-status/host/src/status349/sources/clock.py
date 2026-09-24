"""Wall clock and current UTC offset (seconds east)."""

from __future__ import annotations

import time


class ClockSource:
    def read(self) -> tuple[int, int]:
        local = time.localtime()
        return int(time.time()), int(local.tm_gmtoff)
