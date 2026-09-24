"""Default sink volume via wpctl (PipeWire)."""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger(__name__)

_VOLUME_RE = re.compile(r"Volume:\s*([0-9.]+)")


def parse_wpctl(text: str) -> tuple[float | None, bool]:
    match = _VOLUME_RE.search(text)
    volume = float(match.group(1)) if match else None
    return volume, "[MUTED]" in text


class VolumeSource:
    def read(self) -> dict:
        try:
            result = subprocess.run(
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"vol": None, "mute": None}
        if result.returncode != 0:
            return {"vol": None, "mute": None}

        volume, mute = parse_wpctl(result.stdout)
        return {"vol": volume, "mute": mute}
