"""The three smaller screens: similar artists, a tag, and the source picker."""

from collections.abc import Callable
from contextlib import suppress

import httpx
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import (
    ListItem,
    ListView,
    Static,
)

from ..api.client import MAX_LISTENER_LOOKUPS, LastfmError
from ..api.flows import listener_counts, similar_wide
from ..api.models import (
    SimilarArtist,
    TagArtist,
)
from ..api.protocols import (
    DetailClient,
)
from ..api.rank import DeepCuts, deep_cuts, rank, shared_tags
from ..playback.player import (
    YtDlpError,
    YtResult,
)
from .options import TuiOptions
from .protocols import (
    Player,
)
from .render import (
    one_line,
    profile_url,
)
from .widgets import (
    RowLabel,
    WithPlayerBar,
    artist_row,
    copy_link,
    header_band,
    more_row,
    plain,
    player_bar,
    screen_title,
    source_row,
    tag_artist_row,
)

RADIO_BATCH = 3  # rows one refill adds; keeps a dry-out to a couple of calls
# How much less known than the seed counts as a deep cut; `d` cycles through them.
DEEP_CUT_RATIOS = (10, 30)
SIMILAR_FETCHED = 100  # artist.getSimilar's maximum, in one cached call
SHOW_MORE_STEP = 10
# What an empty window says for itself, per tab.
EMPTY_HINTS = {
    "tracks": "No top tracks on Last.fm for this artist",
    "albums": "No albums on Last.fm for this artist",
    "album": "No tracklist for this album",
    "radio": "Nothing similar to play",
}


class SimilarScreen(WithPlayerBar):
    """Ranked similar artists for one artist; Enter opens their detail."""

    BINDINGS = [
        Binding("b", "app.pop_screen", "Back"),
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("d", "deep_cuts", "Deep cuts"),
        Binding("c", "copy_link", "Copy link"),
    ]

    def __init__(
        self,
        name: str,
        client: DetailClient,
        player: Player,
        options: TuiOptions,
        *,
        deep_cuts: bool = False,
        seed_listeners: int | None = None,
    ) -> None:
        super().__init__()
        self.options = options
        self.artist = name
        self.client = client
        self.player = player
        self.limit = options.limit
        self.min_match = options.min_match
        # The ranked list, and what is on screen right now (the deep cuts, if any).
        self.artists: list[SimilarArtist] = []  # what is on screen
        self._all: list[SimilarArtist] = []  # everything ranked, before deep cuts
        self._ranked: list[SimilarArtist] = []  # and after them
        self._shown = options.limit
        self._depth = 1 if deep_cuts else 0
        self._base_meta = ""
        # The artist screen has already looked this up; only fetch it if it has not.
        self._seed_listeners = seed_listeners
        self._listeners: dict[str, int] = {}
        self._tag_cache: dict[str, list[str]] = {}

    def action_copy_link(self) -> None:
        """Copy the highlighted thing's Last.fm link (OSC 52; silent by design)."""
        index = self.query_one("#artists", ListView).index
        if index is None or not 0 <= index < len(self.artists):
            return  # the "show more" row has no link of its own
        copy_link(self.app, self.artists[index].url or profile_url(self.artists[index].name))

    def compose(self) -> ComposeResult:
        yield from header_band("similar artists")
        with VerticalScroll(id="content"):
            with Horizontal(id="title-row"):
                yield Static(screen_title(self, f"Similar to {self.artist}"), id="title")
                yield Static("Loading…", id="meta")
            yield Static("Similar artists", id="similar-label", classes="section")
            yield ListView(id="artists")
        yield Static("", id="why")

        yield from player_bar()

    async def on_mount(self) -> None:
        self.query_one("#content").can_focus = False
        await self.refresh_playback_bar()  # right from the first frame
        try:
            response = await similar_wide(self.client, self.artist, self.limit)
        except (LastfmError, httpx.HTTPError) as exc:
            await self._show_error(plain(f"error: {one_line(str(exc))}"))
            return

        # Rank the lot: the call is cached for 30 days, so revealing the rest
        # later never asks Last.fm again.
        self._all = rank(
            response.similarartists.artist, min_match=self.min_match, limit=SIMILAR_FETCHED
        )
        self._ranked = list(self._all)
        self.artists = self._ranked[: self._shown]
        if not self.artists:
            await self._show_error(plain(f"No similar artists found for {self.artist}"))
            return
        corrected = response.similarartists.attr
        self._base_meta = (
            f"→ {corrected.artist}"
            if corrected and corrected.artist and corrected.artist != self.artist
            else ""
        )
        self.query_one("#meta", Static).update(plain(self._meta_line()))
        await self._render_rows()
        if self._depth:  # `d` on the artist screen, so start digging straight away
            self.run_worker(self._apply_depth(), exclusive=True, group="deep-cuts")
        else:
            self.run_worker(self._explain(0), exclusive=True, group="explain")

    async def _show_error(self, meta: Text) -> None:
        """A failed or empty list: say so, hide the empty window, offer the way out."""
        self.query_one("#meta", Static).update(plain(f"{meta.plain}  ·  b to go back"))
        for widget_id in ("#similar-label", "#artists", "#why"):
            with suppress(NoMatches):
                self.query_one(widget_id).display = False

    def action_deep_cuts(self) -> None:
        """Cycle how obscure to go: off, then the seed's listeners over 10, over 30.

        The lookups get their own worker group: re-rendering the rows moves the
        highlight, and the explanation worker that follows would otherwise cancel
        the very work this started.
        """
        self._depth = (self._depth + 1) % (len(DEEP_CUT_RATIOS) + 1)
        self.run_worker(self._apply_depth(), exclusive=True, group="deep-cuts")

    async def _apply_depth(self) -> None:
        """Show the list at the current depth, looking counts up if that needs it."""
        meta = self.query_one("#meta", Static)
        if self._depth == 0:
            self._ranked = list(self._all)
            self._shown = self.limit
            self.artists = self._ranked[: self._shown]
            await self._render_rows()
            meta.update(plain(self._meta_line()))
            return

        ratio = DEEP_CUT_RATIOS[self._depth - 1]
        # The list stays as it was while the counts arrive, so nothing flickers.
        meta.update(plain(f"finding deep cuts — under the seed's listeners ÷ {ratio}…"))
        await self._enrich()
        if self._seed_listeners is None:
            self._depth = 0
            self._ranked = list(self._all)
            self._shown = self.limit
            self.artists = self._ranked[: self._shown]
            await self._render_rows()
            meta.update(plain(f"no listener data for {self.artist} — deep cuts off"))
            return

        found = deep_cuts(
            self._all, self._listeners, seed_listeners=self._seed_listeners, ratio=ratio
        )
        self._ranked = found.artists
        self._shown = self.limit
        self.artists = self._ranked[: self._shown]
        await self._render_rows()
        meta.update(plain(self._describe(found)))

    async def _enrich(self) -> None:
        """Listener counts for the rows on screen, once per screen.

        Only the rows the filter has to judge, at most `MAX_LISTENER_LOOKUPS` of
        them, and remembered here so re-filtering asks for nothing.
        """
        if self._seed_listeners is None:
            with suppress(LastfmError, httpx.HTTPError):
                self._seed_listeners = (await self.client.info(self.artist)).stats.listeners
        wanted = [
            artist
            for artist in self._all[:MAX_LISTENER_LOOKUPS]
            if artist.name.casefold() not in self._listeners
        ]
        if not wanted:
            return
        self._listeners.update(await listener_counts(self.client, wanted))

    def _describe(self, found: DeepCuts) -> str:
        """The header line: what is on screen, and how to get more of it."""
        threshold = f"{found.threshold:,}" if found.threshold is not None else "?"
        if not found.artists:
            return f"no deep cuts under {threshold} listeners — press d to widen"
        word = "deep cut" if len(found.artists) == 1 else "deep cuts"
        line = f"{len(found.artists)} {word} under {threshold} listeners"
        if found.unknown:
            line += f"  ·  {found.unknown} unknown"
        return line

    def _meta_line(self) -> str:
        """What the header says: how many are on screen, and how many there are."""
        shown, total = len(self.artists), len(self._ranked)
        count = f"{shown} of {total} artists" if shown < total else f"{total} artists"
        return f"{self._base_meta}  ·  {count}" if self._base_meta else count

    async def _render_rows(self, *, keep_more: bool = False) -> None:
        """Mount `self.artists`, the row that reveals more, and any deep-cut counts."""
        view = self.query_one("#artists", ListView)
        counts = self._listeners if self._depth else {}
        hidden = len(self._ranked) - len(self.artists)
        await view.clear()
        rows = [
            ListItem(RowLabel(artist_row(artist, counts.get(artist.name.casefold()))))
            for artist in self.artists
        ]
        if hidden > 0:
            rows.append(ListItem(RowLabel(more_row(min(SHOW_MORE_STEP, hidden)))))
        if rows:
            await view.mount(*rows)
            view.index = len(self.artists) if keep_more and hidden > 0 else 0
        else:
            view.index = None
        view.focus()

    async def _show_more(self) -> None:
        """Reveal the next batch of what was already fetched and ranked."""
        self._shown = min(self._shown + SHOW_MORE_STEP, len(self._ranked))
        self.artists = self._ranked[: self._shown]
        await self._render_rows(keep_more=True)
        self.query_one("#meta", Static).update(plain(self._meta_line()))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "artists" and event.list_view.index is not None:
            self.run_worker(self._explain(event.list_view.index), exclusive=True, group="explain")

    async def _tags_of(self, artist: str) -> list[str]:
        """Tags for one artist, fetched once per screen; failures just mean no why."""
        if artist not in self._tag_cache:
            try:
                self._tag_cache[artist] = await self.client.top_tags(artist)
            except (LastfmError, httpx.HTTPError):
                self._tag_cache[artist] = []
        return self._tag_cache[artist]

    async def _explain(self, index: int) -> None:
        """Why the highlighted artist is similar: the tags it shares with the seed."""
        if not 0 <= index < len(self.artists):
            return
        seed_tags = await self._tags_of(self.artist)
        tags = await self._tags_of(self.artists[index].name)
        shared = shared_tags(seed_tags, tags)
        names = ", ".join(one_line(tag) for tag in shared)
        text = f"shared: {names}" if names else "no shared tags"
        with suppress(NoMatches):
            self.query_one("#why", Static).update(plain(text))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "artists" or not self.artists:
            return
        if event.index is not None and event.index >= len(self.artists):
            self.run_worker(self._show_more(), exclusive=True)  # the "show more" row
            return
        # imported here because artist.py imports this module for its own pushes
        from .artist import ArtistDetail

        self.app.push_screen(
            ArtistDetail(
                self.artists[event.index].name,
                self.client,
                self.player,
                self.options,
            )
        )


class TagScreen(WithPlayerBar):
    """Top artists for one tag; Enter opens their detail."""

    BINDINGS = [
        Binding("b", "app.pop_screen", "Back"),
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("c", "copy_link", "Copy link"),
    ]

    def __init__(
        self,
        tag: str,
        client: DetailClient,
        player: Player,
        options: TuiOptions,
    ) -> None:
        super().__init__()
        self.options = options
        self.tag = tag
        self.client = client
        self.player = player
        self.limit = options.limit
        self.min_match = options.min_match
        self.artists: list[TagArtist] = []

    def action_copy_link(self) -> None:
        """Copy the highlighted artist's Last.fm link (the tag API gives no url)."""
        index = self.query_one("#artists", ListView).index
        if index is not None and self.artists:
            copy_link(self.app, profile_url(self.artists[index].name))

    def compose(self) -> ComposeResult:
        yield from header_band("tag artists")
        with VerticalScroll(id="content"):
            with Horizontal(id="title-row"):
                yield Static(screen_title(self, f"Tag: {self.tag}"), id="title")
                yield Static("Loading…", id="meta")
            yield Static("Top artists", id="tag-label", classes="section")
            yield ListView(id="artists")

        yield from player_bar()

    async def on_mount(self) -> None:
        self.query_one("#content").can_focus = False
        await self.refresh_playback_bar()  # right from the first frame
        try:
            artists = await self.client.tag_top_artists(self.tag, limit=self.limit)
        except (LastfmError, httpx.HTTPError) as exc:
            self._no_artists(f"error: {one_line(str(exc))}")
            return
        self.artists = artists[: self.limit]  # the API's order is its ranking for the tag
        if not self.artists:
            self._no_artists(f"No artists found for #{self.tag}")
            return
        self.query_one("#meta", Static).update(f"{len(self.artists)} artists")
        view = self.query_one("#artists", ListView)
        await view.mount(*[ListItem(RowLabel(tag_artist_row(a))) for a in self.artists])
        view.index = 0
        view.focus()

    def _no_artists(self, message: str) -> None:
        self.query_one("#meta", Static).update(plain(f"{message}  ·  b to go back"))
        for widget_id in ("#tag-label", "#artists"):
            with suppress(NoMatches):
                self.query_one(widget_id).display = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "artists" or not self.artists:
            return
        # imported here because artist.py imports this module for its own pushes
        from .artist import ArtistDetail

        self.app.push_screen(
            ArtistDetail(
                self.artists[event.index].name,
                self.client,
                self.player,
                self.options,
            )
        )


class SourceScreen(WithPlayerBar):
    """Pick which YouTube result plays for a track; the choice is remembered."""

    BINDINGS = [
        Binding("b", "app.pop_screen", "Back"),
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("c", "copy_link", "Copy link"),
    ]

    def __init__(
        self,
        artist: str,
        track: str,
        player: Player,
        on_played: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.artist = artist
        self.track = track
        self.player = player
        # Told once the picked source is actually playing. The page that opened the
        # picker is the only one that knows which row was asked about: without this
        # nothing marked it, because the queue's index is set by playing a row, and a
        # source is picked rather than played.
        self._on_played = on_played
        self.results: list[YtResult] = []
        self._streams: dict[str, str] = {}  # watch url -> direct audio url, once one is known

    def action_copy_link(self) -> None:
        """Copy the highlighted result's YouTube link."""
        index = self.query_one("#sources", ListView).index
        if index is not None and self.results:
            copy_link(self.app, self.results[index].url)

    def compose(self) -> ComposeResult:
        yield from header_band("sources")
        with VerticalScroll(id="content"):
            with Horizontal(id="title-row"):
                yield Static(
                    screen_title(self, f"Sources for {self.artist} — {self.track}"), id="title"
                )
                yield Static("Searching…", id="meta")
            yield Static("YouTube results", id="source-label", classes="section")
            yield ListView(id="sources")

        yield from player_bar()

    async def on_mount(self) -> None:
        from ..store.sources import Sources

        self.query_one("#content").can_focus = False
        await self.refresh_playback_bar()  # right from the first frame
        try:
            self.results = await self.player.find_sources(self.artist, self.track)
        except YtDlpError as exc:
            self._no_results(f"search failed: {one_line(str(exc))}")
            return
        if not self.results:
            self._no_results("nothing found on YouTube")
            return
        picked = Sources().chosen(self.artist, self.track)
        self.query_one("#meta", Static).update(f"{len(self.results)} results")
        view = self.query_one("#sources", ListView)
        await view.mount(
            *[
                ListItem(
                    RowLabel(source_row(result, chosen=bool(picked and picked.url == result.url)))
                )
                for result in self.results
            ]
        )
        view.index = 0
        view.focus(scroll_visible=False)

    def _no_results(self, message: str) -> None:
        self.query_one("#meta", Static).update(plain(f"{message}  ·  b to go back"))
        for widget_id in ("#source-label", "#sources"):
            with suppress(NoMatches):
                self.query_one(widget_id).display = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "sources" or not self.results:
            return
        self.run_worker(self._choose(self.results[event.index]), exclusive=True)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Resolve the row under the cursor, so Enter plays it without the wait.

        One at a time (the worker cancels the last), so skimming the list costs no
        more than the rows the cursor actually rested on.
        """
        index = event.list_view.index
        if event.list_view.id != "sources" or index is None:
            return
        if not 0 <= index < len(self.results):
            return
        url = self.results[index].url
        if url in self._streams:
            return
        self.run_worker(self._prefetch(url), group="prefetch", exclusive=True)

    async def _prefetch(self, url: str) -> None:
        stream = await self.player.prefetch(url)
        if stream:
            self._streams[url] = stream

    async def _choose(self, result: YtResult) -> None:
        # A source picked by title is not scrobbled — a YouTube title is not
        # metadata, and the spec is explicit about not passing one off as if it
        # were. Stopping the count here is what keeps the *previous* track's play
        # from being credited off this one's position.
        self.lfm.forget_play()
        stream = self._streams.get(result.url)
        played = await self.player.play_url(result.title, stream or result.url)
        if not played.ok and stream:
            # the resolved URL can be stale or refused: play the page instead
            played = await self.player.play_url(result.title, result.url)
        if not played.ok:
            self.notify(played.message, severity="error", timeout=8, markup=False)
            return
        from ..store.sources import Sources

        Sources().remember(self.artist, self.track, title=result.title, url=result.url)
        if self._on_played is not None:
            self._on_played()
        self.app.pop_screen()
