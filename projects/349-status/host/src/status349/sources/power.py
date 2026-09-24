"""Battery state from /sys/class/power_supply."""

from __future__ import annotations

import glob
import logging

log = logging.getLogger(__name__)


def parse_power_supply(capacity: str, status: str) -> tuple[float | None, bool | None]:
    try:
        percent = int(capacity.strip())
    except (AttributeError, ValueError):
        return None, None

    charging = None
    if status:
        charging = status.strip() in ("Charging", "Full")
    return max(0.0, min(1.0, percent / 100.0)), charging


class PowerSource:
    def __init__(self) -> None:
        self._paths = sorted(glob.glob("/sys/class/power_supply/BAT*"))

    def read(self) -> dict:
        if not self._paths:
            return {"batt": None, "charging": None}

        base = self._paths[0]
        try:
            with open(base + "/capacity", encoding="ascii") as fh:
                capacity = fh.read()
            status = ""
            try:
                with open(base + "/status", encoding="ascii") as fh:
                    status = fh.read()
            except OSError:
                pass
        except OSError:
            return {"batt": None, "charging": None}

        batt, charging = parse_power_supply(capacity, status)
        return {"batt": batt, "charging": charging}
