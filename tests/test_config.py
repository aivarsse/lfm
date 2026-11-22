import stat

import pytest

from lfm.config import ConfigError, load, save_value


def write(tmp_path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_missing_file_falls_back_to_defaults(tmp_path):
    config = load(tmp_path / "nope.toml")
    assert config.exists is False
    assert config.get("limit") == 20
    assert config.get("visualizer") is True


def test_reads_and_coerces_values(tmp_path):
    config = load(
        write(
            tmp_path,
            'limit = 7\nmin_match = 0.5\noutput = "json"\nvolume = 42\nvisualizer = false\n',
        )
    )
    assert config.get("limit") == 7
    assert config.get("min_match") == 0.5
    assert config.get("output") == "json"
    assert config.get("volume") == 42
    assert config.get("visualizer") is False


def test_unknown_keys_are_ignored(tmp_path):
    config = load(write(tmp_path, "nonsense = true\nlimit = 3\n"))
    assert config.values == {"limit": 3}


def test_broken_toml_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load(write(tmp_path, "limit = "))


@pytest.mark.parametrize(
    "bad",
    [
        "limit = 0",
        'limit = "many"',
        "min_match = 2",
        'output = "xml"',
        "volume = 999",
        'visualizer = "yes"',
        'theme = "neon"',
    ],
)
def test_invalid_values_are_reported(tmp_path, bad):
    with pytest.raises(ConfigError, match="invalid"):
        load(write(tmp_path, bad))


def test_api_key_prefers_the_environment(tmp_path, monkeypatch):
    config = load(write(tmp_path, 'api_key = "from-file"\n'))

    monkeypatch.setenv("LFM_API_KEY", "from-env")
    assert config.api_key() == "from-env"
    assert config.key_source() == "$LFM_API_KEY"

    monkeypatch.delenv("LFM_API_KEY")
    assert config.api_key() == "from-file"
    assert config.key_source().endswith("config.toml")


def test_api_key_reports_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("LFM_API_KEY", raising=False)
    assert load(tmp_path / "nope.toml").key_source() == "not set"


def test_save_value_creates_the_file(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    save_value(path, "volume", 42)
    assert path.read_text() == "volume = 42\n"
    assert load(path).get("volume") == 42


def test_save_value_replaces_one_key_and_keeps_the_rest(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# my settings\napi_key = "k"\nvolume = 10\nlimit = 5\n')
    save_value(path, "volume", 80)
    text = path.read_text()
    assert "# my settings" in text
    assert 'api_key = "k"' in text
    assert "limit = 5" in text
    assert "volume = 80" in text
    assert "volume = 10" not in text


def test_save_value_appends_a_missing_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("limit = 5\n")
    save_value(path, "volume", 55)
    assert load(path).get("volume") == 55
    assert load(path).get("limit") == 5


def test_save_value_formats_booleans_the_toml_way(tmp_path):
    path = tmp_path / "config.toml"
    save_value(path, "visualizer", False)
    assert path.read_text().strip() == "visualizer = false"


def test_save_value_rejects_bad_input_without_writing(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(ConfigError, match="unknown setting"):
        save_value(path, "nonsense", 1)
    with pytest.raises(ConfigError, match="invalid"):
        save_value(path, "volume", 999)
    assert not path.exists()


def test_save_value_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "config.toml"
    save_value(path, "limit", 3)
    assert path.is_file()
    assert not (tmp_path / "config.toml.tmp").exists()


def test_a_new_config_file_starts_private(tmp_path):
    path = tmp_path / "config.toml"
    save_value(path, "volume", 80)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_value_keeps_the_mode_the_owner_chose(tmp_path):
    """An owner who needs another local account to read the file gets to say so.

    lfm rewrites this file whenever a setting changes, so re-imposing 0600 each
    time would silently undo their `chmod` — and the failure would be invisible.
    """
    path = tmp_path / "config.toml"
    path.write_text('api_key = "secret"\n')
    path.chmod(0o644)
    save_value(path, "volume", 80)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert 'api_key = "secret"' in path.read_text()  # and the contents are still there
    assert "volume = 80" in path.read_text()


def test_the_write_paths_credentials_come_from_the_environment_first(tmp_path, monkeypatch):
    monkeypatch.setenv("LFM_SECRET", "from-the-environment")
    monkeypatch.setenv("LFM_SESSION_KEY", "session-from-the-environment")
    config = load(
        write(
            tmp_path,
            'api_key = "k"\nsecret = "from-the-file"\nsession_key = "session-from-the-file"\n'
            "scrobble = true\n",
        )
    )
    assert config.secret() == "from-the-environment"
    assert config.session_key() == "session-from-the-environment"
    assert config.secret_source() == "$LFM_SECRET"
    assert config.scrobbling() is True


def test_scrobbling_needs_the_setting_the_secret_and_the_session_key(tmp_path):
    """Off means off: any one of the three missing and there is nothing to send with."""
    assert load(write(tmp_path, 'api_key = "k"\nscrobble = true\n')).scrobbling() is False
    assert (
        load(write(tmp_path, 'api_key = "k"\nsecret = "s"\nscrobble = true\n')).scrobbling()
        is False
    )
    everything = load(
        write(tmp_path, 'api_key = "k"\nsecret = "s"\nsession_key = "sk"\nscrobble = true\n')
    )
    assert everything.scrobbling() is True
    assert everything.session_source() == str(tmp_path / "config.toml")


def test_scrobbling_is_off_unless_the_file_says_otherwise(tmp_path):
    """A config file written before scrobbling existed must not start posting."""
    config = load(write(tmp_path, 'api_key = "k"\nsecret = "s"\nsession_key = "sk"\n'))
    assert config.get("scrobble") is False
    assert config.scrobbling() is False


def test_a_malformed_config_says_what_to_do_about_it(tmp_path):
    """`api_key = abc` is the mistake people make; the parser's message does not say so."""
    path = write(tmp_path, "api_key = abcdef0123456789\n")
    with pytest.raises(ConfigError) as caught:
        load(path)
    message = str(caught.value)
    assert str(path) in message  # which file
    assert 'api_key = "' in message  # and how a value has to look
    assert "LFM_API_KEY" in message  # or that the environment avoids the file
