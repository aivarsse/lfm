"""lfm CLI. Heavy imports live inside commands so `lfm --help` stays fast."""

from __future__ import annotations

import asyncio
import enum
import tomllib
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from . import paths

if TYPE_CHECKING:
    from .api.client import LastfmClient

app = typer.Typer(no_args_is_help=True, help="Find similar artists via the Last.fm API.")


def _version() -> str:
    """The version, preferring the working tree over install-time metadata.

    An editable install records the version once, at install time, so after a
    `git pull` its metadata lags the checkout; reading the neighbouring
    pyproject.toml keeps `lfm --version` honest for that workflow. A wheel has
    no pyproject next to it, so it falls back to the installed metadata.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return version("lfm")
    except PackageNotFoundError:  # running from a source tree with no install
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"lfm {_version()}")
        raise typer.Exit()


@dataclass(frozen=True)
class RunOptions:
    """The options that apply to the whole run rather than to one command.

    The root callback puts one on `ctx.obj`, so a command reads it from there
    instead of every command declaring the option and threading it down.
    """

    no_cache: bool = False


@app.callback()
def _root(
    ctx: typer.Context,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the lfm version and exit.",
        ),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Do not read or write the response cache."),
    ] = False,
) -> None:
    """lfm — similar artists, ranked by Last.fm match."""
    ctx.obj = RunOptions(no_cache=no_cache)


class Output(str, enum.Enum):
    table = "table"
    plain = "plain"
    json = "json"
    links = "links"


def _cache_db() -> Path:
    return paths.cache_file("cache.db")


@contextmanager
def _client(ctx: typer.Context, config, refresh: bool = False) -> Generator["LastfmClient"]:
    """A Last.fm client on the shared cache, unless --no-cache was given.

    It is a run-wide option, so it arrives on the context rather than as an
    argument every command would have to declare.
    """
    from .api.client import LastfmClient
    from .store.cache import Cache

    if ctx.obj.no_cache:
        yield LastfmClient(_api_key(config))
        return
    with Cache(_cache_db()) as cache:
        yield LastfmClient(_api_key(config), cache, refresh=refresh)


def _human_size(path: Path) -> str:
    """Bytes, KB or MB: just enough to see whether clearing is worth it."""
    size = path.stat().st_size
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{size / 1000:.0f} KB"


def _load_config():
    """Load the config file, reporting a bad one instead of ignoring it."""
    from .config import ConfigError, load

    try:
        return load()
    except ConfigError as exc:
        _fail(str(exc))


def _setting(value, config, key: str):
    """A CLI flag wins; otherwise the config file, then the built-in default."""
    return value if value is not None else config.get(key)


def _api_key(config) -> str:
    key = config.api_key()
    if not key:
        typer.secho(
            f'error: no API key. Set LFM_API_KEY, or add `api_key = "…"` to {config.path}',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    return key


def _fail(message: str) -> NoReturn:
    """Report a fatal problem and stop. Never returns, so callers need no branch after it."""
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _scrobbling(config):
    """The write path, or None when any part of it is missing.

    Nothing to send with means nothing gets queued at all, which is what makes
    "off" mean off rather than "off until something else is set".
    """
    if not config.scrobbling():
        return None
    from .api.client import Scrobbler
    from .playback.scrobble import Scrobbling

    return Scrobbling(Scrobbler(config.api_key(), config.secret(), config.session_key()))


def _config_mode(config) -> str:
    """`, mode 0644` for a file that exists: a fact on screen, not a scolding.

    The mode is the owner's to choose — lfm keeps whatever they chose — so it is
    reported rather than policed.
    """
    import stat

    if not config.exists:
        return ""
    try:
        return f", mode {stat.S_IMODE(config.path.stat().st_mode):04o}"
    except OSError:
        return ""


def _warn_if_the_credentials_are_readable(config) -> None:
    """Say so, once, when a session key is about to go into a readable file.

    `lfm auth` is the moment a credential is added to whatever mode the owner
    chose, so that is the moment worth a word — not every `lfm config`, which
    reports the mode instead. A warning rather than a failure: the mode is
    theirs, and a credential read from the environment is none of this file's
    business.
    """
    import stat

    on_disk = (
        str(config.values.get("secret", "")).strip()
        or str(config.values.get("session_key", "")).strip()
    )
    if not on_disk:
        return
    try:
        mode = stat.S_IMODE(config.path.stat().st_mode)
    except OSError:
        return
    if mode & 0o044:  # group or other read
        typer.secho(
            f"warning: {config.path} is mode {mode:04o}, so another local user can read the "
            "credentials in it, and a scrobble session key is about to be written there — "
            "`chmod o-r` unless that was deliberate",
            fg=typer.colors.YELLOW,
            err=True,
        )


def _no_such_artist(client, artist: str) -> NoReturn:
    """Print the alternatives for a name Last.fm does not know, then exit."""
    typer.echo(f'No artist found for "{artist}".', err=True)
    _print_suggestions(client, artist)
    raise typer.Exit(1)


def _lookup(coro):
    """Run one lookup, reporting a Last.fm failure the way this CLI does.

    Everything that talks to Last.fm in this file goes through here or
    `_lookup_or_suggest`, so the messages live in one place.
    """
    import httpx

    from .api.client import ArtistNotFound, LastfmError

    try:
        return asyncio.run(coro)
    except ArtistNotFound:
        raise  # a miss means different things to different callers; they decide
    except LastfmError as exc:
        _fail(str(exc))
    except httpx.HTTPError as exc:
        _fail(f"cannot reach Last.fm: {exc}")


def _lookup_or_suggest(client, coro, artist: str):
    """The same, but an unknown name is usually a typo: offer the alternatives."""
    from .api.client import ArtistNotFound

    try:
        return _lookup(coro)
    except ArtistNotFound:
        _no_such_artist(client, artist)


def _remember(artist: str) -> None:
    """Note a looked-up artist for `lfm recent` and the prompt's completion."""
    from .store.history import History

    History().add(artist)


SUGGESTION_MATCHES = 30  # a relevance search buries the act you mean, so ask for a page of hits


def _print_suggestions(client, artist: str) -> None:
    """Best-effort "did you mean" list for a name Last.fm could not find."""
    import httpx

    from .api.client import LastfmError
    from .api.rank import suggestions

    try:
        matches = asyncio.run(client.search_matches(artist, limit=SUGGESTION_MATCHES))
    except (LastfmError, httpx.HTTPError):
        return
    named = suggestions(artist, matches)
    if named:
        typer.echo("Did you mean:", err=True)
        for name in named:
            typer.echo(f"  {name}", err=True)


def _deep_cut_rows(client, artists, max_listeners: int):
    """Filter the similar list down to its deep cuts, looking each row up once.

    Listener counts cost one `artist.getInfo` per candidate, so this only runs when
    `--max-listeners` asked for it, never for a plain lookup.
    """
    from .api.client import MAX_LISTENER_LOOKUPS
    from .api.flows import listener_counts
    from .api.rank import deep_cuts

    checked = artists[:MAX_LISTENER_LOOKUPS]
    if len(checked) < len(artists):
        typer.echo(f"checking the {MAX_LISTENER_LOOKUPS} most similar of {len(artists)}", err=True)
    counts = asyncio.run(listener_counts(client, checked))
    found = deep_cuts(checked, counts, max_listeners=max_listeners)
    if found.unknown:
        typer.echo(f"{found.unknown} candidates had no listener data", err=True)
    if not found.artists:
        typer.echo(f"No deep cuts under {max_listeners:,} listeners.", err=True)
        raise typer.Exit(1)
    return found.artists, [counts[a.name.casefold()] for a in found.artists]


RADIO_LIMIT = 30  # a page of similar tracks to hop through


def _radio_or_fail(client, artist: str, track: str, limit: int):
    """Similar tracks, minus the seed itself and anything repeated.

    Another hop is another call, so this stays one hop: the CLI prints where a
    radio would go next, and the queue that walks it lives in the TUI.
    """
    from .api.client import ArtistNotFound

    try:
        results = _lookup(client.similar_tracks(artist, track, limit=max(limit, RADIO_LIMIT)))
    except ArtistNotFound:
        typer.echo(f'No track found for "{artist} — {track}".', err=True)
        _print_suggestions(client, artist)
        raise typer.Exit(1) from None

    seen = {(artist.casefold(), track.casefold())}  # the seed comes back first
    out = []
    for result in results:
        key = (result.artist.casefold(), result.name.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(result)
        if len(out) >= limit:
            break
    if not out:
        typer.echo(f'Nothing similar to "{track}" by {artist} yet.', err=True)
        raise typer.Exit(1)
    return out


def _walk_or_fail(client, artist: str, depth: int, min_match: float):
    """Walk the graph from one artist; an empty walk gets the same treatment as an empty list."""
    from .api.flows import walk

    results = _lookup_or_suggest(
        client, walk(client, artist, depth=depth, min_match=min_match), artist
    )
    if not results:
        typer.echo(f'Nothing found around "{artist}".', err=True)
        raise typer.Exit(1)
    return results


def _ranked_or_fail(client, artist: str, limit: int, min_match: float):
    """Fetch and rank similar artists, printing "did you mean" and exiting when empty."""
    from .api.client import ArtistNotFound
    from .api.flows import similar_wide
    from .api.rank import rank

    resp = None
    try:
        resp = _lookup(similar_wide(client, artist, limit))
    except ArtistNotFound:
        pass  # an unknown name is not fatal here: there is simply nothing to rank

    raw = resp.similarartists.artist if resp else []
    artists = rank(raw, min_match=min_match, limit=limit)
    if not artists:
        typer.echo(f'No similar artists found for "{artist}".', err=True)
        if raw:
            typer.echo("All results fell below --min-match.", err=True)
        else:
            _print_suggestions(client, artist)
        raise typer.Exit(1)

    attr = resp.similarartists.attr if resp else None
    corrected = attr.artist if attr and attr.artist and attr.artist != artist else None
    return corrected, artists


def _shared_tag_lists(client, seed: str, artists) -> list[list[str]]:
    """Tags shared with each artist, one extra (cached) call per row.

    Enrichment only: a failed lookup leaves that row's list empty rather than
    failing the command.
    """
    import httpx

    from .api.client import LastfmError
    from .api.rank import shared_tags

    async def load():
        return await asyncio.gather(
            client.top_tags(seed),
            *(client.top_tags(artist.name) for artist in artists),
            return_exceptions=True,
        )

    try:
        results = asyncio.run(load())
    except (LastfmError, httpx.HTTPError):
        return [[] for _ in artists]

    seed_tags = results[0] if isinstance(results[0], list) else []
    return [shared_tags(seed_tags, row) if isinstance(row, list) else [] for row in results[1:]]


def _details_or_fail(client, artist: str):
    """The artist card's three lookups, fetched concurrently."""
    from .api.flows import artist_detail

    return _lookup_or_suggest(client, artist_detail(client, artist), artist)


def _blend_or_fail(client, seeds: list[str], limit: int, min_score: float):
    """Similar lists for every seed (fetched concurrently), blended into one ranking."""
    import httpx

    from .api.client import ArtistNotFound, LastfmError
    from .api.flows import similar_wide
    from .api.rank import blend

    async def load():
        return await asyncio.gather(
            *(similar_wide(client, seed, limit) for seed in seeds),
            return_exceptions=True,
        )

    responses = asyncio.run(load())
    seed_lists = {}
    for seed, response in zip(seeds, responses, strict=True):
        if isinstance(response, ArtistNotFound):
            _no_such_artist(client, seed)
        if isinstance(response, LastfmError):
            _fail(str(response))
        if isinstance(response, httpx.HTTPError):
            _fail(f"cannot reach Last.fm: {response}")
        seed_lists[seed] = list(response.similarartists.artist)

    results = blend(seed_lists, limit=limit, min_score=min_score, exclude=set(seeds))
    if not results:
        typer.echo("No artists are close to all of those seeds.", err=True)
        raise typer.Exit(1)
    return results


@app.command()
def similar(
    ctx: typer.Context,
    artist: Annotated[str, typer.Argument(help="Artist to look up.", show_default=False)],
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Show at most N artists (default: config/20)."),
    ] = None,
    min_match: Annotated[
        float | None,
        typer.Option("--min-match", min=0.0, max=1.0, help="Drop below this match (config/0)."),
    ] = None,
    output: Annotated[
        Output | None,
        typer.Option(
            "--output",
            "-o",
            help="Output format: table, plain, json or links (config/table).",
        ),
    ] = None,
    why: Annotated[
        bool,
        typer.Option("--why", help="Also show the tags each artist shares with this one."),
    ] = False,
    max_listeners: Annotated[
        int | None,
        typer.Option(
            "--max-listeners",
            min=1,
            help="Deep cuts: keep only artists with at most this many listeners.",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the cache (still refreshes it).")
    ] = False,
) -> None:
    """Similar artists, ranked by Last.fm match."""
    from .ui import render

    config = _load_config()
    limit = int(_setting(limit, config, "limit"))
    min_match = float(_setting(min_match, config, "min_match"))
    output = Output(_setting(output, config, "output"))

    with _client(ctx, config, refresh) as client:
        corrected, artists = _ranked_or_fail(client, artist, limit, min_match)
        _remember(corrected or artist)
        listeners: list[int | None] | None = None
        if max_listeners is not None:
            artists, listeners = _deep_cut_rows(client, artists, max_listeners)
        shared = _shared_tag_lists(client, artist, artists) if why else None
        if output is Output.plain:
            render.plain(artists)
        elif output is Output.json:
            render.similar_json(artist, corrected, artists, shared, listeners)
        elif output is Output.links:
            render.links(artists)
        else:
            render.table(corrected, artists, shared, listeners)


@app.command()
def radio(
    ctx: typer.Context,
    artist: Annotated[str, typer.Argument(help="Artist of the seed track.", show_default=False)],
    track: Annotated[
        str, typer.Argument(help="Track to build the radio around.", show_default=False)
    ],
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="How many tracks (default: config/20)."),
    ] = None,
    output: Annotated[
        Output | None,
        typer.Option(
            "--output",
            "-o",
            help="Output format: table, plain, json or links (config/table).",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the cache (still refreshes it).")
    ] = False,
) -> None:
    """Tracks close to one song — the first hop of a radio that drifts from here."""
    from .ui import render

    config = _load_config()
    limit = int(_setting(limit, config, "limit"))
    output = Output(_setting(output, config, "output"))

    with _client(ctx, config, refresh) as client:
        results = _radio_or_fail(client, artist, track, limit)
    if output is Output.plain:
        render.radio_plain(results)
    elif output is Output.json:
        render.radio_json(artist, track, results)
    elif output is Output.links:
        render.radio_links(results)
    else:
        render.radio_table(artist, track, results)


@app.command()
def walk(
    ctx: typer.Context,
    artist: Annotated[str, typer.Argument(help="Artist to start from.", show_default=False)],
    depth: Annotated[
        int,
        typer.Option(
            "--depth",
            min=1,
            max=2,
            help="How many hops out to walk (default 2; each hop multiplies the requests).",
        ),
    ] = 2,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Show at most N artists (default: config/20)."),
    ] = None,
    min_match: Annotated[
        float | None,
        typer.Option("--min-match", min=0.0, max=1.0, help="Drop below this match (config/0)."),
    ] = None,
    output: Annotated[
        Output | None,
        typer.Option(
            "--output",
            "-o",
            help="Output format: table, plain, json or links (config/table).",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the cache (still refreshes it).")
    ] = False,
) -> None:
    """Artists a hop or two out, scored by the paths that reach them."""
    from .ui import render

    config = _load_config()
    limit = int(_setting(limit, config, "limit"))
    min_match = float(_setting(min_match, config, "min_match"))
    output = Output(_setting(output, config, "output"))

    with _client(ctx, config, refresh) as client:
        results = _walk_or_fail(client, artist, depth, min_match)[:limit]
    _remember(artist)
    if output is Output.plain:
        render.walk_plain(results)
    elif output is Output.json:
        render.walk_json(artist, depth, results)
    elif output is Output.links:
        render.walk_links(results)
    else:
        render.walk_table(artist, results, depth)


@app.command()
def info(
    ctx: typer.Context,
    artist: Annotated[str, typer.Argument(help="Artist to look up.", show_default=False)],
    output: Annotated[
        Output | None,
        typer.Option(
            "--output",
            "-o",
            help="Output format: table, plain, json or links (config/table).",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the cache (still refreshes it).")
    ] = False,
) -> None:
    """Listeners, tags, top tracks and bio for one artist."""
    from .ui import render

    config = _load_config()
    output = Output(_setting(output, config, "output"))

    with _client(ctx, config, refresh) as client:
        details = _details_or_fail(client, artist)

    info_obj, tags, tracks = details
    _remember(info_obj.name)
    if output is Output.plain:
        render.info_plain(info_obj, tags, tracks)
    elif output is Output.json:
        render.info_json(artist, info_obj, tags, tracks)
    elif output is Output.links:
        render.info_links(info_obj)
    else:
        render.info_table(artist, info_obj, tags, tracks)


@app.command()
def blend(
    ctx: typer.Context,
    artists: Annotated[
        list[str], typer.Argument(help="Two or more seed artists.", show_default=False)
    ],
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Show at most N artists (default: config/20)."),
    ] = None,
    min_score: Annotated[
        float | None,
        typer.Option("--min-score", min=0.0, max=1.0, help="Drop blends below this score."),
    ] = None,
    output: Annotated[
        Output | None,
        typer.Option(
            "--output",
            "-o",
            help="Output format: table, plain, json or links (config/table).",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the cache (still refreshes it).")
    ] = False,
) -> None:
    """Artists close to all of the given artists, scored by the geometric mean."""
    from .ui import render

    if len(artists) < 2:
        _fail("give at least two artists to blend")

    config = _load_config()
    limit = int(_setting(limit, config, "limit"))
    min_score = float(min_score or 0.0)
    output = Output(_setting(output, config, "output"))

    with _client(ctx, config, refresh) as client:
        results = _blend_or_fail(client, artists, limit, min_score)

    if output is Output.plain:
        render.blend_plain(results)
    elif output is Output.json:
        render.blend_json(artists, results)
    elif output is Output.links:
        render.blend_links(results)
    else:
        render.blend_table(artists, results)


@app.command()
def recent(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many to show.")] = 20,
) -> None:
    """Artists you have looked at, most recent first."""
    from .store.history import History

    names = History().names()[:limit]
    if not names:
        typer.echo("no artists yet — look one up first", err=True)
        raise typer.Exit(1)
    for name in names:
        typer.echo(name)


@app.command()
def config() -> None:
    """Show the config file, its path and the values lfm will use."""
    from .config import DEFAULTS

    loaded = _load_config()
    state = "found" if loaded.exists else "not found (using defaults)"
    typer.echo(f"{loaded.path}  [{state}{_config_mode(loaded)}]")
    typer.echo(f"api_key = {loaded.key_source()}")
    # Where the credentials come from, never what they are: this output ends up
    # in bug reports and scrollback.
    typer.echo(f"secret = {loaded.secret_source()}")
    typer.echo(f"session_key = {loaded.session_source()}")
    typer.echo(f"scrobbling = {'on' if loaded.scrobbling() else 'off'}")
    for key in DEFAULTS:
        typer.echo(f"{key} = {loaded.get(key)!r}")


@app.command()
def auth() -> None:
    """Authorise scrobbling: approve lfm on last.fm once, and keep the key.

    The desktop flow, per the how-to: ask for a token, send you to last.fm to
    approve it, then trade it for a session key that goes in the config file.
    Needs `api_key` and `secret` first — both are on the API account page. A
    token is only good for an hour, so approve it while the prompt is waiting.
    """
    import webbrowser

    from .api.client import BadSignature, LastfmError, Scrobbler
    from .config import ENV_SECRET, ConfigError, save_value

    loaded = _load_config()
    _warn_if_the_credentials_are_readable(loaded)
    key = _api_key(loaded)
    secret = loaded.secret()
    if not secret:
        _fail(
            "no secret to sign the authorisation with — the API account's shared "
            f'secret belongs in {loaded.path} as secret = "..." or in ${ENV_SECRET}'
        )

    async def flow() -> tuple[str, str]:
        scrobbler = Scrobbler(key, secret, "")
        token = await scrobbler.auth_token()
        url = f"https://www.last.fm/api/auth/?api_key={key}&token={token}"
        typer.echo("Approve lfm on Last.fm, then come back here:\n")
        typer.echo(f"  {url}\n")
        with suppress(Exception):  # a headless machine still has the URL above
            webbrowser.open(url)
        typer.echo("Press Enter once it is approved (the token lasts an hour).", nl=False)
        input()
        return await scrobbler.auth_session(token)

    try:
        session_key, username = asyncio.run(flow())
    except BadSignature as exc:
        _fail(str(exc))
    except LastfmError as exc:
        _fail(f"could not authorise: {exc}")

    try:
        save_value(loaded.path, "session_key", session_key)
        save_value(loaded.path, "scrobble", True)
    except ConfigError as exc:
        _fail(str(exc))
    typer.echo(f"authorised as {username or 'you'}  ·  session key in {loaded.path}")
    typer.echo("scrobbling is on: `lfm scrobble --dry-run` shows what would be sent")


@app.command()
def scrobble(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what is waiting; send nothing.")
    ] = False,
    drop: Annotated[
        bool, typer.Option("--drop", help="Throw the waiting plays away instead of sending them.")
    ] = False,
    on: Annotated[
        bool | None, typer.Option("--on/--off", help="Switch scrobbling on or off.")
    ] = None,
) -> None:
    """Send the plays waiting to be scrobbled, look at them, or throw them away."""
    from .api.client import LastfmError, Scrobbler
    from .config import ConfigError, save_value
    from .playback.scrobble import Outbox, Scrobbling, ledger_path

    loaded = _load_config()
    outbox = Outbox()

    if on is not None:
        try:
            save_value(loaded.path, "scrobble", on)
        except ConfigError as exc:
            _fail(str(exc))
        loaded = _load_config()
        typer.echo(f"scrobbling {'on' if on else 'off'}  ({loaded.path})")
        if not on:
            # The queue is not touched: switching off stops what goes out, it
            # does not decide what is thrown away.
            typer.echo(f"{len(outbox.entries())} waiting; `lfm scrobble --drop` forgets them")
            return

    waiting = outbox.entries()
    if dry_run:
        if not waiting:
            typer.echo("nothing waiting")
            return
        for play in waiting:
            typer.echo(play.line())
        typer.echo(f"\n{len(waiting)} waiting — `lfm scrobble` sends them, `--drop` forgets them")
        return

    if drop:
        typer.echo(f"dropped {outbox.clear()} waiting")
        return

    if not loaded.scrobbling():
        missing = [
            name
            for name, present in (
                ("scrobble = true", bool(loaded.get("scrobble"))),
                ("secret", bool(loaded.secret())),
                ("session_key (lfm auth)", bool(loaded.session_key())),
            )
            if not present
        ]
        _fail("scrobbling is not set up: missing " + ", ".join(missing))

    scrobbler = Scrobbler(loaded.api_key(), loaded.secret(), loaded.session_key())
    try:
        drained = asyncio.run(Scrobbling(scrobbler, outbox).drain())
    except LastfmError as exc:
        _fail(str(exc))
    if not drained.ok:
        # The queue is intact and will be tried again, but this run did not do
        # what it was asked to, so it does not pretend otherwise.
        _fail(drained.summary())
    typer.echo(drained.summary())
    if drained.accepted:
        # Last.fm has no way to remove a scrobble, so say where the record of
        # what was sent lives while it is still worth knowing.
        typer.echo(f"recorded in {ledger_path()} — that is the list to delete from, by hand")


@app.command()
def cache(
    clear: Annotated[
        bool, typer.Option("--clear", help="Delete every cached response instead of listing it.")
    ] = False,
    prune: Annotated[
        bool,
        typer.Option("--prune", help="Delete only the expired responses, keeping the fresh ones."),
    ] = False,
) -> None:
    """What lfm has cached (Last.fm responses, kept 7-30 days), or empty it."""
    from .store.cache import Cache

    if clear and prune:
        # Asking for both is a contradiction, not a precedence puzzle.
        _fail("--clear throws away everything and --prune keeps what is fresh — ask for one")
    path = _cache_db()
    if not path.exists():
        typer.echo("nothing cached yet")
        return
    with Cache(path) as store:
        if not store.available:  # there is a file, but it is not a usable database
            _fail(f"cannot open {path}")
        if clear:
            removed = store.clear()
            typer.echo(f"cleared {removed} {'entry' if removed == 1 else 'entries'}")
            return
        if prune:
            removed = store.prune()
            typer.echo(f"pruned {removed} expired {'entry' if removed == 1 else 'entries'}")
            return
        total, fresh = store.counts()
    typer.echo(f"{path}  ({_human_size(path)})")
    typer.echo(f"  {total} {'entry' if total == 1 else 'entries'}, {total - fresh} expired")


@app.command()
def tui(
    ctx: typer.Context,
    artist: Annotated[
        str | None,
        typer.Argument(help="Artist to open; prompted for if omitted.", show_default=False),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="How many similar artists (config/30)."),
    ] = None,
    min_match: Annotated[
        float | None,
        typer.Option("--min-match", min=0.0, max=1.0, help="Drop below this match (config/0)."),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the cache (still refreshes it).")
    ] = False,
    volume: Annotated[
        int | None,
        typer.Option(
            "--volume",
            min=0,
            max=130,
            help="Starting volume 0-130 (config/100), remembered for next time.",
        ),
    ] = None,
    visualizer: Annotated[
        bool | None,
        typer.Option(
            "--visualizer/--no-visualizer",
            help="EQ visualiser under the header (config/on, needs cava).",
        ),
    ] = None,
) -> None:
    """Look up an artist interactively; l lists similar artists, Enter plays a track."""
    from .config import ConfigError, save_value
    from .playback.player import MpvPlayer
    from .playback.visualizer import make_cava
    from .ui.app import LfmApp
    from .ui.options import TuiOptions

    loaded = _load_config()

    said = False

    def remember_volume(level: int) -> str | None:
        nonlocal said
        try:
            save_value(loaded.path, "volume", level)
        except ConfigError as exc:
            if not said:  # say it once; repeating it on every key press is noise
                said = True
                return str(exc)
        return None

    limit = int(limit if limit is not None else loaded.values.get("limit", 30))
    min_match = float(_setting(min_match, loaded, "min_match"))
    visualizer = _setting(visualizer, loaded, "visualizer")
    player = MpvPlayer()
    if volume is not None:
        # An explicit level is a choice worth keeping, like pressing `[`/`]`.
        try:
            save_value(loaded.path, "volume", volume)
        except ConfigError as exc:
            typer.secho(f"warning: {exc}", fg=typer.colors.YELLOW, err=True)
    player.volume = float(volume if volume is not None else loaded.get("volume"))

    options = TuiOptions(
        artist=artist,
        limit=limit,
        min_match=min_match,
        theme=str(loaded.get("theme")),
        spectrum=make_cava if visualizer else None,
        save_volume=remember_volume,
        scrobbling=_scrobbling(loaded),
    )
    with _client(ctx, loaded, refresh) as client:
        LfmApp(client, player, options).run()


if __name__ == "__main__":
    app()
