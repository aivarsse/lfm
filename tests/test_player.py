import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from lfm.playback import player as player_module
from lfm.playback.player import (
    INSTALL_HINT,
    PLAY_ATTEMPTS,
    VOLUME_MAX,
    MpvPlayer,
    PlayResult,
    YtDlpError,
    YtResult,
    format_time,
    mpv_cmd,
    parse_resolve,
    parse_search,
    resolve_cmd,
    search_cmd,
    search_query,
    search_tracks,
    set_pdeathsig,
    spawn_kwargs,
    stream_url_cmd,
)

# A stand-in for mpv that speaks enough of its JSON IPC protocol to drive the
# player. FAKE_MPV_MODE selects the behaviour under test:
#   ok    playback progresses (time-pos starts at 1.0)
#   stuck mpv stays up but never advances (broken audio/network)
#   exit  mpv dies early with an error on stderr, like a missing sound device
#   eof   playback is over but mpv is still alive (keep-open): eof-reached is set
FAKE_MPV = """#!{python}
import asyncio, json, os, sys

mode = os.environ.get("FAKE_MPV_MODE", "ok")
state = {{
    "time-pos": 0.0 if mode in ("stuck", "exit") else 1.0,
    "duration": 100.0,
    "pause": False,
    "volume": 100.0,
    "eof-reached": mode == "eof",
}}
socket_path = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--input-ipc-server="))

# Fail the first N runs, to exercise the retry on a refused stream URL.
fail_times = int(os.environ.get("FAKE_MPV_FAIL_TIMES", "0"))
counter = os.environ.get("FAKE_MPV_COUNTER")
if fail_times and counter:
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as handle:
        handle.write(str(seen + 1))
    if seen < fail_times:
        print("https: HTTP error 403 Forbidden", file=sys.stderr)
        sys.exit(2)

async def handle(reader, writer):
    while True:
        line = await reader.readline()
        if not line:
            break
        msg = json.loads(line)
        cmd = msg.get("command", [])
        rid = msg.get("request_id")
        name = cmd[0] if cmd else ""
        if name == "get_property":
            key = cmd[1]
            if key in state:
                reply = {{"request_id": rid, "data": state[key], "error": "success"}}
            else:
                reply = {{"request_id": rid, "error": "property unavailable"}}
        elif name == "cycle":
            state["pause"] = not state["pause"]
            reply = {{"request_id": rid, "error": "success"}}
        elif name == "seek":
            state["time-pos"] += cmd[1]
            reply = {{"request_id": rid, "error": "success"}}
        elif name == "set_property":
            state[cmd[1]] = cmd[2]
            reply = {{"request_id": rid, "error": "success"}}
        else:
            reply = {{"request_id": rid, "error": "unknown command"}}
        writer.write(json.dumps(reply).encode() + b"\\n")
        await writer.drain()
        if name == "quit":
            os._exit(0)
    writer.close()

async def main():
    server = await asyncio.start_unix_server(handle, path=socket_path)
    if mode == "exit":
        await asyncio.sleep(0.2)
        print("couldn't open play stream: No such file or directory", file=sys.stderr)
        sys.stderr.flush()
        os._exit(2)
    async with server:
        await server.serve_forever()

asyncio.run(main())
"""


@pytest.fixture
def fake_mpv(tmp_path: Path) -> str:
    script = tmp_path / "fake-mpv"
    script.write_text(FAKE_MPV.format(python=sys.executable))
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def fake_ytdlp(tmp_path: Path) -> str:
    script = tmp_path / "fake-ytdlp"
    script.write_text("#!/bin/sh\nprintf 'Archangel\\thttps://example.invalid/audio\\n'\n")
    script.chmod(0o755)
    return str(script)


def test_search_query_is_artist_dash_track():
    assert search_query("Burial", "Archangel") == "Burial - Archangel"


def test_resolve_cmd_searches_youtube_music():
    cmd = resolve_cmd("Burial - Archangel", "yt-dlp")
    assert "ytsearch1:Burial - Archangel" in cmd
    assert cmd[0] == "yt-dlp"


def test_parse_resolve_reads_one_tab_separated_line():
    assert parse_resolve("Archangel\thttps://example.invalid/a\n") == (
        "Archangel",
        "https://example.invalid/a",
    )


def test_parse_resolve_ignores_lines_without_a_url():
    # a title containing a newline cannot masquerade as a url
    with pytest.raises(ValueError):
        parse_resolve("Archangel\nhttps://example.invalid/a\n")


def test_parse_resolve_rejects_empty_output():
    with pytest.raises(ValueError):
        parse_resolve("\n")


def test_mpv_cmd_opens_an_ipc_socket_and_keeps_errors():
    cmd = mpv_cmd("https://example.invalid/a", Path("/tmp/mpv.sock"), "mpv", 70)
    assert cmd[0] == "mpv"
    assert "--input-ipc-server=/tmp/mpv.sock" in cmd
    assert cmd[-1] == "https://example.invalid/a"
    # warnings too: the intermittent "HTTP error 403" is logged at warning level
    assert "--msg-level=all=warn" in cmd
    # mpv's manual asks for this when it is embedded, so a user's mpv.conf,
    # scripts or key bindings cannot change what lfm plays
    assert "--no-config" in cmd
    assert "--volume=70" in cmd  # the session level is applied to every new mpv
    # EBU R128 in one pass: the sources lfm plays are mastered at wildly different
    # levels, and the streams carry no ReplayGain tags to read
    assert "--af=lavfi=[loudnorm=I=-16:TP=-1.5:LRA=11]" in cmd
    assert "--vid=no" in cmd  # documented spelling over the --no-video alias
    # mpv's own default is bestvideo+bestaudio/best, which would fetch video
    assert "--ytdl-format=bestaudio/best" in cmd


def test_format_time():
    assert format_time(0) == "0:00"
    assert format_time(65) == "1:05"
    assert format_time(-3) == "0:00"
    assert format_time(3600) == "60:00"


async def test_missing_binaries_give_install_hint():
    player = MpvPlayer(mpv="/nonexistent/mpv", ytdlp="/nonexistent/yt-dlp")
    result = await player.play("Burial", "Archangel")
    assert result.ok is False
    assert result.message == INSTALL_HINT


async def test_resolve_failure_reports_stderr(tmp_path: Path):
    failing = tmp_path / "bad-ytdlp"
    failing.write_text("#!/bin/sh\necho 'no results found' >&2\nexit 1\n")
    failing.chmod(0o755)
    player = MpvPlayer(mpv="/bin/true", ytdlp=str(failing))
    result = await player.play("Burial", "Archangel")
    assert result.ok is False
    assert "no results found" in result.message


async def test_end_of_stream_counts_as_finished(fake_mpv: str, fake_ytdlp: str, monkeypatch):
    """mpv may stay alive at EOF (keep-open); that must not look like playing."""
    monkeypatch.setenv("FAKE_MPV_MODE", "eof")
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    assert (await player.play("Burial", "Archangel")).ok is True
    status = await player.status()
    assert status.playing is False
    assert status.finished is True  # it got to the end, so the next one may follow
    await player.stop()


async def test_a_failed_load_is_not_a_finish(fake_mpv: str, fake_ytdlp: str, monkeypatch):
    monkeypatch.setenv("FAKE_MPV_MODE", "exit")
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    assert (await player.play("Burial", "Archangel")).ok is False
    status = await player.status()
    assert status.playing is False
    assert status.finished is False  # nothing finished, so nothing should play on


async def test_a_stopped_track_is_not_a_finish(fake_mpv: str, fake_ytdlp: str):
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    await player.play("Burial", "Archangel")
    await player.stop()
    assert (await player.status()).finished is False


async def test_choosing_another_track_does_not_wait_for_the_first_ones_search(
    tmp_path: Path, fake_mpv: str
):
    """Pressing Enter on a second track must cost the second track's search, not both.

    The first request is abandoned the moment the second arrives, so it never
    spawns mpv and the second starts after its own resolve. Before this, the
    second call sat on `_play_lock` until the first had searched *and* loaded,
    which is the wait that reads as a hang.
    """
    script = tmp_path / "slow-ytdlp"
    script.write_text(
        "#!/bin/sh\n"
        'case "$*" in *Slow*) sleep 3 ;; esac\n'
        "printf 'Archangel\\thttps://example.invalid/audio\\n'\n"
    )
    script.chmod(0o755)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=str(script))

    first = asyncio.create_task(player.play("Burial", "Slow Song"))
    await asyncio.sleep(0.3)  # by now it is inside the search
    started = time.monotonic()
    second = await player.play("Burial", "Quick Song")
    waited = time.monotonic() - started

    assert second.ok is True
    assert waited < 1.0, f"the second track waited {waited:.1f}s for the first one's search"
    replaced = await first
    assert replaced.ok is False  # the abandoned one says so rather than playing
    assert replaced.superseded is True
    await player.stop()


async def test_a_play_waiting_on_a_prefetch_steps_aside_too(tmp_path: Path, fake_mpv: str):
    """Waiting on a prefetch is seconds long, and it used to hold the lock.

    The second request must not sit behind it, and the first must not wait it out
    either: it is abandoned, prefetch and all.
    """
    slow = tmp_path / "slow-ytdlp"
    slow.write_text("#!/bin/sh\nsleep 2\nprintf 'Archangel\\thttps://example.invalid/audio\\n'\n")
    slow.chmod(0o755)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=str(slow))

    player.prefetch_track("Burial", "Black Sun")  # in flight, two seconds long
    await asyncio.sleep(0.2)
    first = asyncio.create_task(player.play("Burial", "Black Sun"))  # waits on it

    second = asyncio.create_task(player.play("Burial", "Sine"))
    await asyncio.sleep(0.2)

    assert first.done(), "the first play waited out a prefetch it no longer needed"
    assert (await first).superseded is True
    second.cancel()
    with suppress(asyncio.CancelledError):  # its own resolve is the slow one
        await second
    await player.stop()


async def test_a_second_play_does_not_wait_for_the_first_ones_mpv_start(
    tmp_path: Path, fake_ytdlp: str
):
    """Starting mpv takes seconds on a real stream, and that happens under the lock.

    Pressing Enter on another track while the first is coming up must not wait for
    it: the first is abandoned, tears down what it started, and says it was
    superseded. This is the case the owner still hit — the *resolve* is quick when
    the row was prefetched, so the wait is mpv itself.
    """
    mpv = tmp_path / "slow-mpv"
    mpv.write_text(
        FAKE_MPV.format(python=sys.executable).replace(
            "async def main():", "async def main():\n    await asyncio.sleep(1.5)"
        )
    )
    mpv.chmod(0o755)
    player = MpvPlayer(mpv=str(mpv), ytdlp=fake_ytdlp)

    first = asyncio.create_task(player.play("Burial", "Black Sun"))
    await asyncio.sleep(0.3)  # it is starting mpv by now
    started = time.monotonic()
    second = await player.play("Burial", "Sine")
    waited = time.monotonic() - started

    assert second.ok is True
    # The second track should cost its own mpv coming up (1.5s here) and nothing
    # of the first's: two startups, not three, and no wait for the other track.
    assert waited < 2.0, f"the second track waited {waited:.1f}s for the first one's mpv"
    assert (await first).superseded is True
    await player.stop()


async def test_choosing_a_source_overtakes_a_track_that_is_still_searching(
    tmp_path: Path, fake_mpv: str
):
    """The picker's choice wins over a search that is still running."""
    script = tmp_path / "still-slow-ytdlp"
    script.write_text("#!/bin/sh\nsleep 3\nprintf 'Archangel\\thttps://example.invalid/audio\\n'\n")
    script.chmod(0o755)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=str(script))

    first = asyncio.create_task(player.play("Burial", "Slow Song"))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    chosen = await player.play_url("Archangel (rework)", "https://example.invalid/pick")
    waited = time.monotonic() - started

    assert chosen.ok is True
    assert waited < 1.0, f"the chosen source waited {waited:.1f}s for the search"
    assert (await first).superseded is True
    await player.stop()


async def test_play_reports_mpv_error_instead_of_claiming_success(
    fake_mpv: str, fake_ytdlp: str, monkeypatch: pytest.MonkeyPatch
):
    """mpv can load metadata and only then fail to open audio — no false 'Playing'."""
    monkeypatch.setenv("FAKE_MPV_MODE", "exit")
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    result = await player.play("Burial", "Archangel")
    assert result.ok is False
    assert "couldn't open play stream" in result.message
    assert (await player.status()).playing is False


async def test_play_times_out_when_playback_never_starts(
    fake_mpv: str, fake_ytdlp: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_MPV_MODE", "stuck")
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp, load_timeout=0.3)
    result = await player.play("Burial", "Archangel")
    assert result.ok is False
    assert "did not start playing" in result.message


async def test_play_pause_seek_stop_round_trip(fake_mpv: str, fake_ytdlp: str):
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    result = await player.play("Burial", "Archangel")
    assert result.ok is True
    assert "Archangel" in result.message

    status = await player.status()
    assert status.playing is True
    assert status.duration == 100.0
    start = status.position

    assert await player.toggle_pause() is True
    assert (await player.status()).paused is True
    assert await player.toggle_pause() is False

    await player.seek(15)
    assert (await player.status()).position == start + 15
    await player.seek(-15)
    assert (await player.status()).position == start

    await player.stop()
    assert (await player.status()).playing is False


async def test_playing_a_new_track_stops_the_previous_one(fake_mpv: str, fake_ytdlp: str):
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    await player.play("Burial", "Archangel")
    first = player._proc
    await player.play("Burial", "Etched Headplate")
    assert first is not None and first.returncode is not None
    assert player._proc is not first
    await player.stop()


async def test_a_failed_start_is_retried(fake_mpv: str, fake_ytdlp: str, tmp_path, monkeypatch):
    """A refused stream URL (HTTP 403) is common; a fresh resolve usually works."""
    counter = tmp_path / "attempts"
    monkeypatch.setenv("FAKE_MPV_FAIL_TIMES", "1")
    monkeypatch.setenv("FAKE_MPV_COUNTER", str(counter))
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    result = await player.play("Burial", "Archangel")
    assert result.ok is True
    assert counter.read_text() == "2"
    await player.stop()


async def test_a_dead_remembered_source_falls_back_to_a_search(
    fake_mpv: str, fake_ytdlp: str, tmp_path, monkeypatch
):
    """The picked URL is tried first; if it is gone, search instead of refusing."""
    from lfm.store.sources import Sources

    Sources().remember("Burial", "Archangel", title="Old pick", url="https://youtu.be/dead")
    counter = tmp_path / "attempts"
    monkeypatch.setenv("FAKE_MPV_FAIL_TIMES", "1")
    monkeypatch.setenv("FAKE_MPV_COUNTER", str(counter))
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)

    result = await player.play("Burial", "Archangel")

    assert result.ok is True
    assert counter.read_text() == "2"  # the remembered URL failed, the searched one played
    kept = Sources().chosen("Burial", "Archangel")
    assert kept is not None and kept.url == "https://youtu.be/dead"  # the pick survives
    await player.stop()


async def test_retries_are_capped_and_reported(
    fake_mpv: str, fake_ytdlp: str, tmp_path, monkeypatch
):
    counter = tmp_path / "attempts"
    monkeypatch.setenv("FAKE_MPV_FAIL_TIMES", "99")
    monkeypatch.setenv("FAKE_MPV_COUNTER", str(counter))
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    result = await player.play("Burial", "Archangel")
    assert result.ok is False
    assert result.message == "could not play Archangel: https: HTTP error 403 Forbidden"
    assert counter.read_text() == str(PLAY_ATTEMPTS)


def test_parse_search_reads_id_title_channel_and_duration():
    results = parse_search("abc123\tArchangel\tHyperdub\t3:58\n")
    assert results == [
        YtResult(
            title="Archangel",
            url="https://www.youtube.com/watch?v=abc123",
            channel="Hyperdub",
            duration="3:58",
        )
    ]


def test_parse_search_skips_junk_lines():
    assert parse_search("\nnot a result\n\tmissing id\n") == []


def test_search_cmd_asks_for_the_given_number_of_results():
    cmd = search_cmd("Burial - Archangel", 5, "yt-dlp")
    assert cmd[-1] == "ytsearch5:Burial - Archangel"
    # flat results: without this yt-dlp extracts every hit (7.8s vs 1.2s),
    # and --print already implies the --simulate that --skip-download would set
    assert "--flat-playlist" in cmd
    assert "--skip-download" not in cmd


async def test_search_tracks_parses_ytdlp_output(tmp_path: Path):
    script = tmp_path / "ytdlp"
    script.write_text("#!/bin/sh\nprintf 'abc\\tArchangel\\tHyperdub\\t3:58\\n'\n")
    script.chmod(0o755)
    results = await search_tracks("Burial - Archangel", 5, str(script))
    assert [(r.title, r.url) for r in results] == [
        ("Archangel", "https://www.youtube.com/watch?v=abc")
    ]


async def test_search_tracks_gives_up_on_a_hanging_ytdlp(tmp_path: Path):
    script = tmp_path / "ytdlp"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(YtDlpError, match="timed out"):
        await search_tracks("anything", 5, str(script), timeout=0.3)
    assert time.monotonic() - started < 3  # it did not wait the sleep out


async def test_a_hanging_resolve_is_reported_quickly(tmp_path: Path, fake_mpv: str, monkeypatch):
    slow = tmp_path / "ytdlp"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(player_module, "RESOLVE_TIMEOUT", 0.3)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=str(slow))
    started = time.monotonic()
    result = await player.play("Burial", "Archangel")
    assert result.ok is False
    assert "timed out" in result.message
    assert time.monotonic() - started < 3  # not three 30-second sleeps


async def test_search_tracks_says_why_when_ytdlp_fails(tmp_path: Path):
    script = tmp_path / "ytdlp"
    script.write_text("#!/bin/sh\necho 'ERROR: Sign in to confirm you are not a bot' >&2\nexit 1\n")
    script.chmod(0o755)
    with pytest.raises(YtDlpError, match="Sign in to confirm"):
        await search_tracks("anything", 5, str(script))


async def test_search_tracks_is_empty_when_ytdlp_finds_nothing(tmp_path: Path):
    script = tmp_path / "ytdlp"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    assert await search_tracks("anything", 5, str(script)) == []


async def test_search_tracks_reports_a_missing_ytdlp(tmp_path: Path):
    with pytest.raises(YtDlpError, match="could not be started"):
        await search_tracks("anything", 5, str(tmp_path / "nope"))


async def test_find_sources_queries_artist_and_track(tmp_path: Path):
    script = tmp_path / "ytdlp"
    script.write_text("#!/bin/sh\nprintf 'abc\\tArchangel\\tHyperdub\\t3:58\\n'\n")
    script.chmod(0o755)
    player = MpvPlayer(mpv="/bin/true", ytdlp=str(script))
    results = await player.find_sources("Burial", "Archangel")
    assert [result.title for result in results] == ["Archangel"]


async def test_play_url_plays_that_source(fake_mpv: str, fake_ytdlp: str, monkeypatch):
    seen: list[str] = []
    real = player_module.mpv_cmd

    def spy(url, socket, mpv="mpv", volume=100.0):
        seen.append(url)
        return real(url, socket, mpv, volume)

    monkeypatch.setattr(player_module, "mpv_cmd", spy)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    result = await player.play_url("Archangel (rework)", "https://youtu.be/bbb")
    assert result.ok is True
    assert result.message == "Playing Archangel (rework)"
    assert seen == ["https://youtu.be/bbb"]
    await player.stop()


async def test_a_remembered_source_is_used_without_searching(
    fake_mpv: str, fake_ytdlp: str, monkeypatch
):
    from lfm.store.sources import Sources

    Sources().remember("Burial", "Archangel", title="My pick", url="https://youtu.be/zzz")
    seen: list[str] = []
    real = player_module.mpv_cmd

    def spy(url, socket, mpv="mpv", volume=100.0):
        seen.append(url)
        return real(url, socket, mpv, volume)

    async def fail_if_searched(*args, **kwargs):
        raise AssertionError("a remembered source should not be searched for")

    monkeypatch.setattr(player_module, "mpv_cmd", spy)
    monkeypatch.setattr(player_module, "search_tracks", fail_if_searched)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    result = await player.play("Burial", "Archangel")
    assert result.ok is True
    assert seen == ["https://youtu.be/zzz"]
    await player.stop()


async def test_a_miss_falls_back_to_the_track_name(fake_mpv: str, tmp_path: Path, monkeypatch):
    """The precise query finds nothing, so the next attempt drops the artist."""
    ytdlp = tmp_path / "pick-ytdlp"
    ytdlp.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in *" - "*) echo "ERROR: nothing found" >&2; exit 1;; esac\n'
        "done\n"
        "printf 'Archangel\\thttps://example.invalid/a\\n'\n"
    )
    ytdlp.chmod(0o755)
    seen: list[str] = []
    real = player_module.mpv_cmd

    def spy(url, socket, mpv="mpv", volume=100.0):
        seen.append(url)
        return real(url, socket, mpv, volume)

    monkeypatch.setattr(player_module, "mpv_cmd", spy)
    player = MpvPlayer(mpv=fake_mpv, ytdlp=str(ytdlp))
    result = await player.play("Burial", "Archangel")
    assert result.ok is True
    assert seen == ["https://example.invalid/a"]
    await player.stop()


async def test_controls_are_no_ops_when_idle():
    player = MpvPlayer()
    assert await player.toggle_pause() is None
    await player.seek(15)
    await player.stop()
    assert (await player.status()).playing is False


async def test_stop_while_resolving_cancels_the_play(monkeypatch, fake_mpv: str, fake_ytdlp: str):
    """A stop during the slow resolve must win, not start playing anyway."""
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    gate = asyncio.Event()
    real_resolve = player._resolve

    async def slow_resolve(query: str):
        await gate.wait()
        return await real_resolve(query)

    monkeypatch.setattr(player, "_resolve", slow_resolve)
    task = asyncio.create_task(player.play("Burial", "Archangel"))
    await asyncio.sleep(0.05)
    await player.stop()
    gate.set()
    result = await task
    assert result.ok is False
    assert (await player.status()).playing is False


async def test_overlapping_plays_never_leave_two_players(
    monkeypatch, fake_mpv: str, fake_ytdlp: str
):
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    created: list[asyncio.subprocess.Process] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await asyncio.gather(
        player.play("Burial", "Archangel"),
        player.play("Burial", "Near Dark"),
    )
    still_running = [process for process in created if process.returncode is None]
    assert len(still_running) == 1
    await player.stop()


async def test_volume_is_clamped_and_remembered_across_tracks(fake_mpv: str, fake_ytdlp: str):
    player = MpvPlayer(mpv=fake_mpv, ytdlp=fake_ytdlp)
    # Set before anything plays: the session value is remembered...
    assert await player.change_volume(5) == 105
    assert await player.change_volume(-1000) == 0
    assert await player.change_volume(1000) == VOLUME_MAX

    # ...and handed to each new mpv (which restarts for every track).
    await player.play("Burial", "Archangel")
    assert (await player.status()).volume == float(VOLUME_MAX)
    assert await player.change_volume(-5) == VOLUME_MAX - 5
    await player.stop()
    assert (await player.status()).volume == float(VOLUME_MAX - 5)


def test_spawn_kwargs_uses_pdeathsig_on_linux(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert spawn_kwargs()["preexec_fn"] is set_pdeathsig


def test_spawn_kwargs_is_empty_off_linux(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert spawn_kwargs() == {}


def test_set_pdeathsig_arms_prctl(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[int, ...]] = []

    class FakeLibc:
        def prctl(self, *args: int) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr("lfm.playback.player._libc", FakeLibc())
    monkeypatch.setattr("lfm.playback.player.os.getppid", lambda: 4242)
    set_pdeathsig()
    assert calls and calls[0][0] == 1  # PR_SET_PDEATHSIG


def test_set_pdeathsig_without_libc_is_a_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lfm.playback.player._libc", None)
    set_pdeathsig()  # must not raise on musl, macOS or elsewhere


GRANDCHILD_HELPER = """
import subprocess, sys, time
from lfm.playback.player import set_pdeathsig

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"], preexec_fn=set_pdeathsig
)
print(child.pid, flush=True)
time.sleep(30)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_child_dies_when_parent_is_killed(tmp_path: Path):
    """The guarantee: SIGKILL the parent, and the prctl child still goes away."""
    helper = tmp_path / "helper.py"
    helper.write_text(GRANDCHILD_HELPER)
    parent = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.PIPE, text=True)
    assert parent.stdout is not None
    grandchild = int(parent.stdout.readline().strip())
    try:
        parent.kill()
        parent.wait()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        raise AssertionError("grandchild survived its parent being SIGKILLed")
    finally:
        with suppress(ProcessLookupError):
            os.kill(grandchild, signal.SIGKILL)


def test_the_picker_asks_for_ten_candidates():
    assert search_cmd("Burial - Archangel")[-1] == "ytsearch10:Burial - Archangel"
    assert stream_url_cmd("https://youtu.be/aaa")[-1] == "https://youtu.be/aaa"


async def test_a_prefetched_track_plays_without_resolving_again(isolated_env, monkeypatch):
    """The point of the prefetch: one resolve, and Enter does not wait for a second."""
    player = MpvPlayer(mpv="/bin/true", ytdlp="/bin/true")
    asked: list[str] = []
    loaded: list[tuple[str, str]] = []

    async def fake_resolve(query: str) -> tuple[str, str]:
        asked.append(query)
        return ("Archangel", "https://stream.example/audio")

    async def fake_load(title: str, url: str, generation: int) -> PlayResult:
        loaded.append((title, url))
        return PlayResult(True, "playing")

    monkeypatch.setattr(player, "_resolve", fake_resolve)
    monkeypatch.setattr(player, "_load", fake_load)

    player.prefetch_track("Burial", "Archangel")
    for _ in range(5):  # let the detached task run
        await asyncio.sleep(0)

    result = await player.play("Burial", "Archangel")
    assert result.ok
    assert asked == ["Burial - Archangel"]  # resolved once, by the prefetch
    assert loaded[-1] == ("Archangel", "https://stream.example/audio")


async def test_prefetching_a_second_track_cancels_the_first(isolated_env, monkeypatch):
    """Skimming a list costs one extraction, not one per row."""
    player = MpvPlayer(mpv="/bin/true", ytdlp="/bin/true")
    started: list[str] = []

    async def slow_resolve(query: str) -> tuple[str, str]:
        started.append(query)
        await asyncio.sleep(30)
        return ("never", "never")

    monkeypatch.setattr(player, "_resolve", slow_resolve)
    player.prefetch_track("Burial", "Black Sun")
    await asyncio.sleep(0)
    player.prefetch_track("Burial", "Sine")
    await asyncio.sleep(0)

    assert started == ["Burial - Black Sun", "Burial - Sine"]
    assert list(player._resolving) == ["burial\tsine"]  # the first was cancelled
