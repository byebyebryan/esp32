import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_dir(tmp_path, monkeypatch):
    """Keep test sockets/pause flags out of the real $XDG_RUNTIME_DIR."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path
