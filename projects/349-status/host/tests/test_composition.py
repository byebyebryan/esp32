import copy

from status349.composition import build_zones
from status349.config import DEFAULT_PRESET


def zones_by_id(preset=None, values=None):
    zones = build_zones(preset or DEFAULT_PRESET, values or {})
    return {zone["id"]: zone for zone in zones}


def test_formats_values():
    zones = zones_by_id(
        values={"cpu": 0.123, "mem": 0.5, "vol": 0.42, "mute": False, "batt": 0.78, "charging": True}
    )
    assert zones["cpu"]["text"] == "12%"
    assert zones["mem"]["value"] == 0.5
    assert zones["vol"]["value"] == 0.42
    assert zones["batt"]["text"] == "78%+"
    assert zones["clock"]["kind"] == "clock"


def test_media_zone_passes_through_when_configured():
    preset = [{"id": "media", "kind": "media", "w": 220}]
    assert build_zones(preset, {})[0]["kind"] == "media"


def test_unknown_values_render_as_unknown():
    zones = zones_by_id()
    assert zones["cpu"]["text"] == "--"
    assert zones["mem"]["value"] == 0.0
    assert zones["vol"]["value"] == 0.0
    assert zones["batt"]["text"] == "--"


def test_mute_shows_label():
    zones = zones_by_id(values={"vol": 0.5, "mute": True})
    assert zones["vol"]["value"] == 0.0
    assert zones["vol"]["text"] == "mute"


def test_values_clamped():
    zones = zones_by_id(values={"mem": 1.5, "vol": -1})
    assert zones["mem"]["value"] == 1.0
    assert zones["vol"]["value"] == 0.0


def test_preset_not_mutated():
    preset = copy.deepcopy(DEFAULT_PRESET)
    before = copy.deepcopy(preset)
    build_zones(preset, {"cpu": 0.5, "mem": 0.5})
    assert preset == before


def test_extra_string_values_pass_through():
    preset = [{"id": "weather", "kind": "text", "w": 80}]
    zones = build_zones(preset, {"weather": "21C"})
    assert zones[0]["text"] == "21C"


def test_configured_zone_text_uses_display_font_policy():
    preset = [{"id": "weather", "kind": "text", "w": 80}]
    zones = build_zones(preset, {"weather": "Café 東京"})
    assert zones[0]["text"] == "Cafe 東京"


def test_clock_format_uses_display_font_policy():
    preset = [{"id": "clock", "kind": "clock", "w": 80, "format": "%H時%M"}]
    zones = build_zones(preset, {})
    assert zones[0]["format"] == "%H時%M"


def test_static_text_is_preserved():
    preset = [{"id": "greet", "kind": "text", "w": 80, "text": "hello"}]
    assert build_zones(preset, {})[0]["text"] == "hello"
