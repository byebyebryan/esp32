"""Configuration: defaults, TOML overlay, and the default bar preset.

The bar preset is host-side composition only - changing it never requires a
device firmware change.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field

# Default 640x172 layout. Fixed widths sum to 340; the spacer absorbs the rest
# (the device also drops trailing zones if a row overflows).
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
    mode: str = "mirror"  # mirror | off | consume (M3)
    device_dismiss: str = "local"  # local | propagate
    ignore_apps: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_APPS))
    max_visible: int = 3


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
    return cfg


def apply_config(target: Config, source: Config) -> None:
    """Copy values into the live config so existing references stay valid."""
    for section in ("link", "daemon", "notifications", "bar"):
        dst = getattr(target, section)
        src = getattr(source, section)
        for field_name in vars(src):
            setattr(dst, field_name, getattr(src, field_name))
