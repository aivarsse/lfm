"""Playback through mpv: one track at a time, with pause, seek and a timeline.

lfm resolves a YouTube Music search with yt-dlp, then owns a single mpv process
and talks to it over mpv's JSON IPC socket. Owning the process is what makes
play/pause, +/-15s seeking, knowing the position, and stopping on quit possible.
The previous `ymc` hand-off could do none of that: each invocation spawned a
detached mpv that outlived lfm, so tracks stacked up and nothing could reach them.

Requires `mpv` and `yt-dlp` on PATH.
"""

import asyncio
import ctypes
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MPV = "mpv"
YTDLP = "yt-dlp"
INSTALL_HINT = "playback needs mpv and yt-dlp on PATH"
# The message a request carries when a newer one has taken its place: the user
# moved on, so this is an explanation for a log, not an error for the screen.
SUPERSEDED = "another track was chosen"


class _Superseded(Exception):
    """A newer request took this one's place. Unwind quietly; do not report it."""


SEEK_SECONDS = 15
VOLUME_STEP = 5
VOLUME_MAX = 130  # mpv's own ceiling
# How long to wait for mpv to report a loaded track before calling it a failure.
LOAD_TIMEOUT = 15.0
# How many YouTube candidates the picker offers, and how long to wait for
# the search: --flat-playlist answers in about a second, so this is generous.
SOURCE_COUNT = 10
SEARCH_TIMEOUT = 15.0
# yt-dlp resolving a track is one video; 30s is already pathological.
RESOLVE_TIMEOUT = 30.0
# A stream that fails to load is retried this many times; the URL is fetched
# fresh each attempt, which clears the intermittent HTTP 403 from YouTube.
PLAY_ATTEMPTS = 3

# Load libc once, at import: doing dlopen between fork and exec is a deadlock risk.
try:
    _libc: Any = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:  # not glibc (musl, or not Linux at all)
    _libc = None
_PDEATHSIG = 1


def set_pdeathsig() -> None:
    """Linux: SIGTERM this child when its parent thread dies (runs between fork and exec).

    This is the guarantee for the unhandleable exits: if lfm is SIGKILLed, no
    cleanup code runs, but the kernel still kills mpv instead of leaving it
    playing. musl libc exposes prctl too, but the symbol may differ — best effort.
    """
    if _libc is None:
        return
    parent = os.getppid()
    _libc.prctl(_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    if os.getppid() != parent:  # parent died before the prctl landed
        os._exit(1)


def spawn_kwargs() -> dict[str, Any]:
    """Extra spawn options; PR_SET_PDEATHSIG is Linux-only."""
    if sys.platform.startswith("linux"):
        return {"preexec_fn": set_pdeathsig}
    return {}


@dataclass(frozen=True)
class PlayResult:
    ok: bool
    message: str
    # True when the request was dropped because a newer one arrived first: the
    # user did not ask for this and does not want to be told it failed.
    superseded: bool = False


def _superseded() -> PlayResult:
    """What a request that a newer one overtook reports."""
    return PlayResult(False, SUPERSEDED, superseded=True)


@dataclass(frozen=True)
class YtResult:
    """One YouTube candidate for a track, as the picker shows it."""

    title: str
    url: str
    channel: str = ""
    duration: str = ""


def search_cmd(query: str, count: int = SOURCE_COUNT, ytdlp: str = YTDLP) -> list[str]:
    """yt-dlp command printing id, title, channel and duration per search hit.

    `--flat-playlist` is what makes the picker quick: without it yt-dlp extracts
    every result individually just to fill the template (measured 7.8s for five
    hits against 1.2s), and the search page already carries all four fields.
    """
    return [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--flat-playlist",
        "--print",
        "%(id)s\t%(title)s\t%(channel)s\t%(duration_string)s",
        "--",
        f"ytsearch{count}:{query}",
    ]


def parse_search(output: str) -> list[YtResult]:
    """Tab-separated yt-dlp lines into candidates; junk lines are skipped."""
    results: list[YtResult] = []
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        results.append(
            YtResult(
                title=parts[1].strip(),
                url=f"https://www.youtube.com/watch?v={parts[0].strip()}",
                channel=parts[2].strip() if len(parts) > 2 else "",
                duration=parts[3].strip() if len(parts) > 3 else "",
            )
        )
    return results


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a hung yt-dlp and anything it spawned, without waiting on their pipes.

    `Process.wait()` waits for the pipes to close as well, so a grandchild that
    inherited our stdout (yt-dlp's ffmpeg, or a shell script in a test) keeps it
    blocked long after the process we killed is gone. `start_new_session=True`
    makes the child its own process-group leader, so killing the group collects
    the descendants too.
    """
    with suppress(ProcessLookupError, PermissionError):
        group = os.getpgid(proc.pid)
        if group != os.getpgid(0):  # never signal our own group
            os.killpg(group, signal.SIGKILL)
    with suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=1)


async def search_tracks(
    query: str,
    count: int = SOURCE_COUNT,
    ytdlp: str = YTDLP,
    timeout: float = SEARCH_TIMEOUT,
) -> list[YtResult]:
    """Search YouTube; an empty list means yt-dlp answered, with nothing to show.

    A failure raises YtDlpError carrying yt-dlp's own last line, so the picker can
    say *why* it is empty instead of claiming YouTube has nothing. That line used
    to go to /dev/null, which made a bot check, a timeout and no results look the
    same.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *search_cmd(query, count, ytdlp),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # so a timeout can kill its children too
        )
    except OSError as exc:
        raise YtDlpError(f"yt-dlp could not be started: {exc}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _stop_process(proc)  # never leave a search running behind the picker
        raise YtDlpError("yt-dlp timed out") from None
    if proc.returncode != 0:
        raise YtDlpError(_last_line(err) or f"yt-dlp exited {proc.returncode}")
    return parse_search(out.decode(errors="replace"))


class YtDlpError(Exception):
    """yt-dlp could not answer; the message is its own last line of complaint."""


def _last_line(output: bytes) -> str:
    """The last non-empty stderr line, which is where yt-dlp explains itself."""
    lines = output.decode(errors="replace").strip().splitlines()
    return lines[-1].strip() if lines else ""


class MpvStartError(Exception):
    """mpv died before it was ready, with its explanation as the message."""


@dataclass(frozen=True)
class PlayerStatus:
    """What the player bar needs to draw itself."""

    playing: bool = False
    paused: bool = False
    position: float = 0.0
    duration: float = 0.0
    volume: float = 0.0
    title: str = ""
    # True when a loaded track reached its end cleanly, False when it failed or
    # was stopped: the screen may only play on after a clean finish.
    finished: bool = False


def search_query(artist: str, track: str) -> str:
    """The YouTube Music search string for a Last.fm top track."""
    return f"{artist} - {track}"


def resolve_cmd(query: str, ytdlp: str = YTDLP) -> list[str]:
    """yt-dlp command that prints the title and direct audio URL of the top hit."""
    return [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bestaudio/best",
        # --print implies --simulate (per its help), and one line per result
        # keeps a title containing a newline from looking like a URL.
        "--print",
        "%(title)s\t%(url)s",
        "--",
        f"ytsearch1:{query}",
    ]


def stream_url_cmd(url: str, ytdlp: str = YTDLP) -> list[str]:
    """yt-dlp command printing the direct audio URL of one already-found video."""
    return [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bestaudio/best",
        "--print",
        "%(url)s",
        "--",
        url,
    ]


def parse_resolve(output: str) -> tuple[str, str]:
    """The title and stream URL from one tab-separated yt-dlp line."""
    for line in output.strip().splitlines():
        title, separator, url = line.partition("\t")
        if separator and url.strip():
            return title.strip(), url.strip()
    raise ValueError("yt-dlp returned no track")


def mpv_cmd(url: str, socket: Path, mpv: str = MPV, volume: float = 100.0) -> list[str]:
    return [
        mpv,
        # mpv's manual recommends this (or --config-dir) when it is embedded:
        # "to avoid conflicts with the normal mpv user configuration intended for
        # CLI playback". Everything lfm needs is on this command line, so a
        # user's mpv.conf, scripts or key bindings cannot change what plays  -  at
        # the price of ignoring their audio-output settings as well.
        "--no-config",
        "--vid=no",  # documented spelling; --video/--no-video are aliases
        "--no-terminal",
        # mpv's default is bestvideo+bestaudio/best, so a picked YouTube URL
        # would fetch the video too and we only ever play audio.
        "--ytdl-format=bestaudio/best",
        # Errors must reach stderr so a failed start can be reported instead of
        # swallowed (this is why --really-quiet is deliberately not used).
        "--msg-level=all=warn",
        # Sources are not mastered to one level: a YouTube upload runs loud, a promo
        # clip quiet, and yt-dlp hands back whichever it found. loudnorm is EBU R128
        # in one pass, aiming every track at -16 LUFS (what streaming targets) with
        # -1.5 dBTP headroom, so the volume knob means the same thing track to track.
        # It is a filter rather than ReplayGain because the streams carry no tags.
        "--af=lavfi=[loudnorm=I=-16:TP=-1.5:LRA=11]",
        f"--volume={volume:g}",
        f"--input-ipc-server={socket}",
        "--",
        url,
    ]


def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


class MpvPlayer:
    """A single mpv process, replaced track by track and controlled over IPC."""

    def __init__(
        self, *, mpv: str = MPV, ytdlp: str = YTDLP, load_timeout: float = LOAD_TIMEOUT
    ) -> None:
        self._mpv = mpv
        self._ytdlp = ytdlp
        self._load_timeout = load_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stderr: deque[str] = deque(maxlen=5)
        self._drain_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._play_lock = asyncio.Lock()
        self._generation = 0
        # The newest request, and the one thing it waits on: a search, a
        # prefetch, or mpv coming up. `_claim` cancels the second.
        self._wanted = 0
        self._awaiting: asyncio.Task | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._request_id = 0
        self._title = ""
        self._loaded = False
        self._dir: Path | None = None
        # Session volume: mpv is replaced per track, so lfm has to remember it.
        self.volume = 100.0
        # Resolved tracks, so the row under the cursor can be ready before Enter:
        # keyed by artist and track, and thrown away with the session, because a
        # stream URL is only good for hours.
        self._resolved: dict[str, tuple[str, str]] = {}
        self._resolving: dict[str, asyncio.Task[tuple[str, str] | None]] = {}

    async def play(self, artist: str, track: str) -> PlayResult:
        """Resolve and play one track, stopping whatever was playing first.

        Serialised by `_play_lock` so two overlapping calls can never leave two
        mpv processes playing, and generation-checked so a stop that lands while
        the track is still being resolved wins over the play. A newer *request*
        wins too, and sooner: every slow step of this call is a task `_claim` can
        cancel — the search, the prefetch it finally waits on, and mpv coming up —
        so choosing another track costs the second track's work and not both. A
        failed start is retried: the resolved stream URL is intermittently
        refused (HTTP 403) and a fresh resolve usually gets a working one.
        """
        ticket = self._claim()
        try:
            async with self._hold_the_player(ticket) as early:
                if early is not None:
                    return early
                generation = self._generation

                failure: PlayResult | None = None
                key = self._track_key(artist, track)
                cached = self._resolved.get(key)
                if cached is None and (pending := self._resolving.get(key)) is not None:
                    # A prefetch is already doing this work, and waiting on it is
                    # abandonable like every other step.
                    try:
                        cached = await self._held(pending, ticket)
                    except asyncio.CancelledError:
                        cached = None  # another prefetch took over: search ourselves
                if cached is not None:
                    ready = await self._start_mpv(cached[0], cached[1], generation, ticket)
                    if ready.ok or generation != self._generation:
                        return ready
                    failure = ready  # a stream URL that has expired: search again

                from ..store.sources import Sources

                picked = Sources().chosen(artist, track)
                if picked is not None:
                    chosen = await self._start_mpv(picked.title, picked.url, generation, ticket)
                    if chosen.ok or generation != self._generation:
                        return chosen
                    # A remembered source that no longer plays: search again rather
                    # than refusing, and leave the pick alone in case it is only
                    # temporarily gone. `y` re-picks it.
                    failure = chosen

                for attempt in range(PLAY_ATTEMPTS):
                    if ticket != self._wanted:
                        return _superseded()
                    # first the precise query, then just the track, which finds more
                    result = await self._held(
                        asyncio.create_task(
                            self._start(artist, track, generation, loosen=attempt > 0)
                        ),
                        ticket,
                    )
                    if result.ok or generation != self._generation:
                        return result
                    failure = result
                reason = failure.message if failure is not None else "unknown error"
                return PlayResult(False, f"could not play {track}: {reason}")
        except _Superseded:
            return _superseded()

    async def _held(self, task: asyncio.Task, ticket: int):
        """Await one slow step, remembering it so a newer request can abandon it.

        Everything a play waits for is seconds long — a yt-dlp search, a prefetch
        already running, or mpv coming up — and all of it used to happen while
        holding `_play_lock`, so the next request sat behind it. Recording the one
        in flight lets `_claim` cancel it; this call then unwinds through
        `_Superseded` instead of playing a track the user has moved past.
        """
        self._awaiting = task
        try:
            return await task
        except asyncio.CancelledError:
            if task.cancelled() and ticket != self._wanted:
                raise _Superseded from None
            raise  # cancelled for some other reason: not ours to swallow

    async def _start_mpv(self, title: str, url: str, generation: int, ticket: int) -> PlayResult:
        """Load one stream as a step a newer request can abandon."""
        if generation != self._generation:
            return PlayResult(False, "playback was stopped")
        return await self._held(asyncio.create_task(self._load(title, url, generation)), ticket)

    def _clean_up_soon(self) -> None:
        """Tear down whatever is running, without making anyone wait for it.

        Fire and forget on purpose. The process is being replaced, and the request
        replacing it calls `stop()` — which tears down again — the moment it holds
        the lock, so nothing has to wait here; `_teardown` is idempotent.
        """
        if self._cleanup is None or self._cleanup.done():
            self._cleanup = asyncio.create_task(self._teardown())

    def _claim(self) -> int:
        """Take the next ticket, abandoning whatever an older request waits on.

        Choosing another track makes the older request's work pointless, and it is
        the older request that holds `_play_lock` while it runs: the newer one
        would sit behind it. Cancelling that step here is what makes the first
        call stand aside promptly — `_resolve` kills its yt-dlp and `_load` tears
        down its mpv as they are cancelled — so the newer request costs its own
        work and nothing else.
        """
        self._wanted += 1
        awaiting = self._awaiting
        if awaiting is not None and not awaiting.done():
            awaiting.cancel()
        return self._wanted

    @asynccontextmanager
    async def _hold_the_player(self, ticket: int) -> AsyncGenerator[PlayResult | None]:
        """The gate every play goes through: hold the lock, stand aside if replaced.

        Yields `None` when this request owns the player — the lock taken and
        whatever was playing stopped — or the result to return instead when a newer
        request has taken its place, or playback cannot start at all.
        """
        if ticket != self._wanted:  # a newer request is already the one to serve
            yield _superseded()
            return
        async with self._play_lock:
            if ticket != self._wanted:
                yield _superseded()
                return
            if not self._ready():
                yield PlayResult(False, INSTALL_HINT)
                return
            await self.stop()
            yield None

    async def play_url(self, title: str, url: str) -> PlayResult:
        """Play one specific source, as the picker chose it."""
        ticket = self._claim()  # choosing a source overtakes a search in flight too
        try:
            async with self._hold_the_player(ticket) as early:
                if early is not None:
                    return early
                return await self._start_mpv(title, url, self._generation, ticket)
        except _Superseded:
            return _superseded()

    @staticmethod
    def _track_key(artist: str, track: str) -> str:
        return f"{artist.strip().casefold()}\t{track.strip().casefold()}"

    def prefetch_track(self, artist: str, track: str) -> None:
        """Start resolving a track, so pressing Enter on it does not wait.

        One at a time: starting one cancels the last, so skimming a list costs one
        extraction rather than one per row. Detached on purpose - a screen worker
        being cancelled must not take the resolve with it, since `play` waits on
        this very task when Enter arrives.
        """
        key = self._track_key(artist, track)
        if key in self._resolved or key in self._resolving:
            return
        for other, task in list(self._resolving.items()):
            if other != key:
                task.cancel()
                del self._resolving[other]
        task = asyncio.create_task(self._resolve_track(artist, track))
        self._resolving[key] = task
        task.add_done_callback(lambda _finished, key=key: self._resolving.pop(key, None))

    async def _resolve_track(self, artist: str, track: str) -> tuple[str, str] | None:
        """The remembered source if there is one, else the query, then cached.

        Best effort: nothing plays differently when it fails, it just waits.
        """
        key = self._track_key(artist, track)
        from ..store.sources import Sources

        try:
            picked = Sources().chosen(artist, track)
            if picked is not None:
                stream = await self.prefetch(picked.url)
                if stream is None:
                    return None
                resolved = (picked.title, stream)
            else:
                resolved = await self._resolve(search_query(artist, track))
        except (OSError, ValueError):
            return None
        self._resolved[key] = resolved
        return resolved

    async def find_sources(self, artist: str, track: str) -> list[YtResult]:
        """YouTube candidates for a track, for the picker; raises YtDlpError."""
        return await search_tracks(search_query(artist, track), SOURCE_COUNT, self._ytdlp)

    async def prefetch(self, url: str) -> str | None:
        """The direct audio URL for one candidate, so choosing it plays at once.

        Best effort by design: this hides a wait rather than being a step playback
        depends on, so a refusal or a timeout just means the picker plays the
        watch URL as it always did. Measured at ~1.6s per video, which is why one
        is resolved (the row under the cursor) and not ten.
        """
        proc = await asyncio.create_subprocess_exec(
            *stream_url_cmd(url, self._ytdlp),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # so a timeout can kill its children too
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=RESOLVE_TIMEOUT)
        except TimeoutError:
            await _stop_process(proc)
            return None
        if proc.returncode != 0:
            return None
        lines = out.decode(errors="replace").strip().splitlines()
        return lines[0].strip() or None if lines else None

    def _ready(self) -> bool:
        return shutil.which(self._mpv) is not None and shutil.which(self._ytdlp) is not None

    async def _start(
        self, artist: str, track: str, generation: int, *, loosen: bool = False
    ) -> PlayResult:
        try:
            title, url = await self._resolve_for(artist, track, loosen=loosen)
        except (OSError, ValueError) as exc:
            return PlayResult(False, f"could not find {track}: {exc}")
        return await self._load(title, url, generation)

    async def _resolve_for(self, artist: str, track: str, *, loosen: bool) -> tuple[str, str]:
        """The artist + track search, or just the track when that found nothing."""
        query = track if loosen else search_query(artist, track)
        return await self._resolve(query)

    async def _load(self, title: str, url: str, generation: int) -> PlayResult:
        if generation != self._generation:
            # Someone pressed stop (or started another track) while we resolved.
            return PlayResult(False, "playback was stopped")

        self._dir = Path(tempfile.mkdtemp(prefix="lfm-mpv-"))
        socket = self._dir / "mpv.sock"
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *mpv_cmd(url, socket, self._mpv, self.volume),
                stdin=asyncio.subprocess.DEVNULL,  # belt and braces with --no-terminal
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **spawn_kwargs(),
            )
            self._stderr.clear()
            self._drain_task = asyncio.create_task(self._drain_stderr())
            self._reader, self._writer = await self._connect(socket)
        except MpvStartError as exc:
            await self._teardown()
            return PlayResult(False, str(exc))
        except (OSError, TimeoutError) as exc:
            await self._teardown()
            return PlayResult(False, f"cannot start mpv: {exc}")
        except asyncio.CancelledError:
            # Replaced: clean up off the critical path. Awaiting the teardown
            # here would hold `_play_lock` while mpv dies.
            self._clean_up_soon()
            raise

        self._request_id = 0
        self._title = title
        try:
            problem = await self._wait_until_loaded()
        except asyncio.CancelledError:
            self._clean_up_soon()  # same again, for an mpv that never came up
            raise
        if problem is not None:
            await self._teardown()
            return PlayResult(False, problem)
        if generation != self._generation:
            await self._teardown()
            return PlayResult(False, "playback was stopped")
        self._loaded = True
        return PlayResult(True, f"Playing {title}")

    async def toggle_pause(self) -> bool | None:
        """Flip play/pause; returns whether it is now paused, or None if idle."""
        if not await self._is_playing():
            return None
        await self._command("cycle", "pause")
        return bool(await self._get("pause"))

    async def seek(self, seconds: int) -> None:
        if await self._is_playing():
            await self._command("seek", seconds)

    async def change_volume(self, delta: int) -> int:
        """Nudge the session volume by delta (clamped 0..VOLUME_MAX); returns the new level.

        mpv restarts for every track, so the level lives here and is passed to
        each new mpv. Changing it with nothing playing still sets it for later.
        """
        self.volume = max(0.0, min(float(VOLUME_MAX), self.volume + delta))
        if await self._is_playing():
            await self._command("set_property", "volume", self.volume)
        return int(self.volume)

    async def stop(self) -> None:
        # Bump the generation first: a play still resolving must not spawn after
        # this, and one already spawning will tear itself down once it checks.
        self._generation += 1
        await self._teardown()

    async def _teardown(self) -> None:
        """Kill the current mpv and clear state, without touching the generation."""
        if self._writer is not None:
            try:
                self._writer.write(json.dumps({"command": ["quit"]}).encode() + b"\n")
                await self._writer.drain()
            except (OSError, ConnectionError):
                pass
        if self._proc is not None and self._proc.returncode is None:
            if self._writer is None:
                # Never got as far as accepting a command, so there is nothing to
                # flush: kill it now rather than making its replacement wait.
                with suppress(ProcessLookupError):
                    self._proc.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._proc.wait(), timeout=1)
            else:
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        if self._writer is not None:
            self._writer.close()
        if self._drain_task is not None:
            self._drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._loaded = False  # a stopped or failed player is not a finished one
        self._proc = None
        self._reader = None
        self._writer = None
        self._title = ""
        self._request_id = 0
        self._dir = None

    async def _drain_stderr(self) -> None:
        """Keep the last few stderr lines; also stops mpv blocking on a full pipe."""
        assert self._proc is not None and self._proc.stderr is not None
        while line := await self._proc.stderr.readline():
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr.append(text)

    async def _wait_until_loaded(self) -> str | None:
        """None once playback is actually progressing, otherwise why it never got there.

        Waiting for `time-pos` to advance (not just for duration) matters: mpv
        reports metadata before it opens the audio output, so a machine with no
        working sound device looks 'loaded' for a moment before mpv exits.
        """
        deadline = time.monotonic() + self._load_timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                return await self._exit_reason()
            position = await self._get("time-pos")
            if position is not None and float(position) > 0:
                return None
            await asyncio.sleep(0.1)
        return self._stderr[-1] if self._stderr else "mpv did not start playing (timed out)"

    async def _exit_reason(self) -> str:
        """Wait for stderr to drain, then explain the early exit."""
        if self._drain_task is not None:
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._drain_task), timeout=2)
        if self._stderr:
            return self._stderr[-1]
        code = self._proc.returncode if self._proc is not None else "?"
        return f"mpv exited with status {code}"

    async def status(self) -> PlayerStatus:
        if not await self._is_playing():
            return PlayerStatus(volume=self.volume, finished=self._finished())
        if await self._get("eof-reached"):
            # mpv can stay alive at the end of a track (its config may set
            # keep-open), sitting paused at the last frame. Without this it would
            # look like a paused track forever, so repeat and auto-advance never
            # fired and playback simply stopped.
            return PlayerStatus(volume=self.volume, finished=True)
        position = await self._get("time-pos")
        duration = await self._get("duration")
        paused = await self._get("pause")
        return PlayerStatus(
            playing=True,
            paused=bool(paused),
            position=float(position or 0.0),
            duration=float(duration or 0.0),
            volume=self.volume,
            title=self._title,
        )

    def _finished(self) -> bool:
        """A track that loaded and then ended, rather than dying or being stopped."""
        return self._loaded and self._proc is not None and self._proc.returncode == 0

    async def _is_playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._writer is not None

    async def _resolve(self, query: str) -> tuple[str, str]:
        proc = await asyncio.create_subprocess_exec(
            *resolve_cmd(query, self._ytdlp),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # so a timeout can kill its children too
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=RESOLVE_TIMEOUT)
        except TimeoutError:
            await _stop_process(proc)
            raise ValueError("yt-dlp timed out") from None
        except asyncio.CancelledError:
            # a cancelled resolve - the track changed, or the cursor moved on -
            # must not leave its yt-dlp running to completion
            await _stop_process(proc)
            raise
        if proc.returncode != 0:
            raise ValueError(_last_line(err) or f"yt-dlp exited {proc.returncode}")
        return parse_resolve(out.decode(errors="replace"))

    async def _connect(
        self, socket: Path, timeout: float = 10.0
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        deadline = time.monotonic() + timeout
        while True:
            if self._proc is not None and self._proc.returncode is not None:
                # Died before it was ready: no point waiting out the timeout.
                raise MpvStartError(await self._exit_reason())
            if socket.exists():
                try:
                    return await asyncio.open_unix_connection(str(socket))
                except OSError:
                    pass
            if time.monotonic() > deadline:
                raise TimeoutError("mpv never opened its socket")
            await asyncio.sleep(0.05)

    async def _command(self, *args: Any) -> dict[str, Any] | None:
        if self._writer is None or self._reader is None:
            return None
        async with self._lock:
            self._request_id += 1
            request_id = self._request_id
            payload = {"command": list(args), "request_id": request_id}
            try:
                self._writer.write(json.dumps(payload).encode() + b"\n")
                await self._writer.drain()
            except (OSError, ConnectionError):  # mpv may have just died
                return None
            return await self._response(request_id)

    async def _response(self, request_id: int) -> dict[str, Any] | None:
        assert self._reader is not None
        try:
            while True:
                line = await asyncio.wait_for(self._reader.readline(), timeout=2)
                if not line:
                    return None
                message = json.loads(line)
                if message.get("request_id") == request_id:
                    return message
        except (TimeoutError, ValueError, ConnectionError):
            return None

    async def _get(self, prop: str) -> Any:
        message = await self._command("get_property", prop)
        if not message or message.get("error") != "success":
            return None
        return message.get("data")
