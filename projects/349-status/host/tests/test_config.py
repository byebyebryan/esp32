import pytest

from status349.config import default_config, load_config
from status349.daemon import main


def test_defaults():
    cfg = default_config()
    assert cfg.daemon.tick_s == 1.0
    assert cfg.daemon.sync_interval_s == 60.0
    assert cfg.notifications.mode == "mirror"
    assert cfg.notifications.device_dismiss == "local"
    assert cfg.notifications.ignore_apps
    assert cfg.bar.preset


def test_default_preset_is_copied():
    first = default_config()
    first.bar.preset[0]["w"] = 1
    assert default_config().bar.preset[0]["w"] == 80


def test_toml_overlay(tmp_path):
    path = tmp_path / "349d.toml"
    path.write_text(
        "[link]\nport = '/dev/fake'\n\n[daemon]\ntick_s = 0.25\n\n[notifications]\nmax_visible = 5\n"
    )
    cfg = load_config(str(path))
    assert cfg.link.port == "/dev/fake"
    assert cfg.daemon.tick_s == 0.25
    assert cfg.notifications.max_visible == 5
    assert cfg.notifications.device_dismiss == "local"


def test_custom_preset(tmp_path):
    path = tmp_path / "349d.toml"
    path.write_text("[bar]\npreset = [{ id = 'cpu', kind = 'text', w = 60 }]\n")
    cfg = load_config(str(path))
    assert cfg.bar.preset == [{"id": "cpu", "kind": "text", "w": 60}]


def test_unknown_key_rejected(tmp_path):
    path = tmp_path / "349d.toml"
    path.write_text("[daemon]\nnope = 1\n")
    with pytest.raises(ValueError):
        load_config(str(path))


@pytest.mark.parametrize(
    "config_text, message",
    [
        ("[notifications]\nmax_visible = 9\n", "max_visible"),
        ("[bar]\npreset = [" + ",".join("{ id = 'z%d', kind = 'text', w = 1 }" % i for i in range(9)) + "]\n", "zones"),
        ("[bar]\npreset = [{ id = 'wide', kind = 'text', w = 641 }]\n", "w must"),
        ("[bar]\npreset = [{ id = 'a', kind = 'text', w = 310 }, { id = 'b', kind = 'text', w = 310 }]\n", "including gaps"),
        ("[bar]\npreset = [{ id = 'spacer', kind = 'spacer', w = 10 }]\n", "flex spacer"),
        ("[bar]\npreset = [{ id = 'long', kind = 'text', w = 20, text = '" + "x" * 96 + "' }]\n", "text"),
        ("[daemon]\ntick_s = 0.2\nsync_interval_s = 0.1\n", "at least"),
        ("[notifications]\nmode = 'consume'\n", "mode"),
    ],
)
def test_protocol_limits_rejected(tmp_path, config_text, message):
    path = tmp_path / "349d.toml"
    path.write_text(config_text)
    with pytest.raises(ValueError, match=message):
        load_config(str(path))


def test_invalid_startup_config_has_a_clear_cli_error(tmp_path, capsys):
    path = tmp_path / "349d.toml"
    path.write_text("[notifications]\nmax_visible = 9\n")
    with pytest.raises(SystemExit) as error:
        main(["--config", str(path)])
    assert error.value.code == 2
    assert "invalid configuration" in capsys.readouterr().err
