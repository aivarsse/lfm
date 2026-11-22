"""Rendering for the CLI, plus the small display helpers the TUI shares.

Rich already drops colour for non-TTY streams and when `NO_COLOR` is set, so
`table` output is safe to pipe; `plain`, `json` and `links` never use Rich at
all, which is what makes them scriptable.
"""

import html
import json
import re
from collections.abc import Callable
from urllib.parse import quote_plus

from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from ..api.models import ArtistInfo, SimilarArtist, SimilarTrack, Track
from ..api.rank import BlendArtist, WalkedArtist

# How many tags the artist card shows, and how much of the bio.
MAX_TAGS = 10  # the TUI's tag window is ten rows; the CLI clips to the same
BIO_CHARS = 600

_TAG_RE = re.compile(r"<[^>]+>")
_READ_MORE_RE = re.compile(r"\s*Read more on Last\.fm\s*$")


def bio_text(summary: str) -> str:
    """Last.fm bios are HTML with a trailing "Read more" link; flatten for display."""
    text = html.unescape(_TAG_RE.sub("", summary)).strip()
    text = _READ_MORE_RE.sub("", text)
    if len(text) > BIO_CHARS:
        text = text[: BIO_CHARS - 1].rstrip() + "…"
    return text


UNITS = ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"))


def compact_count(value: int) -> str:
    """A count for reading: exact under a million, short above it (13.9M, 1.1B, 1T).

    The CLI's `plain` and `json` output stay exact — those are for scripts — so this
    is used where a human is scanning numbers, and the boundary is the point: a
    six-figure count is still worth every digit. A value that rounds up to a
    thousand of its unit rolls into the next one, or 999,999,999,999 reads 1000B.
    """
    if value < 1_000_000:
        return f"{value:,}"
    for index, (scale, suffix) in enumerate(UNITS):
        if value < scale:
            continue
        scaled = value / scale
        if round(scaled, 1) >= 1000 and index > 0:
            scale, suffix = UNITS[index - 1]
            scaled = value / scale
        return f"{scaled:.1f}{suffix}".replace(f".0{suffix}", suffix)
    return f"{value:,}"  # unreachable while UNITS covers every larger unit


def one_line(text: str) -> str:
    """Other people's names on one line: control characters become spaces.

    Last.fm and YouTube titles are not ours. A newline grows a row or the player
    bar by a line; a BEL or ESC in the window title would end the OSC 2 sequence
    early, and the rest of the title would be read as terminal commands. C1
    controls and the Unicode line separators have the same effect as C0.
    """
    flattened = "".join(
        " " if ord(char) < 32 or 127 <= ord(char) < 160 or char in ("\u2028", "\u2029") else char
        for char in text
    )
    return flattened.strip()


def truncated(text: str, limit: int) -> str:
    """One line, at most `limit` cells, with an ellipsis when something is lost.

    Cell-aware, so a CJK name loses the right number of columns; used for page
    titles and tab labels, where a name can be longer than the row allows.
    """
    text = one_line(text)
    if cell_len(text) <= limit:
        return text
    cut = Text(text)
    cut.truncate(limit, overflow="ellipsis")
    plain = cut.plain
    return plain[:-2] + "…" if plain.endswith(" …") else plain


def exact_count(value: int) -> str:
    """The plain thousands-separated form, for the CLI."""
    return f"{value:,}"


def stats_line(info: ArtistInfo, count: Callable[[int], str] = exact_count) -> str:
    """Listeners and plays, formatted by `count` (the TUI passes `compact_count`)."""
    listeners = f"{count(info.stats.listeners)} listeners"
    plays = f"{count(info.stats.playcount)} plays"
    return f"{listeners} · {plays}"


def _match_style(match: float) -> str:
    if match >= 0.8:
        return "green"
    if match >= 0.5:
        return "yellow"
    return "dim"


def profile_url(name: str) -> str:
    return f"https://www.last.fm/music/{quote_plus(name)}"


def table(
    corrected: str | None,
    artists: list[SimilarArtist],
    shared: list[list[str]] | None = None,
    listeners: list[int | None] | None = None,
) -> None:
    """The ranked table; `listeners` (one per artist) adds the why-am-I-seeing-this column."""
    console = Console()
    if corrected:
        console.print(f"→ [italic]{escape(corrected)}[/italic] [dim](corrected)[/dim]")
    t = Table(show_header=True, header_style="bold")
    t.add_column("#", justify="right", style="dim")
    t.add_column("MATCH", justify="right")
    t.add_column("ARTIST")
    if listeners is not None:
        t.add_column("LISTENERS", justify="right", style="dim")
    if shared is not None:
        t.add_column("SHARED")
    for i, artist in enumerate(artists):
        row = [
            str(i + 1),
            f"[{_match_style(artist.match)}]{artist.match:.0%}[/]",
            Text(artist.name),
        ]
        if listeners is not None:
            count = listeners[i]
            row.append(f"{count:,}" if count is not None else "—")
        if shared is not None:
            row.append(Text(", ".join(shared[i]), style="dim"))
        t.add_row(*row)
    console.print(t)


def plain(artists: list[SimilarArtist]) -> None:
    """One artist per line, nothing else — built for pipes."""
    for artist in artists:
        print(artist.name)


def links(artists: list[SimilarArtist]) -> None:
    """One Last.fm URL per line."""
    for artist in artists:
        print(artist.url or profile_url(artist.name))


def similar_json(
    artist: str,
    corrected: str | None,
    artists: list[SimilarArtist],
    shared: list[list[str]] | None = None,
    listeners: list[int | None] | None = None,
) -> None:
    """Stable shape for jq: the canonical name, what was asked for, and the rows."""
    results = []
    for i, a in enumerate(artists):
        row = {"name": a.name, "match": round(a.match, 4), "url": a.url or profile_url(a.name)}
        if listeners is not None:
            row["listeners"] = listeners[i]
        if shared is not None:
            row["shared_tags"] = shared[i]
        results.append(row)
    payload = {"requested": artist, "artist": corrected or artist, "results": results}
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def info_table(requested: str, info: ArtistInfo, tags: list[str], tracks: list[Track]) -> None:
    console = Console()
    if info.name and info.name != requested:
        console.print(f"→ [italic]{escape(info.name)}[/italic] [dim](corrected)[/dim]")
    console.print(Text(info.name, style="bold"))
    console.print(f"[dim]{stats_line(info)}[/dim]")
    console.print(f"[dim]{escape(info.url or profile_url(info.name))}[/dim]")
    if tags:
        console.print("[italic dim]" + escape(" · ".join(tags[:MAX_TAGS])) + "[/italic dim]")
    if tracks:
        t = Table(show_header=True, header_style="bold")
        t.add_column("#", justify="right", style="dim")
        t.add_column("TRACK")
        t.add_column("PLAYS", justify="right", style="dim")
        for i, track in enumerate(tracks, 1):
            t.add_row(str(i), Text(track.name), f"{track.playcount:,}")
        console.print(t)
    bio = bio_text(info.bio.summary)
    if bio:
        # Text, not str: rich would read a bio's brackets as markup, so
        # "[citation needed]" disappears and "[/]" raises MarkupError.
        console.print(Text(bio))


def info_plain(info: ArtistInfo, tags: list[str], tracks: list[Track]) -> None:
    print(info.name)
    print(stats_line(info))
    print(info.url or profile_url(info.name))
    if tags:
        print(", ".join(tags[:MAX_TAGS]))
    for track in tracks:
        print(f"{track.name}\t{track.playcount}")
    bio = bio_text(info.bio.summary)
    if bio:
        print(bio)


def info_json(requested: str, info: ArtistInfo, tags: list[str], tracks: list[Track]) -> None:
    payload = {
        "requested": requested,
        "artist": info.name,
        "url": info.url or profile_url(info.name),
        "listeners": info.stats.listeners,
        "playcount": info.stats.playcount,
        "tags": tags[:MAX_TAGS],
        "top_tracks": [{"name": t.name, "playcount": t.playcount} for t in tracks],
        "bio": bio_text(info.bio.summary),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def blend(low: str, high: str, fraction: float) -> str:
    """A colour partway between two `#rrggbb` values, by the fraction 0..1."""
    fraction = min(1.0, max(0.0, fraction))
    parts = []
    for index in (1, 3, 5):
        start, end = int(low[index : index + 2], 16), int(high[index : index + 2], 16)
        parts.append(round(start + (end - start) * fraction))
    return "#" + "".join(f"{part:02x}" for part in parts)


def ease_in_out_cubic(x: float) -> float:
    """Textual's default easing curve, for a fade that moves like its own transitions.

    The tab bar's `transition: color 150ms` runs on this curve; a fade drawn by hand
    on the same curve and duration reads as the same kind of movement rather than as a
    second animation with its own personality.
    """
    x = min(1.0, max(0.0, x))
    if x < 0.5:
        return 4 * x * x * x
    return 1 - pow(-2 * x + 2, 3) / 2


def progress_bar(position: float, duration: float, width: int, colour: str) -> Text:
    """The progress line: a solid run, one brighter playhead, and a dim rest.

    One colour rather than a gradient across the run: the bar's job is to say how far in
    you are at a glance, and shading it lengthwise made the colour carry meaning nobody
    asked for while dimming the end the eye is looking for. The playhead is one cell
    lighter than the run — the point you are at, not only the length behind it.
    """
    filled = 0 if duration <= 0 else round(width * min(position, duration) / duration)
    filled = min(width, max(0, filled))
    text = Text()
    # One append per region: a span then means a region rather than a single cell.
    if filled:
        if filled > 1:
            text.append("━" * (filled - 1), style=colour)
        text.append("━", style=blend(colour, "#ffffff", 0.35))
    if width > filled:
        text.append("━" * (width - filled), style="dim")
    return text


VOLUME_LEVELS = "▁▂▃▄▅▆▇█"


def volume_glyph(volume: float, ceiling: float = 130.0) -> str:
    """One block for the volume, so the number has a shape beside it."""
    step = min(len(VOLUME_LEVELS) - 1, int(volume / ceiling * len(VOLUME_LEVELS)))
    return VOLUME_LEVELS[max(0, step)]


def track_url(artist: str, name: str) -> str:
    """Last.fm's own track URL scheme, for when the API gave us no url."""
    return f"https://www.last.fm/music/{quote_plus(artist)}/_/{quote_plus(name)}"


def radio_table(artist: str, track: str, results: list[SimilarTrack]) -> None:
    """The first hop of a radio: what is close to this song."""
    console = Console()
    console.print(f"[dim]{escape(artist)} — {escape(track)}[/dim]")
    t = Table(show_header=True, header_style="bold")
    t.add_column("#", justify="right", style="dim")
    t.add_column("MATCH", justify="right")
    t.add_column("ARTIST")
    t.add_column("TRACK")
    for i, result in enumerate(results, 1):
        t.add_row(
            str(i),
            # raw score: track.getSimilar is not a 0..1 ratio (docs example: 10.95)
            f"[dim]{result.match:>5.2f}[/]",
            Text(result.artist),
            Text(result.name),
        )
    console.print(t)


def radio_plain(results: list[SimilarTrack]) -> None:
    """One artist and track per line, tab-separated, as `info --plain` does."""
    for result in results:
        print(f"{result.artist}\t{result.name}")


def radio_links(results: list[SimilarTrack]) -> None:
    for result in results:
        print(result.url or track_url(result.artist, result.name))


def radio_json(artist: str, track: str, results: list[SimilarTrack]) -> None:
    payload = {
        "requested": {"artist": artist, "track": track},
        "results": [
            {
                "name": result.name,
                "artist": result.artist,
                "match": round(result.match, 4),
                "url": result.url or track_url(result.artist, result.name),
            }
            for result in results
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def info_links(info: ArtistInfo) -> None:
    print(info.url or profile_url(info.name))


def blend_table(seeds: list[str], results: list[BlendArtist]) -> None:
    console = Console()
    console.print(f"[dim]Blend of {escape(' + '.join(seeds))}[/dim]")
    t = Table(show_header=True, header_style="bold")
    t.add_column("#", justify="right", style="dim")
    t.add_column("SCORE", justify="right")
    t.add_column("ARTIST")
    for i, artist in enumerate(results, 1):
        t.add_row(
            str(i),
            f"[{_match_style(artist.score)}]{artist.score:.0%}[/]",
            Text(artist.name),
        )
    console.print(t)


def blend_plain(results: list[BlendArtist]) -> None:
    for artist in results:
        print(artist.name)


def blend_links(results: list[BlendArtist]) -> None:
    for artist in results:
        print(artist.url or profile_url(artist.name))


def blend_json(seeds: list[str], results: list[BlendArtist]) -> None:
    payload = {
        "seeds": seeds,
        "results": [
            {
                "name": a.name,
                "score": round(a.score, 4),
                "url": a.url or profile_url(a.name),
                "matches": {seed: round(match, 4) for seed, match in a.matches.items()},
            }
            for a in results
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def walk_table(requested: str, results: list[WalkedArtist], depth: int) -> None:
    """What the walk found; the hops column says why the scores are as small as they are."""
    console = Console()
    console.print(
        f"[dim]Walk from {escape(requested)} · {depth} hop{'s' if depth != 1 else ''}[/dim]"
    )
    t = Table(show_header=True, header_style="bold")
    t.add_column("#", justify="right", style="dim")
    t.add_column("SCORE", justify="right")
    t.add_column("HOPS", justify="right", style="dim")
    t.add_column("ARTIST")
    for i, artist in enumerate(results, 1):
        # The raw score, not a percentage, for the radio's reason: a path score is not a
        # ratio of anything a listener would recognise.
        t.add_row(str(i), f"{artist.score:.2f}", str(artist.hops), Text(artist.name))
    console.print(t)


def walk_plain(results: list[WalkedArtist]) -> None:
    """One artist per line, nothing else — built for pipes."""
    for artist in results:
        print(artist.name)


def walk_links(results: list[WalkedArtist]) -> None:
    for artist in results:
        print(artist.url or profile_url(artist.name))


def walk_json(requested: str, depth: int, results: list[WalkedArtist]) -> None:
    payload = {
        "requested": requested,
        "depth": depth,
        "results": [
            {
                "name": a.name,
                "score": round(a.score, 4),
                "hops": a.hops,
                "url": a.url or profile_url(a.name),
            }
            for a in results
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
