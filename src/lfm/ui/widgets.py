"""Shared pieces: row builders, the prompt, the player bar, small app helpers."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import httpx
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Key
from textual.screen import Screen
from textual.suggester import Suggester, SuggestFromList
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from ..api.client import LastfmError
from ..api.models import (
    Album,
    AlbumInfo,
    AlbumTrack,
    SearchArtist,
    SimilarArtist,
    SimilarTrack,
    TagArtist,
    Track,
)
from ..playback.player import (
    PlayerStatus,
    YtResult,
    format_time,
)
from ..playback.visualizer import (
    SpectrumFactory,
)

if TYPE_CHECKING:
    from .app import LfmApp
from ..api.protocols import (
    DetailClient,
)
from .render import (
    blend,
    compact_count,
    one_line,
    progress_bar,
    truncated,
    volume_glyph,
)

HISTORY_HINT = "  ·  Tab for recent  ·  Del to forget"


def track_row(track: Track, status: str = "") -> tuple[ListItem, Label]:
    """A track row: a playback marker, the dim play count, then the title.

    Returns the row and its marker label, so the screen can flip it between
    playing (▶), paused (⏸) and not loaded (blank) without rebuilding the list.
    """
    marker = Label(status, classes="status")
    text = Text()
    text.append(f"{compact_count(track.playcount):>7}  ", style="dim")
    text.append(one_line(track.name))
    return ListItem(Horizontal(marker, RowLabel(text))), marker


def clock(seconds: int) -> str:
    """3:58, or 1:02:17 when an album runs past the hour."""
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def more_row(count: int) -> Text:
    """The row that reveals the next batch: it was fetched and ranked already."""
    return Text(f"↓  show {count} more", style="dim italic")


def album_row(album: Album, count: int | None = None) -> tuple[ListItem, Label]:
    """An albums-tab row: the dim play count, the name, and its song count."""
    marker = Label("", classes="status")
    text = Text()
    text.append(f"{compact_count(album.playcount):>7}  ", style="dim")
    text.append(one_line(album.name))
    if count:
        text.append(f"  ·  {count} track{'s' if count != 1 else ''}", style="dim")
    return ListItem(Horizontal(marker, RowLabel(text))), marker


def album_track_row(track: AlbumTrack, status: str = "") -> tuple[ListItem, Label]:
    """A tracklist row: the rank, the name, then the duration Last.fm knows.

    The duration trails the name the way the other lists' extra detail does. It
    used to sit between the rank and the name, which put the name nine cells
    from the number it belongs to.
    """
    marker = Label(status, classes="status")
    text = Text()
    text.append(f"{track.rank:>2}  " if track.rank else " " * 4, style="dim")
    text.append(one_line(track.name))
    if track.duration:
        text.append(f"  ·  {clock(track.duration)}", style="dim")
    return ListItem(Horizontal(marker, RowLabel(text))), marker


def album_meta_line(info: AlbumInfo) -> str:
    """One line under the tabs: how loved, how long.

    It used to lead with the release date, from the `releasedate` field `album.getInfo` no longer
    returns — checked live across four albums, including the one the fixture was recorded from,
    and the key is simply absent. The date survives only in the album's wiki prose, which is not
    a field to read, so the line does without it rather than pretending.
    """
    parts = []
    if info.listeners:
        parts.append(
            f"{compact_count(info.listeners)} listeners · {compact_count(info.playcount)} plays"
        )
    if info.tracks:
        parts.append(f"{len(info.tracks)} track{'s' if len(info.tracks) != 1 else ''}")
    total = sum(track.duration for track in info.tracks)
    if total:
        parts.append(clock(total))
    return "   ·   ".join(parts)


def source_row(result: YtResult, *, chosen: bool) -> Text:
    """Duration, title and channel, with a marker on the source already picked."""
    # Text() with no base style: the marker is green, the rest is not
    row = Text()
    row.append("▶ " if chosen else "  ", style="green")
    row.append(f"{result.duration:>6}  ", style="dim")
    row.append(one_line(result.title))
    if result.channel:
        row.append(f"  ·  {one_line(result.channel)}", style="dim")
    return row


def plain(text: str) -> Text:
    """External text as a Text, so Textual does not read its brackets as markup.

    A `Static` or `Label` given a `str` parses it as markup, and unknown tags are
    dropped silently: mpv's `media-title` for a YouTube result ("Archangel
    [Official Video]") loses the bracketed part, and a bio can lose phrases.
    Anything carrying an artist, track, tag, title or bio goes through here.
    """
    return Text(text)


# 84 screen columns is about 76 inside the box (90%, capped at 84, less its border and
# padding): below that a pair of columns wraps every description instead of holding one.
class PromptInput(Input):
    """The artist prompt: Tab takes the suggestion, or moves on when there is none.

    Handled in the widget because this is what has the keyboard: an app or screen binding
    for Tab would have to fight the artist page's own Tab, which walks its track tabs, and
    whoever won would be wrong somewhere. Accepting is Textual's own path — what the right
    arrow does — and with nothing to accept the key is passed on, so Tab stays focus-next
    and the second press lands in the recent list.
    """

    async def _on_key(self, event: Key) -> None:
        if (
            event.key == "tab"
            and self.cursor_at_end
            and len(self.value) < len(self._suggestion or "")
        ):
            self.action_cursor_right()  # Textual's own acceptance
            event.stop()
            return
        await super()._on_key(event)


SUGGEST_DELAY = 0.2  # let the next keystroke land before asking Last.fm
SUGGEST_MIN_CHARS = 3  # two letters name nothing specific: "Ra" fits Ra Ra Riot
# How far ahead of its runner-up the winner must be. Nothing stops two popular acts sharing a
# prefix — "the" fits The Beatles and The Killers — and then neither is the answer. On the live
# counts quoted below, "bur" wins by 4.1x and "mich" by 7.2x, so 3.0 lets those through and a
# 2.2x pair does not. Judgement rather than measurement: see the issue.
RUNNER_UP_MARGIN = 3.0
# A generous page: `artist.search` is a relevance search, not a prefix index, so the act you
# mean is often well down the list — and the completion picks by listeners, not by rank.
SUGGEST_LIMIT = 30


class PromptSuggester(Suggester):
    """What the prompt offers as you type: a remembered name first, then Last.fm's.

    History answers instantly and needs no network, so it goes first and most typing
    needs nothing else. When it has nothing, the artist search fills in the rest — for
    the last keystroke only, because the earlier ones return before they ask. Typing
    "bur" is one request, not three, and the suggester's own cache means the same prefix
    is never asked for twice.
    """

    def __init__(self, client: DetailClient) -> None:
        super().__init__(case_sensitive=False)
        self.client = client
        self.recents = SuggestFromList([], case_sensitive=False)
        self._latest = ""

    def set_recents(self, recents: list[str]) -> None:
        self.recents = SuggestFromList(recents, case_sensitive=False)

    async def get_suggestion(self, value: str) -> str | None:
        typed = value.strip()
        if len(typed) < SUGGEST_MIN_CHARS:
            return None
        remembered = await self.recents.get_suggestion(value)
        if remembered:
            return remembered
        self._latest = typed
        await asyncio.sleep(SUGGEST_DELAY)
        if typed != self._latest:
            return None  # a later keystroke owns the answer
        try:
            matches = await self.client.search_matches(typed, limit=SUGGEST_LIMIT)
        except (LastfmError, httpx.HTTPError):
            return None  # a suggestion is never worth interrupting typing for
        return self._completion(typed, matches)

    @staticmethod
    def _completion(typed: str, matches: list[SearchArtist]) -> str | None:
        """The artist to complete to, or nothing at all.

        `artist.search` answers a relevance search, not a prefix one. For most prefixes it echoes
        the query back as an artist in its own right — "Mich", "Michael J", "Radiohe" are real
        entries with a handful of listeners — and it ranks those above the act you mean. So
        neither the rank nor "the first name that starts with it" can be trusted:

        - complete to the *most listened* artist whose name extends what was typed, not the first;
        - only when that artist beats whatever the typed text already names. That is the bar that
          leaves "the Beatles" alone instead of offering "The Beatles Tribute Band" (69,703
          listeners against 6,685,905), and "Michael Jackson" alone instead of chaining into
          "Michael Jackson & Janet Jackson". It also fires on every echo: "mich" *is* an artist
          with 44,313 listeners, so "Mich Gerber" (35,425) is not an answer to it.

        - and only when it beats the *next* candidate by `RUNNER_UP_MARGIN`. Beating the echo
          alone let any popular act through a prefix that names nothing: "the" has no artist of
          its own to beat, so "The Killers" won by default. An ambiguous prefix completes to
          nothing now, which beats landing on an act you did not mean.
        """
        folded = typed.casefold()
        typed_runs = max((m.listeners for m in matches if m.name.casefold() == folded), default=0)
        best: SearchArtist | None = None
        runner_up: SearchArtist | None = None
        for match in matches:
            if not (match.name.casefold().startswith(folded) and len(match.name) > len(typed)):
                continue
            if best is None or match.listeners > best.listeners:
                runner_up, best = best, match
            elif runner_up is None or match.listeners > runner_up.listeners:
                runner_up = match
        if best is None or best.listeners <= typed_runs:
            return None
        if runner_up is not None and runner_up.listeners * RUNNER_UP_MARGIN >= best.listeners:
            return None  # "the" fits The Beatles and The Killers; neither is the answer
        return best.name


class IsolatedList(ListView):
    """A list that keeps the mouse wheel while it has anywhere to scroll.

    Textual stops a scroll event only when the list actually moved, so at the end of a
    list the event walked on to the page behind it and carried the artist page along.
    Overscroll belongs to the list: while it has a scrollbar, the page never sees the
    wheel. A list with nothing to scroll has no overscroll to keep, so it lets the
    event through, as a browser does.
    """

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.allow_vertical_scroll:
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.allow_vertical_scroll:
            event.stop()


@dataclass
class BarWidgets:
    """A bar's five widgets, wherever they are hosted."""

    progress: Static
    toggle: Button
    now_playing: Static
    playing: Static
    info: Static
    seek: tuple[Button, Button, Button]


def bar_widgets(host: Widget) -> BarWidgets:
    return BarWidgets(
        host.query_one("#progress", Static),
        host.query_one("#toggle", Button),
        host.query_one("#now-playing", Static),
        host.query_one("#playing", Static),
        host.query_one("#bar-info", Static),
        (
            host.query_one("#seek-back", Button),
            host.query_one("#seek-forward", Button),
            host.query_one("#stop", Button),
        ),
    )


# Below this the seek buttons go. It is not 56 any more: at 56 the four buttons were
# back but left the progress line two cells wide, so widening the window from 55 to 56
# made the transport worse. 68 keeps at least a dozen cells for the progress.
NARROW_BAR_WIDTH = 68


class RowLabel(Label):
    """A row's text: one line, ellipsized where the row ends.

    Textual cuts an overlong Label at the list's edge, mid-word. The width is only
    known once the row has been laid out, so the truncation lives in render.
    """

    def render(self):
        visual = super().render()
        width = self.size.width
        if width and isinstance(visual, Content) and visual.cell_length > width:
            return visual.truncate(width, ellipsis=True)
        return visual


def screen_title(screen: Widget, text: str) -> Text:
    """A page title, leaving the row's right side for the stats or context."""
    budget = max(20, (screen.size.width or 100) - 40)
    return plain(truncated(text, budget))


def queue_note(app: "LfmApp") -> Text:
    """`3/10 · Untrue`, with the position in body ink: that is the part that changes."""
    position, separator, rest = one_line(app.queue_note).partition(" · ")
    bright = app.theme_colour("foreground", "#e8e3da")
    if not separator:
        return Text(position, style=bright)
    # the air goes at the end of the bright run: Textual drops a run's leading spaces
    text = Text(f"{position} ", style=bright)
    text.append((separator + rest).lstrip(" "))
    return text


# U+21BB. The fancier `⟳` (U+27F3) is not in every monospace font, and where kitty has
# to fall back for it, it draws the glyph wider than the one cell the bar gives it, so
# the ink sits off-centre between the dots. `↻` stays in its cell in the fonts that have
# it; bold and the accent give it the weight the plain glyph lacked.
REPEAT_GLYPH = "↻"


def repeat_mark(app: "LfmApp") -> Text:
    """The repeat mark: grey while off, the accent once on, a fade between.

    The fade eases the glyph's own colour from the muted ink to the accent, on the tab
    bar's curve and timing -- no filled cell, so nothing in the bar moves.
    """
    muted = app.theme_colour("text-muted", "#acacb5")
    if not app.repeat_on:
        return Text(REPEAT_GLYPH, style=f"bold {muted}")
    accent = app.theme_colour("accent", "#7aa2d6")
    faded = app.repeat_fade()
    colour = accent if faded >= 1.0 else blend(muted, accent, faded)
    return Text(REPEAT_GLYPH, style=f"bold {colour}")


def draw_player_bar(widgets: BarWidgets, app: "LfmApp", status: PlayerStatus) -> None:
    """Draw one bar: the same five widgets on every screen, and on the home page."""
    # The transport line is wider than a small terminal: the seek buttons give up their
    # room first, which is what was clipping `0:00 / 0:00` and eating the progress line.
    for button in widgets.seek:
        button.display = app.size.width >= NARROW_BAR_WIDTH
    width = widgets.progress.size.width or 40  # no width before the first layout
    widgets.progress.update(
        progress_bar(status.position, status.duration, width, app.progress_colour())
    )
    # five cells either way: same length, so both labels centre on the same
    # column (a four-cell "Play" against a five-cell "Pause" cannot)
    widgets.toggle.label = "Pause" if status.playing and not status.paused else "Start"
    right: list[Text] = []
    if status.playing:
        widgets.now_playing.update(plain(one_line(status.title) or "Playing"))
        if app.queue_note:
            right.append(queue_note(app))
        playing = Text(
            f"{format_time(status.position)} ",
            style=app.theme_colour("foreground", "#e8e3da"),
        )
        playing.append(f"/ {format_time(status.duration)}")
        widgets.playing.update(playing)
    else:
        # resolving takes a moment or two: say so rather than claiming silence
        widgets.now_playing.update(
            plain(f"Resolving {app.resolving}…" if app.resolving else "Nothing playing")
        )
        widgets.playing.update("0:00 / 0:00")
    right.append(repeat_mark(app))
    # `m` stops the player's volume at zero; say why the track is silent rather than
    # showing another "vol 0%", which reads like a broken play.
    muted = app.muted_volume is not None and status.volume == 0
    if muted:
        right.append(Text("muted"))
    else:
        right.append(Text("vol "))
    info = Text()
    for index, part in enumerate(right):
        if index:
            # The dot starts the run; a run that begins with spaces has them dropped,
            # so the air before the dot belongs to the end of the part before it.
            info.append("·  ")
        if index < len(right) - 1:
            part.append("  ")
        info.append_text(part)
    if not muted:
        # The glyph keeps the widget's colour and gains a background: the meter's full
        # height, of which the glyph is the filled part.
        info.append(volume_glyph(status.volume), style=f"on {app.volume_track()}")
        # Fixed width, so the glyph cannot move: `100%` and ` 95%` are both four cells,
        # and the readout is right-aligned as a block, so a shorter number would slide.
        info.append(f" {int(status.volume):>3}%")
    widgets.info.update(info)


# The prompt's wordmark: lfm in block art, drawn with `▓` over `▒` and arcs above
# and below, so the depth is in the glyphs rather than in a drawn shadow.
# Shown only on a terminal wide enough for it.
WORDMARK_ROWS = (
    "   ▄▀▄            ▄▀▄            ▄▀▄          ▄▀▄",
    " ▄▀▄█▄▀▄        ▄▀▄█▄▀▀▀▀▀▀▀▄  ▄▀▄█▄▀▄ ▄▀▀▄ ▄▀▄█▄▀▄",
    "█ █████ █      █ ██████████ █ █ █████ ▀▄██▄▀ █████ █",
    "█ ▓████ █      █ ▓███▓▀▀▀▀▀▄▀ █ ▓████████████████▓ █",
    "█ ▓▓▓▓▓ █      █ ▓▓▓▓▓ ▀▀▀▀▄  █ ▓▓▓▓▓ ▀▓▓▓▓▀ ▓▓▓▓▓ █",
    "█ ▓▓▓▓▓ █      █ ▓▓▓▓▓▓▓▓▓ █  █ ▓▓▓▓▓  ▀▓▓▀  ▓▓▓▓▓ █",
    "█ ▓▓▓▓▓ ▀▀▀▀▀▄ █ ▓▓▓▓▓▀▀▀▀▄▀  █ ▓▓▓▓▓        ▓▓▓▓▓ █",
    "█ ▓▒▒▒▒▒▒▒▒▒ █ █ ▓▒▒▒▓        █ ▓▒▒▒▓        ▓▒▒▒▓ █",
    "▄▀▄▒▒▒▓▀▀▀▀▀▄▀ ▄▀▄▒▒▒▓ █      ▄▀▄▒▒▒▓ █    ▄▀▄▒▒▒▓ █",
    "▀▄▄▀▀▄▄▀       ▀▄▄▀▀▄▄▀       ▀▄▄▀▀▄▄▀     ▀▄▄▀▀▄▄▀",
)
WORDMARK_MIN_WIDTH = 80


def header_band(context: str) -> ComposeResult:
    """One slim row: the app's name, and which screen you are on."""
    with Horizontal(id="band"):
        yield Static(plain("lfm"), id="band-name")
        yield Static(plain(context), id="band-context")


def player_bar() -> ComposeResult:
    """The player bar's widgets, so every screen can host the same one."""
    with Vertical(id="player-bar"):
        with Horizontal(id="now-playing-row"):
            yield RowLabel(plain("Nothing playing"), id="now-playing")
            yield Static("", id="bar-info")
        with Horizontal(id="controls"):
            for button in (
                Button("-15s", id="seek-back", classes="player-button"),
                Button("Start", id="toggle", classes="player-button"),
                Button("+15s", id="seek-forward", classes="player-button"),
                Button("Stop", id="stop", classes="player-button"),
            ):
                button.can_focus = False  # keyboard-only for buttons, as ever
                yield button
            yield Static("0:00 / 0:00", id="playing")
            yield Static("", id="progress")


class WithPlayerBar(Screen[None]):
    _bar_handles: BarWidgets | None = None  # taken once: the bar is drawn on every tick

    """For screens that host the player bar: draw it, and let the app tick it.

    The queue itself still belongs to the artist screen; this is only the
    window onto it, so browsing away never loses the music or its keys.
    """

    @property
    def lfm(self) -> "LfmApp":
        """The app in its real type: the bar needs its player and queue state."""
        return cast("LfmApp", self.app)

    def bar_handles(self) -> BarWidgets:
        """The bar's widgets. Looked up once, because the clock redraws twice a second."""
        if self._bar_handles is None:
            self._bar_handles = bar_widgets(self)
        return self._bar_handles

    async def refresh_playback_bar(self, status: PlayerStatus | None = None) -> None:
        status = status if status is not None else await self.lfm.player.status()
        draw_player_bar(self.bar_handles(), self.lfm, status)

    async def tick_bar(self, status: PlayerStatus | None = None) -> None:
        await self.refresh_playback_bar(status)


def make_spectrum_factory(app: App[object]) -> SpectrumFactory | None:
    """The app's visualiser factory, if it has one (keeps screens decoupled)."""
    factory = getattr(app, "spectrum", None)
    return factory if callable(factory) else None


def remember_artist(artist: str) -> None:
    """Note a viewed artist for `lfm recent` and the prompt's completion."""
    from ..store.history import History

    History().add(artist)


def copy_link(app: App[object], url: str) -> None:
    """Put a link on the clipboard (OSC 52; most terminals honour it, silently)."""
    with suppress(Exception):  # a terminal that refuses the sequence is not fatal
        app.copy_to_clipboard(url)


def remember_volume(app: object, level: int) -> str | None:
    """Hand a volume change to the app's saver; None when it went through.

    A saver that cannot write (a read-only config, say) hands back the reason, so
    the screen can admit that the level will not survive this session. Failing to
    save must never stop playback, so a broken saver is a message, not an error.
    """
    saver = getattr(app, "save_volume", None)
    if not callable(saver):
        return None
    try:
        return saver(level) or None
    except Exception as exc:
        return str(exc)


def set_terminal_title(app: object, text: str) -> None:
    """Put the current track in the window title (OSC 2); terminals may ignore it.

    Textual has no public driver accessor, so this goes through whatever the app
    exposes, and does nothing if the sequence cannot be written.
    """
    driver = getattr(app, "driver", None) or getattr(app, "_driver", None)
    if driver is None:
        return
    with suppress(Exception):
        driver.write(f"\x1b]2;{one_line(text)}\x07")


def artist_row(artist: SimilarArtist, listeners: int | None = None) -> Text:
    """A similar-artist row; a deep cut also shows the count that qualified it."""
    row = Text()
    row.append(f"{artist.match:>3.0%}  ", style="dim")
    if listeners is not None:
        row.append(f"{listeners:>10,}  ", style="dim")
    row.append(one_line(artist.name))
    return row


def radio_row(entry: SimilarTrack, status: str = "") -> tuple[ListItem, Label]:
    """A radio row: the ▶ marker, the match, then someone else's artist and track."""
    marker = Label(status, classes="status")
    text = Text()
    # no match%: track.getSimilar's scores are not 0..1 (the docs example says 10.95),
    # and fallback rows have none at all — a number that lies is worse than none
    text.append(one_line(entry.artist), style="dim")
    text.append(" — ")
    text.append(one_line(entry.name))
    return ListItem(Horizontal(marker, RowLabel(text))), marker


def tag_artist_row(artist: TagArtist) -> Text:
    """The artist's name. The count this row used to lead with is not in the response any more."""
    row = Text()
    row.append(one_line(artist.name))
    return row
