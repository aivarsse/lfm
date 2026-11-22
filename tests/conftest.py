import json
from pathlib import Path

import pytest
import stamina

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch):
    """Keep tests away from the real config, cache and history."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def _instant_retries():
    """Keep the real attempt count but drop the backoff waits in tests."""
    with stamina.set_testing(True, attempts=3):
        yield


@pytest.fixture
def fixture_json():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text())

    return _load


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Point config/cache at a tmp dir and provide a dummy API key."""
    monkeypatch.setenv("LFM_API_KEY", "test-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
