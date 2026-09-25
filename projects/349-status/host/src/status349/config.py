"""Configuration: defaults, TOML overlay, and the default bar preset.

The bar preset is host-side composition only - changing it never requires a
device firmware change.
"""

from __future__ import annotations

import copy
import math
import re
import tomllib
from dataclasses import dataclass, field

from . import proto

DEVICE_WIDTH = 640
BAR_HORIZONTAL_PADDING = 16
BAR_ZONE_GAP = 8
BAR_DEFAULT_ZONE_WIDTH = 60
DEVICE_MAX_ZONES = 8
DEVICE_MAX_NOTIFS = 8
ZONE_ID_BYTES = 15
ZONE_KIND_BYTES = 11
ZONE_TEXT_BYTES = 95
ZONE_FORMAT_BYTES = 15
ZONE_ALIGN_BYTES = 7
ZONE_KINDS = {"text", "progress", "clock", "media", "spacer"}
ZONE_KEYS = {"id", "kind", "w", "text", "value", "format", "color", "align"}

# Default 640x172 layout. Fixed widths sum to 340; the spacer absorbs the rest.
DEFAULT_PRESET: list[dict] = [
    {"id": "clock", "kind": "clock", "w": 80, "format": "%H:%M"},
    {"id": "spacer", "kind": "spacer", "w": 0},
    {"id": "cpu", "kind": "text", "w": 60},
    {"id": "mem", "kind": "progress", "w": 70},
    {"id": "vol", "kind": "progress", "w": 70},
    {"id": "batt", "kind": "text", "w": 60},
]

# Notification text can contain OTPs; mirroring those to a desk display is a
# deliberate default-ignore list, not an accident.
DEFAULT_IGNORE_APPS = [
    "KeePassXC",
    "KeePass",
    "Bitwarden",
    "1Password",
    "Proton Pass",
    "Enpass",
    "gnome-keyring",
    "KDE Wallet",
]


@dataclass
class LinkConfig:
    port: str | None = None
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 10.0


@dataclass
class DaemonConfig:
    tick_s: float = 1.0
    sync_interval_s: float = 60.0


@dataclass
class NotificationsConfig:
    mode: str = "mirror"  # mirror | off
    device_dismiss: str = "local"  # local | propagate
    ignore_apps: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_APPS))
    max_visible: int = 3
    popup_timeout_ms: int = 5000  # fallback when Notify requests server default (-1)
    critical_popup_timeout_ms: int = 0  # 0 keeps critical cards until close


@dataclass
class BarConfig:
    preset: list[dict] = field(default_factory=lambda: copy.deepcopy(DEFAULT_PRESET))


@dataclass
class Config:
    link: LinkConfig
    daemon: DaemonConfig
    notifications: NotificationsConfig
    bar: BarConfig


def default_config() -> Config:
    return Config(
        link=LinkConfig(),
        daemon=DaemonConfig(),
        notifications=NotificationsConfig(),
        bar=BarConfig(),
    )


def load_config(path: str | None = None) -> Config:
    cfg = default_config()
    if path is None:
        return cfg

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    for section, target in (
        ("link", cfg.link),
        ("daemon", cfg.daemon),
        ("notifications", cfg.notifications),
        ("bar", cfg.bar),
    ):
        values = data.get(section, {})
        for key, value in values.items():
            if not hasattr(target, key):
                raise ValueError(f"unknown config key {section}.{key}")
            setattr(target, key, value)

    if cfg.bar.preset is DEFAULT_PRESET:
        cfg.bar.preset = copy.deepcopy(DEFAULT_PRESET)
    validate_config(cfg)
    return cfg


def _string_bytes(value: object, field_name: str, max_bytes: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    if size > max_bytes:
        raise ValueError(f"{field_name} is {size} UTF-8 bytes; device limit is {max_bytes}")
    return value


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _validate_zone(zone: object, index: int) -> dict:
    label = f"bar.preset[{index}]"
    if not isinstance(zone, dict):
        raise ValueError(f"{label} must be a table")
    extra = set(zone) - ZONE_KEYS
    if extra:
        raise ValueError(f"{label} has unsupported fields: {', '.join(sorted(extra))}")
    if not {"id", "kind", "w"}.issubset(zone):
        raise ValueError(f"{label} requires id, kind, and w")

    _string_bytes(zone["id"], f"{label}.id", ZONE_ID_BYTES, allow_empty=False)
    kind = _string_bytes(zone["kind"], f"{label}.kind", ZONE_KIND_BYTES, allow_empty=False)
    if kind not in ZONE_KINDS:
        raise ValueError(f"{label}.kind must be one of {', '.join(sorted(ZONE_KINDS))}")
    width = zone["w"]
    if isinstance(width, bool) or not isinstance(width, int) or not 0 <= width <= DEVICE_WIDTH:
        raise ValueError(f"{label}.w must be an integer from 0 to {DEVICE_WIDTH}")
    if kind == "spacer" and width != 0:
        raise ValueError(f"{label}.w must be 0 for a flex spacer")

    if "text" in zone:
        _string_bytes(zone["text"], f"{label}.text", ZONE_TEXT_BYTES)
    if "format" in zone:
        _string_bytes(zone["format"], f"{label}.format", ZONE_FORMAT_BYTES)
    if "align" in zone:
        align = _string_bytes(zone["align"], f"{label}.align", ZONE_ALIGN_BYTES)
        if align not in {"", "left", "center", "right"}:
            raise ValueError(f"{label}.align must be left, center, or right")
    if "color" in zone:
        color = zone["color"]
        if not isinstance(color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None:
            raise ValueError(f"{label}.color must be a #RRGGBB string")
    if "value" in zone:
        value = zone["value"]
        if not _finite_number(value):
            raise ValueError(f"{label}.value must be a finite number from 0 to 1")
        if not 0 <= value <= 1:
            raise ValueError(f"{label}.value must be a finite number from 0 to 1")
    return zone


def _validate_sync_size(preset: list[dict]) -> None:
    """Prove the configured bar and fixed sync envelope fit before runtime."""
    zones = []
    for spec in preset:
        zone = dict(spec)
        # Measure worst-case escaped strings within the device's fixed buffers.
        zone["id"] = "\0" * ZONE_ID_BYTES
        zone["kind"] = "\0" * ZONE_KIND_BYTES
        zone["w"] = DEVICE_WIDTH
        if "text" in zone or spec["kind"] in {"text", "progress"}:
            zone["text"] = "\0" * ZONE_TEXT_BYTES
        if "format" in zone:
            zone["format"] = "\0" * ZONE_FORMAT_BYTES
        if "align" in zone:
            zone["align"] = "\0" * ZONE_ALIGN_BYTES
        zones.append(zone)

    sync = {
        "t": "sync",
        "rev": 9223372036854775807,
        "bar": {"t": "bar", "rev": 9223372036854775807, "zones": zones},
        "clock": {"epoch": 9223372036854775807, "offset": -2147483648},
        "media": None,
        "notifs": [],
        "notifs_overflow": 999,
    }
    try:
        proto.encode(sync)
    except ValueError as exc:
        raise ValueError(f"bar preset makes a sync exceed the {proto.LINE_MAX}-byte device line limit") from exc


def validate_config(cfg: Config) -> None:
    """Validate host settings against the fixed v1 device protocol limits."""
    if cfg.link.port is not None and not isinstance(cfg.link.port, str):
        raise ValueError("link.port must be a string or omitted")
    for field_name, value in (
        ("link.reconnect_min_s", cfg.link.reconnect_min_s),
        ("link.reconnect_max_s", cfg.link.reconnect_max_s),
    ):
        if not _finite_number(value) or value <= 0:
            raise ValueError(f"{field_name} must be a finite positive number")
    if cfg.link.reconnect_max_s < cfg.link.reconnect_min_s:
        raise ValueError("link.reconnect_max_s must be at least link.reconnect_min_s")

    for field_name, value in (
        ("daemon.tick_s", cfg.daemon.tick_s),
        ("daemon.sync_interval_s", cfg.daemon.sync_interval_s),
    ):
        if not _finite_number(value):
            raise ValueError(f"{field_name} must be a finite number")
        if value < 0.05:
            raise ValueError(f"{field_name} must be at least 0.05 seconds")
    if cfg.daemon.sync_interval_s < cfg.daemon.tick_s:
        raise ValueError("daemon.sync_interval_s must be at least daemon.tick_s")

    if not isinstance(cfg.notifications.mode, str) or cfg.notifications.mode not in {"mirror", "off"}:
        raise ValueError("notifications.mode must be 'mirror' or 'off'")
    if not isinstance(cfg.notifications.device_dismiss, str) or cfg.notifications.device_dismiss not in {"local", "propagate"}:
        raise ValueError("notifications.device_dismiss must be 'local' or 'propagate'")
    if not isinstance(cfg.notifications.ignore_apps, list) or any(not isinstance(app, str) for app in cfg.notifications.ignore_apps):
        raise ValueError("notifications.ignore_apps must be an array of strings")
    if (
        isinstance(cfg.notifications.max_visible, bool)
        or not isinstance(cfg.notifications.max_visible, int)
        or not 0 <= cfg.notifications.max_visible <= DEVICE_MAX_NOTIFS
    ):
        raise ValueError(f"notifications.max_visible must be an integer from 0 to {DEVICE_MAX_NOTIFS}")
    for field_name in ("popup_timeout_ms", "critical_popup_timeout_ms"):
        timeout = getattr(cfg.notifications, field_name)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 86_400_000:
            raise ValueError(f"notifications.{field_name} must be an integer from 0 to 86400000")

    if not isinstance(cfg.bar.preset, list):
        raise ValueError("bar.preset must be an array of tables")
    if len(cfg.bar.preset) > DEVICE_MAX_ZONES:
        raise ValueError(f"bar.preset has {len(cfg.bar.preset)} zones; device limit is {DEVICE_MAX_ZONES}")
    seen_ids: set[str] = set()
    total_width = 0
    validated_zones = []
    for index, raw_zone in enumerate(cfg.bar.preset):
        zone = _validate_zone(raw_zone, index)
        if zone["id"] in seen_ids:
            raise ValueError(f"bar.preset contains duplicate zone id {zone['id']!r}")
        seen_ids.add(zone["id"])
        total_width += zone["w"] or (0 if zone["kind"] == "spacer" else BAR_DEFAULT_ZONE_WIDTH)
        validated_zones.append(zone)
    total_width += max(0, len(validated_zones) - 1) * BAR_ZONE_GAP
    usable_width = DEVICE_WIDTH - BAR_HORIZONTAL_PADDING
    if total_width > usable_width:
        raise ValueError(f"bar.preset needs {total_width} pixels including gaps; device bar has {usable_width}")
    _validate_sync_size(validated_zones)


def apply_config(target: Config, source: Config) -> None:
    """Copy values into the live config so existing references stay valid."""
    for section in ("link", "daemon", "notifications", "bar"):
        dst = getattr(target, section)
        src = getattr(source, section)
        for field_name in vars(src):
            setattr(dst, field_name, getattr(src, field_name))
