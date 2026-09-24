import time

from status349.sources.power import parse_power_supply
from status349.sources.sysinfo import SysinfoSource
from status349.sources.volume import parse_wpctl


def test_parse_wpctl():
    assert parse_wpctl("Volume: 0.42\n") == (0.42, False)
    assert parse_wpctl("Volume: 0.42 [MUTED]\n") == (0.42, True)
    assert parse_wpctl("garbage") == (None, False)


def test_parse_power_supply():
    assert parse_power_supply("78\n", "Charging\n") == (0.78, True)
    assert parse_power_supply("78\n", "Full\n") == (0.78, True)
    assert parse_power_supply("78\n", "Discharging\n") == (0.78, False)
    assert parse_power_supply("junk", "Charging") == (None, None)


def test_sysinfo_read():
    source = SysinfoSource()
    first = source.read()
    assert set(first) == {"cpu", "mem"}
    assert first["cpu"] is None  # no delta on the first sample
    assert first["mem"] is None or 0.0 <= first["mem"] <= 1.0

    time.sleep(0.02)
    second = source.read()
    assert second["cpu"] is None or 0.0 <= second["cpu"] <= 1.0
