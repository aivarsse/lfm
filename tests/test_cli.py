import json
import stat

import respx
from httpx import Response
from typer.testing import CliRunner

from lfm.api.client import BASE_URL, MAX_LISTENER_LOOKUPS
from lfm.cli import _cache_db, _scrobbling, app
from lfm.config import config_path, load, save_value
from lfm.playback.scrobble import Ledger, Outbox, Play, ledger_path
from lfm.store.cache import Cache

runner = CliRunner()


def full_output(result) -> str:
    """stdout + stderr, whichever way this click version captures them."""
    try:
        return result.output + (result.stderr or "")
    except ValueError:
        return result.output


def test_version_flag_reads_the_working_tree():
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text())["project"]["version"]
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"lfm {expected}"


def test_version_flag_in_help():
    # Scraping the rendered help is terminal-width dependent, so check the
    # registered option instead.
    from typer.main import get_command

    opts = [opt for param in get_command(app).params for opt in param.opts]
    assert "--version" in opts


def test_version_falls_back_to_metadata_without_a_pyproject(monkeypatch):
    monkeypatch.setattr("lfm.cli.__file__", "/nowhere/lfm/cli.py")
    monkeypatch.setattr("lfm.cli.version", lambda name: "9.9.9")
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "lfm 9.9.9"


def test_version_is_unknown_without_an_install(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("lfm.cli.__file__", "/nowhere/lfm/cli.py")
    monkeypatch.setattr("lfm.cli.version", missing)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "lfm unknown"


def mock_similar(fixture_json):
    return respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )


@respx.mock
def test_table_output(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 0
    out = full_output(result)
    assert "→ Burial (corrected)" in out
    assert "MATCH" in out and "ARTIST" in out
    assert "Kode9" in out and "100%" in out
    assert "Zomby" in out and "87%" in out
    assert "Darkstar" in out and "74%" in out


@respx.mock
def test_no_correction_line_when_name_matches_input(isolated_env, fixture_json):
    mock_similar(fixture_json)  # @attr artist is "Burial", input is "Burial"
    result = runner.invoke(app, ["similar", "Burial"])
    assert result.exit_code == 0
    assert "corrected" not in full_output(result)


@respx.mock
def test_plain_output_is_names_only(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "plain"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Kode9", "Zomby", "Darkstar"]


@respx.mock
def test_limit_and_min_match(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "plain", "--min-match", "0.8"])
    assert result.output.splitlines() == ["Kode9", "Zomby"]

    result = runner.invoke(app, ["similar", "burial", "-o", "plain", "-n", "1"])
    assert result.output.splitlines() == ["Kode9"]


@respx.mock
def test_did_you_mean_on_not_found(isolated_env, fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(404, json=fixture_json("not_found.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.search"}).mock(
        return_value=Response(200, json=fixture_json("search_burl.json"))
    )
    result = runner.invoke(app, ["similar", "burl"])
    assert result.exit_code == 1
    out = full_output(result)
    assert 'No similar artists found for "burl"' in out
    assert "Did you mean:" in out
    assert "Burial" in out and "Burl Ives" in out


@respx.mock
def test_all_filtered_by_min_match_says_so(isolated_env):
    # an artist whose neighbours all score below the threshold
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(
            200,
            json={
                "similarartists": {
                    "artist": [{"name": "Obscure", "match": "0.42"}],
                    "@attr": {"artist": "Seeded"},
                }
            },
        )
    )
    result = runner.invoke(app, ["similar", "Seeded", "--min-match", "0.8"])
    assert result.exit_code == 1
    assert "fell below --min-match" in full_output(result)


@respx.mock
def test_api_key_falls_back_to_config_file(tmp_path, monkeypatch, fixture_json):
    monkeypatch.delenv("LFM_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = tmp_path / "config" / "lfm"
    config.mkdir(parents=True)
    (config / "config.toml").write_text('api_key = "from-config"\n')
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "plain"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Kode9", "Zomby", "Darkstar"]


@respx.mock
def test_missing_api_key_fails_cleanly(tmp_path, monkeypatch, fixture_json):
    monkeypatch.delenv("LFM_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 1
    assert "no API key" in full_output(result)


@respx.mock
def test_lastfm_error_fails_cleanly(isolated_env):
    respx.get(BASE_URL).mock(
        return_value=Response(403, json={"error": 10, "message": "Invalid API key"})
    )
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 1
    assert "Last.fm error 10: Invalid API key" in full_output(result)


@respx.mock
def test_network_error_fails_cleanly(isolated_env):
    import httpx

    respx.get(BASE_URL).mock(side_effect=httpx.ConnectError("no route"))
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 1
    assert "cannot reach Last.fm" in full_output(result)


def mock_info(fixture_json):
    """The three lookups the artist card makes."""
    respx.get(BASE_URL, params__contains={"method": "artist.getinfo"}).mock(
        return_value=Response(200, json=fixture_json("info_burial.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.gettoptags"}).mock(
        return_value=Response(200, json=fixture_json("toptags_burial.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.gettoptracks"}).mock(
        return_value=Response(200, json=fixture_json("toptracks_burial.json"))
    )


@respx.mock
def test_info_table(isolated_env, fixture_json):
    mock_info(fixture_json)
    result = runner.invoke(app, ["info", "burial"])
    assert result.exit_code == 0
    out = result.output
    assert "→ Burial (corrected)" in out
    assert "1,642,253 listeners" in out
    assert "dubstep" in out
    assert "Archangel" in out
    assert "William Bevan" in out


@respx.mock
def test_info_json(isolated_env, fixture_json):
    mock_info(fixture_json)
    result = runner.invoke(app, ["info", "burial", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requested"] == "burial"
    assert payload["artist"] == "Burial"
    assert payload["listeners"] == 1642253
    assert payload["tags"] == ["dubstep", "uk garage", "electronic"]
    assert payload["top_tracks"][0] == {"name": "Archangel", "playcount": 1812345}
    assert "Read more" not in payload["bio"]


@respx.mock
def test_info_plain(isolated_env, fixture_json):
    mock_info(fixture_json)
    result = runner.invoke(app, ["info", "Burial", "-o", "plain"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "Burial"
    assert "1,642,253 listeners" in lines[1]
    assert "Archangel\t1812345" in lines


@respx.mock
def test_info_links(isolated_env, fixture_json):
    mock_info(fixture_json)
    result = runner.invoke(app, ["info", "Burial", "-o", "links"])
    assert result.exit_code == 0
    assert result.output.strip() == "https://www.last.fm/music/Burial"


@respx.mock
def test_info_unknown_artist_offers_suggestions(isolated_env, fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getinfo"}).mock(
        return_value=Response(404, json=fixture_json("not_found.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.search"}).mock(
        return_value=Response(200, json=fixture_json("search_burl.json"))
    )
    result = runner.invoke(app, ["info", "burl"])
    assert result.exit_code == 1
    out = full_output(result)
    assert 'No artist found for "burl"' in out
    assert "Did you mean" in out and "Burial" in out


def test_recent_is_empty_at_first(isolated_env):
    result = runner.invoke(app, ["recent"])
    assert result.exit_code == 1
    assert "no artists yet" in full_output(result)


@respx.mock
def test_a_similar_lookup_is_remembered(isolated_env, fixture_json):
    mock_similar(fixture_json)
    assert runner.invoke(app, ["similar", "burial", "-o", "plain"]).exit_code == 0

    result = runner.invoke(app, ["recent"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Burial"]  # the corrected name, not "burial"


@respx.mock
def test_an_info_lookup_is_remembered(isolated_env, fixture_json):
    mock_info(fixture_json)
    assert runner.invoke(app, ["info", "Burial", "-o", "plain"]).exit_code == 0
    assert runner.invoke(app, ["recent"]).output.splitlines() == ["Burial"]


@respx.mock
def test_recent_honours_the_limit(isolated_env, fixture_json):
    # the specific route has to be registered first: respx takes the first match
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar", "artist": "bonobo"}).mock(
        return_value=Response(200, json=fixture_json("similar_bonobo.json"))
    )
    mock_similar(fixture_json)  # everything else -> Burial
    for artist in ("burial", "bonobo"):
        runner.invoke(app, ["similar", artist, "-o", "plain"])

    result = runner.invoke(app, ["recent", "-n", "1"])
    assert result.output.splitlines() == ["Bonobo"]  # the most recent one


@respx.mock
def test_similar_json(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requested"] == "burial"
    assert payload["artist"] == "Burial"
    assert [r["name"] for r in payload["results"]] == ["Kode9", "Zomby", "Darkstar"]
    assert payload["results"][0]["match"] == 1.0
    assert payload["results"][0]["url"] == "https://www.last.fm/music/Kode9"


@respx.mock
def test_similar_links(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "links"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "https://www.last.fm/music/Kode9",
        "https://www.last.fm/music/Zomby",
        "https://www.last.fm/music/Darkstar",
    ]


def mock_blend(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar", "artist": "burial"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar", "artist": "bonobo"}).mock(
        return_value=Response(200, json=fixture_json("similar_bonobo.json"))
    )


@respx.mock
def test_blend_ranks_by_geometric_mean(isolated_env, fixture_json):
    mock_blend(fixture_json)
    result = runner.invoke(app, ["blend", "burial", "bonobo", "-o", "plain"])
    assert result.exit_code == 0
    # Kode9 is the only artist both seeds know, so it wins; the rest are known to
    # one seed and get the epsilon for the other, ordered by their match.
    assert result.output.splitlines() == ["Kode9", "Four Tet", "Zomby", "Darkstar"]


@respx.mock
def test_blend_json_includes_per_seed_matches(isolated_env, fixture_json):
    mock_blend(fixture_json)
    result = runner.invoke(app, ["blend", "burial", "bonobo", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["seeds"] == ["burial", "bonobo"]
    top = payload["results"][0]
    assert top["name"] == "Kode9"
    assert top["matches"] == {"burial": 1.0, "bonobo": 0.5}
    names = [r["name"] for r in payload["results"]]
    assert "Burial" not in names and "Bonobo" not in names  # seeds are not answers


@respx.mock
def test_blend_table(isolated_env, fixture_json):
    mock_blend(fixture_json)
    result = runner.invoke(app, ["blend", "burial", "bonobo"])
    assert result.exit_code == 0
    out = result.output
    assert "Blend of burial + bonobo" in out
    assert "SCORE" in out and "ARTIST" in out
    assert "Kode9" in out and "71%" in out  # sqrt(1.0 * 0.5)


@respx.mock
def test_blend_links(isolated_env, fixture_json):
    mock_blend(fixture_json)
    result = runner.invoke(app, ["blend", "burial", "bonobo", "-o", "links"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "https://www.last.fm/music/Kode9"


def test_blend_needs_two_seeds(isolated_env):
    result = runner.invoke(app, ["blend", "burial"])
    assert result.exit_code == 1
    assert "at least two artists" in full_output(result)


@respx.mock
def test_blend_reports_an_unknown_seed(isolated_env, fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar", "artist": "burial"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar", "artist": "nope"}).mock(
        return_value=Response(404, json=fixture_json("not_found.json"))
    )
    respx.get(BASE_URL, params__contains={"method": "artist.search"}).mock(
        return_value=Response(200, json=fixture_json("search_burl.json"))
    )
    result = runner.invoke(app, ["blend", "burial", "nope"])
    assert result.exit_code == 1
    out = full_output(result)
    assert 'No artist found for "nope"' in out
    assert "Did you mean" in out


def mock_similar_with_tags(fixture_json):
    """similar plus the tag lookups --why triggers (same fixture for every artist)."""
    route = mock_similar(fixture_json)
    respx.get(BASE_URL, params__contains={"method": "artist.gettoptags"}).mock(
        return_value=Response(200, json=fixture_json("toptags_burial.json"))
    )
    return route


@respx.mock
def test_similar_why_adds_a_shared_column(isolated_env, fixture_json):
    mock_similar_with_tags(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "--why"])
    assert result.exit_code == 0
    out = result.output
    assert "SHARED" in out
    assert "dubstep, uk garage, electronic" in out


@respx.mock
def test_similar_why_puts_shared_tags_in_the_json(isolated_env, fixture_json):
    mock_similar_with_tags(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "--why", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["results"][0]["shared_tags"] == ["dubstep", "uk garage", "electronic"]


@respx.mock
def test_no_why_means_no_tag_lookups(isolated_env, fixture_json):
    route = respx.get(BASE_URL, params__contains={"method": "artist.gettoptags"}).mock(
        return_value=Response(200, json=fixture_json("toptags_burial.json"))
    )
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "json"])
    assert result.exit_code == 0
    assert route.call_count == 0
    assert "shared_tags" not in json.loads(result.output)["results"][0]


@respx.mock
def test_tui_command_wires_the_app(isolated_env, monkeypatch):
    seen = {}

    def fake_run(self):
        seen["artist"] = self.artist
        seen["limit"] = self.limit
        seen["min_match"] = self.min_match
        seen["visualizer"] = self.spectrum is not None
        seen["save_volume"] = self.save_volume is not None

    monkeypatch.setattr("lfm.ui.app.LfmApp.run", fake_run)
    result = runner.invoke(app, ["tui", "burial", "-n", "5", "--min-match", "0.3"])
    assert result.exit_code == 0
    assert seen == {
        "artist": "burial",
        "limit": 5,
        "min_match": 0.3,
        "visualizer": True,
        "save_volume": True,
    }


def test_tui_command_can_disable_the_visualizer(isolated_env, monkeypatch):
    seen = {}

    def fake_run(self):
        seen["visualizer"] = self.spectrum is not None

    monkeypatch.setattr("lfm.ui.app.LfmApp.run", fake_run)
    result = runner.invoke(app, ["tui", "burial", "--no-visualizer"])
    assert result.exit_code == 0
    assert seen == {"visualizer": False}


def test_config_command_shows_path_and_values(isolated_env):
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "config.toml" in result.output
    assert "not found" in result.output
    assert "limit = 20" in result.output
    assert "api_key = $LFM_API_KEY" in result.output


def test_config_command_never_prints_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LFM_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config_dir = tmp_path / "config" / "lfm"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('api_key = "SECRET"\n')
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "SECRET" not in result.output
    assert "api_key" in result.output


@respx.mock
def test_config_file_supplies_defaults(tmp_path, monkeypatch, fixture_json):
    monkeypatch.delenv("LFM_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config_dir = tmp_path / "config" / "lfm"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'api_key = "file-key"\nlimit = 2\nmin_match = 0.9\noutput = "plain"\n'
    )
    route = respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Kode9"]  # plain + limit 2 + min-match 0.9
    assert route.calls.last.request.url.params["api_key"] == "file-key"


@respx.mock
def test_cli_flag_beats_the_config_file(tmp_path, monkeypatch, fixture_json):
    monkeypatch.delenv("LFM_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config_dir = tmp_path / "config" / "lfm"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('api_key = "k"\nlimit = 1\n')
    mock_similar(fixture_json)
    result = runner.invoke(app, ["similar", "burial", "-o", "plain", "-n", "2"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Kode9", "Zomby"]


def test_broken_config_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("LFM_API_KEY", "test-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config_dir = tmp_path / "config" / "lfm"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("limit = \n")
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 1
    assert "cannot read" in full_output(result)


def test_invalid_config_value_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("LFM_API_KEY", "test-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config_dir = tmp_path / "config" / "lfm"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("limit = 0\n")
    result = runner.invoke(app, ["similar", "burial"])
    assert result.exit_code == 1
    assert "invalid 'limit'" in full_output(result)


def test_tui_command_without_artist_prompts(isolated_env, monkeypatch):
    seen = {}

    def fake_run(self):
        seen["artist"] = self.artist

    monkeypatch.setattr("lfm.ui.app.LfmApp.run", fake_run)
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 0
    assert seen == {"artist": None}


def test_cache_command_says_nothing_is_cached_yet(isolated_env):
    result = runner.invoke(app, ["cache"])
    assert result.exit_code == 0
    assert result.output.strip() == "nothing cached yet"


def test_cache_command_lists_the_entries_and_their_expiry(isolated_env):
    with Cache(_cache_db()) as store:
        store.set("fresh", "payload", ttl=60)
        store.set("stale", "payload", ttl=0)
    result = runner.invoke(app, ["cache"])
    assert result.exit_code == 0
    path_line, counts = result.output.splitlines()
    assert path_line.startswith(str(_cache_db()))
    assert "KB" in path_line  # so you can see whether clearing is worth it
    assert counts.strip() == "2 entries, 1 expired"


def test_cache_command_reports_a_database_it_cannot_open(isolated_env):
    _cache_db().parent.mkdir(parents=True, exist_ok=True)
    _cache_db().write_bytes(b"not a database")
    result = runner.invoke(app, ["cache"])
    assert result.exit_code == 1
    assert "cannot open" in full_output(result)


def test_cache_command_switches_to_mb_for_a_big_cache(isolated_env):
    with Cache(_cache_db()) as store:
        store.set("big", "x" * 2_000_000, ttl=60)
    result = runner.invoke(app, ["cache"])
    assert "MB" in result.output.splitlines()[0]


def test_cache_clear_empties_it(isolated_env):
    with Cache(_cache_db()) as store:
        store.set("a", "payload", ttl=60)
    result = runner.invoke(app, ["cache", "--clear"])
    assert result.exit_code == 0
    assert result.output.strip() == "cleared 1 entry"  # singular, not 1 entries
    with Cache(_cache_db()) as store:
        assert store.counts() == (0, 0)


def test_cache_prune_keeps_the_fresh_entries(isolated_env):
    with Cache(_cache_db()) as store:
        store.set("fresh", "payload", ttl=60)
        store.set("stale", "payload", ttl=0)
    result = runner.invoke(app, ["cache", "--prune"])
    assert result.exit_code == 0
    assert result.output.strip() == "pruned 1 expired entry"
    with Cache(_cache_db()) as store:
        assert store.counts() == (1, 1)  # what was still fresh is still there


def test_cache_prune_on_nothing_cached_says_so(isolated_env):
    result = runner.invoke(app, ["cache", "--prune"])
    assert result.exit_code == 0
    assert result.output.strip() == "nothing cached yet"


def test_cache_clear_and_prune_together_are_refused(isolated_env):
    """One throws everything away and the other keeps the fresh half; asking for both is a bug."""
    with Cache(_cache_db()) as store:
        store.set("fresh", "payload", ttl=60)
    result = runner.invoke(app, ["cache", "--clear", "--prune"])
    assert result.exit_code == 1
    assert "ask for one" in full_output(result)
    with Cache(_cache_db()) as store:
        assert store.counts() == (1, 1)  # refused before anything was touched


@respx.mock
def test_walk_table_names_the_start_and_counts_the_hops(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["walk", "Burial"])
    assert result.exit_code == 0
    out = full_output(result)
    assert "Walk from Burial" in out
    assert "SCORE" in out and "HOPS" in out
    assert "Kode9" in out


@respx.mock
def test_walk_asks_each_artist_once_and_no_more(isolated_env, fixture_json):
    """The fixture answers the same three names for every artist, so a walk that
    followed them twice would be visible here as extra calls."""
    route = mock_similar(fixture_json)
    assert runner.invoke(app, ["walk", "Burial", "--depth", "2"]).exit_code == 0
    assert route.call_count == 1 + 3  # the seed, then each artist it named, once each


@respx.mock
def test_walk_at_one_hop_asks_about_the_seed_alone(isolated_env, fixture_json):
    route = mock_similar(fixture_json)
    assert runner.invoke(app, ["walk", "Burial", "--depth", "1"]).exit_code == 0
    assert route.call_count == 1


def test_walk_refuses_a_depth_it_cannot_afford(isolated_env):
    # A third hop multiplies the requests by the fan-out, so the option refuses rather
    # than quietly spending four hundred calls.
    result = runner.invoke(app, ["walk", "Burial", "--depth", "3"])
    assert result.exit_code != 0


@respx.mock
def test_walk_json_carries_the_score_and_the_hops(isolated_env, fixture_json):
    mock_similar(fixture_json)
    result = runner.invoke(app, ["walk", "Burial", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert (payload["requested"], payload["depth"]) == ("Burial", 2)
    first = payload["results"][0]
    assert first["name"] == "Kode9"  # the strongest path wins
    assert first["hops"] == 1  # and the closest path to it is the one reported
    # The fixture answers the same three names for every artist, so Kode9's 0.5 from the
    # seed is joined by the two paths that reach it at the second hop: 0.218 from Zomby
    # and 0.1845 from Darkstar.
    assert first["score"] == 0.9025
    assert first["url"].startswith("https://www.last.fm/music/")


@respx.mock
def test_walk_plain_and_links_are_one_line_each(isolated_env, fixture_json):
    mock_similar(fixture_json)
    plain = runner.invoke(app, ["walk", "Burial", "-o", "plain"])
    assert plain.exit_code == 0
    assert plain.output.splitlines()[0] == "Kode9"
    links = runner.invoke(app, ["walk", "Burial", "-o", "links"])
    assert links.exit_code == 0
    assert links.output.splitlines()[0] == "https://www.last.fm/music/Kode9"


@respx.mock
def test_walk_says_so_when_there_is_nothing_around_the_artist(isolated_env):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json={"similarartists": {"artist": [], "@attr": {}}})
    )
    result = runner.invoke(app, ["walk", "Hermit"])
    assert result.exit_code == 1
    assert "Nothing found around" in full_output(result)


@respx.mock
def test_a_second_run_is_answered_from_the_cache(isolated_env, fixture_json):
    route = mock_similar(fixture_json)
    assert runner.invoke(app, ["similar", "burial", "-o", "plain"]).exit_code == 0
    assert runner.invoke(app, ["similar", "burial", "-o", "plain"]).exit_code == 0
    assert route.call_count == 1


@respx.mock
def test_no_cache_neither_reads_nor_writes(isolated_env, fixture_json):
    route = mock_similar(fixture_json)
    for _ in range(2):
        result = runner.invoke(app, ["--no-cache", "similar", "burial", "-o", "plain"])
        assert result.exit_code == 0
    assert route.call_count == 2  # Last.fm answered twice; the cache never did
    assert not _cache_db().exists()  # and nothing was stored for next time


def test_tui_volume_flag_sets_and_remembers_the_level(isolated_env, monkeypatch):
    seen = {}

    def fake_run(self):
        seen["volume"] = self.player.volume
        # the app the command built is the one holding the player

    monkeypatch.setattr("lfm.ui.app.LfmApp.run", fake_run)
    result = runner.invoke(app, ["tui", "burial", "--volume", "40"])
    assert result.exit_code == 0
    assert seen["volume"] == 40.0

    from lfm.config import load

    assert load().get("volume") == 40  # so the next session starts there


def test_tui_volume_flag_is_bounded(isolated_env):
    result = runner.invoke(app, ["tui", "burial", "--volume", "200"])
    assert result.exit_code != 0
    assert "volume" in full_output(result).lower()


def test_a_volume_that_cannot_be_saved_is_reported(isolated_env, monkeypatch):
    """A read-only config must not stop lfm, but losing the level should be said."""
    from lfm.config import config_path

    config_dir = config_path().parent
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o500)  # the file itself is replaceable only via the directory
    seen = {}

    def fake_run(self):
        seen["first"] = self.save_volume(80)
        seen["second"] = self.save_volume(85)  # said once; it must not nag

    monkeypatch.setattr("lfm.ui.app.LfmApp.run", fake_run)
    try:
        result = runner.invoke(app, ["tui", "burial"])
    finally:
        config_dir.chmod(0o700)
    assert result.exit_code == 0
    assert "cannot write" in (seen["first"] or "")
    assert seen["second"] is None


def test_the_volume_flag_warns_when_it_cannot_be_remembered(isolated_env, monkeypatch):
    from lfm.config import config_path

    config_dir = config_path().parent
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o500)
    seen = {}

    def fake_run(self):
        seen["volume"] = self.player.volume

    monkeypatch.setattr("lfm.ui.app.LfmApp.run", fake_run)
    try:
        result = runner.invoke(app, ["tui", "burial", "--volume", "40"])
    finally:
        config_dir.chmod(0o700)
    assert result.exit_code == 0
    assert seen["volume"] == 40.0  # the level still applies for this session
    assert "warning:" in full_output(result)


def mock_listener_counts(counts: dict[str, int]):
    """artist.getinfo per artist, so a deep-cut run has something to chew on."""

    def handler(request):
        artist = request.url.params["artist"]
        return Response(
            200,
            json={"artist": {"name": artist, "stats": {"listeners": counts.get(artist, 0)}}},
        )

    return respx.get(BASE_URL, params__contains={"method": "artist.getinfo"}).mock(
        side_effect=handler
    )


@respx.mock
def test_max_listeners_keeps_only_the_deep_cuts(isolated_env, fixture_json):
    mock_similar(fixture_json)
    info_route = mock_listener_counts({"Kode9": 900_000, "Zomby": 40_000, "Darkstar": 12_000})

    result = runner.invoke(app, ["similar", "burial", "--max-listeners", "50000"])

    assert result.exit_code == 0
    assert "LISTENERS" in result.output  # the column that says why they qualify
    assert "Kode9" not in result.output  # 900k listeners is not a deep cut
    assert "Zomby" in result.output and "40,000" in result.output
    assert "Darkstar" in result.output
    assert info_route.call_count == 3  # one lookup per row shown, no more


@respx.mock
def test_a_plain_lookup_never_asks_for_listener_counts(isolated_env, fixture_json):
    mock_similar(fixture_json)
    info_route = mock_listener_counts({"Kode9": 1})

    result = runner.invoke(app, ["similar", "burial"])

    assert result.exit_code == 0
    assert info_route.call_count == 0  # the flag is what pays for the extra calls


@respx.mock
def test_deep_cuts_carry_their_count_into_json(isolated_env, fixture_json):
    mock_similar(fixture_json)
    mock_listener_counts({"Kode9": 900_000, "Zomby": 40_000, "Darkstar": 12_000})

    result = runner.invoke(app, ["similar", "burial", "--max-listeners", "50000", "-o", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [row["name"] for row in payload["results"]] == ["Zomby", "Darkstar"]
    assert [row["listeners"] for row in payload["results"]] == [40_000, 12_000]


@respx.mock
def test_no_deep_cuts_is_said_rather_than_shown_as_empty(isolated_env, fixture_json):
    mock_similar(fixture_json)
    mock_listener_counts({"Kode9": 900_000, "Zomby": 40_000, "Darkstar": 12_000})

    result = runner.invoke(app, ["similar", "burial", "--max-listeners", "100"])

    assert result.exit_code == 1
    assert "No deep cuts under 100 listeners." in full_output(result)


@respx.mock
def test_a_long_list_is_truncated_rather_than_hammering_the_api(isolated_env):
    artists = [{"name": f"Artist {i:02d}", "match": "0.5"} for i in range(40)]
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json={"similarartists": {"artist": artists}})
    )
    info_route = mock_listener_counts({f"Artist {i:02d}": 100 for i in range(40)})

    result = runner.invoke(app, ["similar", "burial", "--max-listeners", "50000", "-n", "40"])

    assert result.exit_code == 0
    assert info_route.call_count == MAX_LISTENER_LOOKUPS
    assert f"checking the {MAX_LISTENER_LOOKUPS} most similar of 40" in full_output(result)


def mock_similar_tracks(fixture_json):
    return respx.get(BASE_URL, params__contains={"method": "track.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_tracks_burial.json"))
    )


@respx.mock
def test_radio_lists_what_is_close_and_leaves_the_seed_out(isolated_env, fixture_json):
    mock_similar_tracks(fixture_json)
    result = runner.invoke(app, ["radio", "Burial", "Archangel"])
    assert result.exit_code == 0
    assert "Jamie Woon" in result.output
    assert "Etched Headplate" in result.output
    assert "0.87" in result.output  # the raw score, not a percent
    # the seed comes back as the top match, and there is a duplicate in there
    assert result.output.count("Etched Headplate") == 1
    assert result.output.count("Archangel") == 1  # the header, not a row


@respx.mock
def test_radio_honours_the_limit(isolated_env, fixture_json):
    mock_similar_tracks(fixture_json)
    result = runner.invoke(app, ["radio", "Burial", "Archangel", "-n", "1"])
    assert result.exit_code == 0
    assert "Etched Headplate" in result.output
    assert "Jamie Woon" not in result.output


@respx.mock
def test_radio_plain_is_tab_separated_for_pipes(isolated_env, fixture_json):
    mock_similar_tracks(fixture_json)
    result = runner.invoke(app, ["radio", "Burial", "Archangel", "-o", "plain"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Burial\tEtched Headplate", "Jamie Woon\tNight Air"]


@respx.mock
def test_radio_json_carries_the_match_and_the_url(isolated_env, fixture_json):
    mock_similar_tracks(fixture_json)
    result = runner.invoke(app, ["radio", "Burial", "Archangel", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requested"] == {"artist": "Burial", "track": "Archangel"}
    assert payload["results"][0] == {
        "name": "Etched Headplate",
        "artist": "Burial",
        "match": 0.87,
        "url": "https://www.last.fm/music/Burial/_/Etched+Headplate",
    }


@respx.mock
def test_radio_links_falls_back_to_the_track_url(isolated_env, fixture_json):
    mock_similar_tracks(fixture_json)
    result = runner.invoke(app, ["radio", "Burial", "Archangel", "-o", "links"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "https://www.last.fm/music/Burial/_/Etched+Headplate"


@respx.mock
def test_radio_says_so_when_the_seed_is_unknown(isolated_env):
    respx.get(BASE_URL, params__contains={"method": "track.getsimilar"}).mock(
        return_value=Response(
            200, json={"error": 6, "message": "The track you supplied could not be found"}
        )
    )
    respx.get(BASE_URL, params__contains={"method": "artist.search"}).mock(
        return_value=Response(200, json={"results": {"artistmatches": {"artist": []}}})
    )
    result = runner.invoke(app, ["radio", "Nobody", "Nothing"])
    assert result.exit_code == 1
    assert "No track found" in full_output(result)


# --- scrobbling: the queue, sending it, and backing out ---------------------


def queue_two() -> Outbox:
    outbox = Outbox()
    outbox.add(Play("Burial", "Archangel", 1_700_000_000, "Untrue", 239))
    outbox.add(Play("Burial", "Near Dark", 1_700_000_300, "Untrue", 217))
    return outbox


def signed_in(monkeypatch) -> None:
    """The two credentials the write path needs, without a config file."""
    monkeypatch.setenv("LFM_SECRET", "test-secret")
    monkeypatch.setenv("LFM_SESSION_KEY", "test-session")


def scrobbling_on() -> None:
    save_value(config_path(), "scrobble", True)


def test_scrobble_shows_what_is_waiting_and_sends_nothing(isolated_env):
    queue_two()
    result = runner.invoke(app, ["scrobble", "--dry-run"])
    assert result.exit_code == 0
    assert "Burial — Archangel  ·  Untrue" in result.output
    assert "2 waiting" in result.output
    assert len(Outbox().entries()) == 2  # shown, not sent, not dropped


def test_scrobble_drop_throws_the_queue_away(isolated_env):
    queue_two()
    result = runner.invoke(app, ["scrobble", "--drop"])
    assert result.exit_code == 0
    assert result.output.strip() == "dropped 2 waiting"
    assert Outbox().entries() == []


def test_scrobble_says_what_is_missing_rather_than_just_failing(isolated_env):
    result = runner.invoke(app, ["scrobble"])
    assert result.exit_code == 1
    assert "missing scrobble = true, secret, session_key (lfm auth)" in full_output(result)


def test_scrobble_off_switches_it_off_and_leaves_the_queue_alone(isolated_env):
    save_value(config_path(), "scrobble", True)
    queue_two()
    result = runner.invoke(app, ["scrobble", "--off"])
    assert result.exit_code == 0
    assert load(config_path()).get("scrobble") is False
    # Off stops what goes out; it does not decide what is thrown away.
    assert len(Outbox().entries()) == 2


@respx.mock
def test_scrobble_sends_what_is_waiting_and_says_where_the_record_is(isolated_env, monkeypatch):
    signed_in(monkeypatch)
    scrobbling_on()
    route = respx.post(BASE_URL).mock(
        return_value=Response(200, json={"scrobbles": {"@attr": {"accepted": "2", "ignored": "0"}}})
    )
    queue_two()
    result = runner.invoke(app, ["scrobble"])
    assert result.exit_code == 0
    assert route.call_count == 1  # both plays in one signed request
    assert "scrobbled 2" in result.output
    assert str(ledger_path()) in result.output  # the list to delete from, by hand
    assert Outbox().entries() == []
    assert [row["status"] for row in Ledger().entries()] == ["accepted", "accepted"]


@respx.mock
def test_a_failed_send_reports_it_and_keeps_the_queue(isolated_env, monkeypatch):
    signed_in(monkeypatch)
    scrobbling_on()
    respx.post(BASE_URL).mock(
        return_value=Response(400, json={"error": 16, "message": "There was a temporary error"})
    )
    queue_two()
    result = runner.invoke(app, ["scrobble"])
    assert result.exit_code == 1
    assert "temporary" in full_output(result).lower()
    assert len(Outbox().entries()) == 2  # nothing is thrown away over a blip


def test_auth_without_a_secret_says_where_one_belongs(isolated_env):
    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "LFM_SECRET" in full_output(result)


@respx.mock
def test_auth_approves_in_a_browser_and_keeps_what_came_back(isolated_env, monkeypatch):
    monkeypatch.setenv("LFM_SECRET", "test-secret")
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    respx.post(BASE_URL).mock(
        side_effect=[
            Response(200, json={"token": "a-token"}),
            Response(
                200,
                json={"session": {"name": "someone", "key": "a-session", "subscriber": "0"}},
            ),
        ]
    )
    result = runner.invoke(app, ["auth"], input="\n")
    assert result.exit_code == 0
    assert "https://www.last.fm/api/auth/?api_key=test-key&token=a-token" in result.output
    assert opened and "a-token" in opened[0]  # the browser was pointed at it
    assert "authorised as someone" in result.output
    saved = load(config_path())
    assert saved.session_key() == "a-session"
    assert saved.get("scrobble") is True  # authorising is asking for it


@respx.mock
def test_auth_reports_a_signature_last_fm_refused(isolated_env, monkeypatch):
    monkeypatch.setenv("LFM_SECRET", "the-wrong-accounts-secret")
    respx.post(BASE_URL).mock(
        return_value=Response(
            400, json={"error": 13, "message": "Invalid method signature supplied"}
        )
    )
    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "same Last.fm API account" in full_output(result)


def test_config_never_prints_a_credential_only_where_it_comes_from(isolated_env, monkeypatch):
    monkeypatch.setenv("LFM_SECRET", "a-secret-that-must-not-be-printed")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "secret = $LFM_SECRET" in result.output
    assert "session_key = not set" in result.output
    assert "scrobbling = off" in result.output
    assert "a-secret-that-must-not-be-printed" not in result.output


def test_the_tui_is_handed_a_write_path_only_when_all_of_it_is_there(isolated_env, monkeypatch):
    assert _scrobbling(load(config_path())) is None  # nothing configured
    signed_in(monkeypatch)
    assert _scrobbling(load(config_path())) is None  # credentials, but off
    save_value(config_path(), "scrobble", True)
    assert _scrobbling(load(config_path())) is not None
    monkeypatch.delenv("LFM_SESSION_KEY")
    assert _scrobbling(load(config_path())) is None  # a missing piece is enough


def hand_written_config(text: str, mode: int) -> None:
    """A config file the owner made themselves, at whatever mode they chose."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def test_config_reports_the_mode_rather_than_policing_it(isolated_env):
    hand_written_config('api_key = "k"\nsecret = "s"\n', 0o644)
    shown = full_output(runner.invoke(app, ["config"]))
    assert "mode 0644" in shown
    assert "warning" not in shown.lower()  # the owner's choice, reported not scolded

    config_path().chmod(0o600)
    assert "mode 0600" in full_output(runner.invoke(app, ["config"]))


def test_config_reports_the_mode_even_without_a_credential_in_the_file(isolated_env):
    hand_written_config('api_key = "k"\n', 0o644)
    # Nothing secret in it, and the mode is still a fact worth showing.
    assert "mode 0644" in full_output(runner.invoke(app, ["config"]))


@respx.mock
def test_auth_warns_about_a_readable_file_before_putting_more_in_it(isolated_env, monkeypatch):
    hand_written_config('api_key = "k"\nsecret = "s"\n', 0o644)
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    respx.post(BASE_URL).mock(
        side_effect=[
            Response(200, json={"token": "a-token"}),
            Response(200, json={"session": {"name": "someone", "key": "a-session"}}),
        ]
    )
    result = runner.invoke(app, ["auth"], input="\n")
    assert result.exit_code == 0
    assert "another local user can read" in full_output(result)
    assert "chmod o-r" in full_output(result)  # the minimal change, not a lecture
    # And the mode the owner chose survived the write that stored the session key.
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o644
    assert load(config_path()).session_key() == "a-session"
