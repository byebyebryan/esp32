"""Compose bar zones from source values according to the configured preset.

Pure functions: the preset is never mutated and the result is deterministic,
so the daemon can cheaply detect changes.
"""

from __future__ import annotations


def build_zones(preset: list[dict], values: dict) -> list[dict]:
    zones: list[dict] = []
    for spec in preset:
        zone = dict(spec)
        kind = zone.get("kind")
        zid = zone.get("id", "")

        if kind == "text":
            value = _text(zid, values)
            if value is not None:
                zone["text"] = value
            elif "text" not in zone:
                zone["text"] = "--"
        elif kind == "progress":
            value, text = _progress(zid, values)
            zone["value"] = value
            if text is not None:
                zone["text"] = text
        # clock/media/spacer carry no host-side data.

        zones.append(zone)
    return zones


def _text(zid: str, values: dict) -> str | None:
    if zid == "cpu":
        v = values.get("cpu")
        return f"{round(v * 100)}%" if v is not None else None
    if zid == "batt":
        v = values.get("batt")
        if v is None:
            return None
        return f"{round(v * 100)}%" + ("+" if values.get("charging") else "")
    v = values.get(zid)
    return v if isinstance(v, str) else None


def _progress(zid: str, values: dict) -> tuple[float, str | None]:
    if zid == "mem":
        v = values.get("mem")
    elif zid == "vol":
        if values.get("mute"):
            return 0.0, "mute"
        v = values.get("vol")
    else:
        v = values.get(zid)

    if not isinstance(v, (int, float)):
        return 0.0, None
    return max(0.0, min(1.0, float(v))), None
