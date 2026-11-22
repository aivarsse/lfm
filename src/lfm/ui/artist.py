"""The artist page: stats, tags, albums, top tracks, radio, and playback."""

import asyncio
from contextlib import suppress

import httpx
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import DescendantBlur, DescendantFocus
from textual.widgets import (
    Label,
    ListItem,
    ListView,
    Static,
    Tab,
    Tabs,
)

from ..api.client import LastfmError
from ..api.flows import artist_detail
from ..api.models import (
    Album,
    AlbumInfo,
    SimilarTrack,
    Track,
)
from ..api.protocols import (
    DetailClient,
)
from ..api.rank import suggestions
from ..playback.player import (
    PlayerStatus,
    PlayResult,
)
from ..playback.visualizer import (
    MAX_BARS,
    Spectrum,
    spectrum_text,
)
from .options import TuiOptions
from .protocols import (
    Player,
)
from .render import (
    MAX_TAGS,
    bio_text,
    compact_count,
    one_line,
    profile_url,
    truncated,
)
from .screens import (
    EMPTY_HINTS,
    RADIO_BATCH,
    SimilarScreen,
    SourceScreen,
    TagScreen,
)
from .widgets import (
    SUGGEST_LIMIT,
    IsolatedList,
    RowLabel,
    WithPlayerBar,
    album_meta_line,
    album_row,
    album_track_row,
    copy_link,
    header_band,
    make_spectrum_factory,
    plain,
    player_bar,
    radio_row,
    remember_artist,
    screen_title,
    set_terminal_title,
    track_row,
)

TRACKS_IN_LIST = 20
TRACKS_LABEL = "Most played"
# One glyph for both states, and the colour is what distinguishes them: green while
# the track is going, muted while it is paused. `●` (U+25CF) is the fullest of the
# round dots, so it holds its shape at one cell beside text. It is East-Asian
# Ambiguous, which the fixed marker cell absorbs -- the cell is reserved either way,
# so a terminal drawing it two cells wide cannot shift the row. What the marker may
# not be is a glyph with an emoji presentation, like `⏸` or `▶`: a terminal renders
# those in their own colours and the theme's colour never appears.
MARK = "●"  # U+25CF


class ArtistDetail(WithPlayerBar):
    """One artist: stats, tags, playable top tracks, bio, and a player bar."""

    BINDINGS = [
        Binding("b", "app.pop_screen", "Back"),
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("l", "similar", "Similar"),
        Binding("d", "deep_cuts", "Deep cuts", show=False),
        Binding("t", "focus_tags", "Tags"),
        Binding("r", "toggle_repeat", "Repeat"),
        Binding("y", "pick_source", "Source"),
        Binding("n", "next_track", "Next"),
        Binding("p", "previous_track", "Prev"),
        Binding("w", "radio", "Radio"),
        Binding("tab", "cycle_tab", "Tabs", priority=True),
        Binding("v", "toggle_visualizer", "Visualiser"),
        Binding("shift+tab", "cycle_tab_back", "Tabs", show=False, priority=True),
        Binding("c", "copy_link", "Copy link"),
    ]

    def __init__(
        self,
        name: str,
        client: DetailClient,
        player: Player,
        options: TuiOptions,
    ) -> None:
        super().__init__()
        self.options = options  # passed on to the screens this one pushes
        self.artist = name
        self.client = client
        self.player = player
        self.limit = options.limit
        self.min_match = options.min_match
        self.tracks: list[Track] = []
        self.tags: list[str] = []
        self._spectrum: Spectrum | None = None
        # The row the strip draws into, kept so the 30 Hz tick does not look it up.
        self._spectrum_widget: Static | None = None
        self._spectrum_unavailable = False
        self._visualizer_off = False  # `v` decided against it, so later tracks keep quiet
        self._queue_index: int | None = None
        self._seed_listeners: int | None = None
        self._listeners: int | None = None
        self._plays: int | None = None
        self._url: str | None = None
        self._status_labels: list[Label] = []
        self._switching = False
        # Bumped per play: only the newest clears `_switching`, so an older
        # one cannot lift the guard while a newer play resolves.
        self._switch_token = 0
        # Set by `cancel_play` when `s` lands while this play is still resolving:
        # the play arrives later, so it has to be stopped after the fact.
        self._stop_pending = False
        self._repeat = False
        # Set once `w` has built a radio: the queue is other artists' tracks then.
        self.radio: list[SimilarTrack] | None = None
        self._seen: set[tuple[str, str]] = set()
        self._pool: list[str] = []  # neighbour artists not yet drawn from
        self._pool_seed: str | None = None
        self._tab = "tracks"  # the tab the list is showing
        self._queue_id = "tracks"  # and the one the playing queue was queued from
        self.albums: list[Album] = []
        self._albums_loaded = False
        self._album_counts: dict[str, int] = {}
        self.album_info: AlbumInfo | None = None

    def _update_stats(self) -> None:
        """The counts in body ink, their words muted.

        The bright colour is the theme's foreground, baked into the Text, so this is
        also the theme's callback: redraw the line or it keeps the old theme's ink. The
        spaces belong to the bright run -- Textual drops a styled run's leading space.
        """
        if self._listeners is None or self._plays is None:
            return
        bright = self.lfm.theme_colour("foreground", "#e8e3da")
        meta = Text()
        meta.append(f"{compact_count(self._listeners)} ", style=bright)
        meta.append("listeners · ")
        meta.append(f"{compact_count(self._plays)} ", style=bright)
        meta.append("plays")
        self.query_one("#meta", Static).update(meta)

    def _light_title(self) -> None:
        self.query_one("#title", Static).add_class("lit")

    def action_copy_link(self) -> None:
        """Copy this artist's Last.fm link."""
        copy_link(self.app, self._url or profile_url(self.artist))

    def compose(self) -> ComposeResult:
        yield from header_band("artist")
        yield Static("", id="spectrum")
        with VerticalScroll(id="content"):
            with Horizontal(id="title-row"):
                yield Static(screen_title(self, self.artist), id="title")
                yield Static("Loading…", id="meta")
            with Horizontal(id="columns"):
                with Vertical(id="tracks-column"):
                    yield Tabs(
                        Tab(TRACKS_LABEL, id="tracks"),
                        Tab("Top albums", id="albums"),
                        id="tracks-tabs",
                    )
                    yield Static("", id="album-meta")
                    yield IsolatedList(id="tracks")
                    yield Static("", id="empty-hint")
                with Vertical(id="tags-column"):
                    yield Static("Tags", id="tags-label", classes="section")
                    yield IsolatedList(id="tags")
            yield Static("About", id="about-label", classes="section")
            yield Static("", id="bio")
        yield from player_bar()

    async def on_mount(self) -> None:
        self.query_one("#content").can_focus = False
        # The counts bake the theme's foreground into a Text, so a theme switch has to
        # rebuild that line or it keeps the ink of the theme you left.
        self.watch(self.app, "theme", self._update_stats, init=False)
        # The name's accent band fades in once the first frame is on screen: a page
        # should settle into place rather than appear already lit.
        self.call_after_refresh(self._light_title)
        # 30 fps: cava emits at 60, so sampling slower than this adds visible lag.
        self.set_interval(1 / 30, self._tick_spectrum)
        try:
            info, tags, tracks = await artist_detail(self.client, self.artist)
        except (LastfmError, httpx.HTTPError) as exc:
            self.query_one("#meta", Static).update(
                plain(f"error: {one_line(str(exc))}\nb for the prompt")
            )
            await self.suggestions()
            for widget_id in ("#columns", "#about-label", "#bio"):
                with suppress(NoMatches):
                    self.query_one(widget_id).display = False
            return
        self.tracks = tracks[:TRACKS_IN_LIST]
        if info.name:
            corrected = info.name != self.artist
            self.artist = info.name
            self.query_one("#title", Static).update(
                screen_title(self, f"{info.name} (corrected)" if corrected else info.name)
            )
        self._seed_listeners = info.stats.listeners
        self._url = info.url
        self._listeners = info.stats.listeners
        self._plays = info.stats.playcount
        self._update_stats()
        remember_artist(self.artist)
        self.tags = tags[:MAX_TAGS]
        bio = bio_text(info.bio.summary)
        self.query_one("#bio", Static).update(plain(bio))
        # nothing to say, no heading: an empty section reads as a bug
        self.query_one("#about-label", Static).display = bool(bio)
        tag_view = self.query_one("#tags", ListView)
        await tag_view.mount(*[ListItem(RowLabel(Text(one_line(tag)))) for tag in self.tags])
        if self.tags:
            tag_view.index = 0
        else:
            self.query_one("#tags-label", Static).update("")
            self.query_one("#tags-label", Static).display = False
            tag_view.display = False
        tracks_view = self.query_one("#tracks", ListView)
        await self._render_queue_rows()  # rows, the empty hint and the meta line
        await self._restore_playing()
        if self.tracks:
            tracks_view.index = 0
            # The list is already in view; letting focus scroll it would push the
            # artist name off the top of a short terminal.
            tracks_view.focus(scroll_visible=False)
        self._mark_window_focus()
        # one clock for every bar: the app's
        await self.refresh_bar()
        # A page opened while a track is already going: the play that started the
        # audio happened on another page, so nothing here has brought the strip up.
        await self._start_spectrum_if_playing()

    async def _start_spectrum_if_playing(self) -> None:
        if self._visualizer_off:
            return
        if (await self.player.status()).playing:
            await self._start_spectrum()

    async def suggestions(self) -> None:
        """Best effort: turn a failed lookup into a "did you mean" hint."""
        with suppress(Exception):
            matches = await self.client.search_matches(self.artist, limit=SUGGEST_LIMIT)
            names = suggestions(self.artist, matches)
            if names:
                meta = self.query_one("#meta", Static)
                suggestions_line = ", ".join(one_line(name) for name in names)
                meta.update(plain(f"{meta.content}\nDid you mean: {suggestions_line}"))

    def _claim_queue(self) -> None:
        """Point the marker back at the playing row, if this page is showing its list.

        The queue is a list of rows on one tab -- the artist's top tracks, or one album's
        tracks -- and the marker only paints on the tab it came from. A page, and each tab's
        list, is built fresh, so both the tab and the index have to be claimed back. An album
        index is only meaningful against that album's own list, which is why the album is
        carried with it: the same row number of a different album is a different track.
        """
        artist, index = self.lfm.playing_artist, self.lfm.playing_index
        if artist is None or index is None:
            return
        if artist.casefold() != self.artist.casefold():
            return
        album = self.lfm.playing_album
        if album:
            if self.album_info is None or self.album_info.name.casefold() != album.casefold():
                return
            rows, where = self.album_info.tracks, "album"
        else:
            rows, where = self.tracks, "tracks"
        if not 0 <= index < len(rows):
            return
        self._queue_id, self._queue_index = where, index

    def _queue_is_on_screen(self) -> bool:
        """Whether the list this tab shows is the list the playing row belongs to.

        A top-tracks queue belongs to this page, wherever the user is looking. An album
        queue belongs only to that album's tab: the index is a row number, so without this
        the same row of another album would wear the marker.
        """
        if self._tab != self._queue_id:
            return False
        album = self.lfm.playing_album
        if not album:
            return True
        return bool(self.album_info) and self.album_info.name.casefold() == album.casefold()

    def _is_the_playing_album(self, index: int) -> bool:
        """Whether the albums-tab row at `index` is the album the player is in.

        The list is where an album gets chosen, so this is the screen that should answer
        "which one is playing". The row that belongs to the playing album wears the same
        marker as the playing row itself -- and the artist has to match too, or an album of
        the same name by someone else would light up.
        """
        album, artist = self.lfm.playing_album, self.lfm.playing_artist
        if not album or artist is None:
            return False
        if artist.casefold() != self.artist.casefold():
            return False
        if not 0 <= index < len(self.albums):
            return False
        return self.albums[index].name.casefold() == album.casefold()

    async def _restore_playing(self) -> None:
        """Take up the queue where this artist left off, if it is still playing.

        A page is built fresh every time you open it, so without this the marker
        and the bar's count belonged to whichever visit started the track.
        """
        self._claim_queue()
        index = self.lfm.playing_index
        if index is not None and self._tab == self._queue_id:  # the row shows in its own tab
            self.query_one("#tracks", ListView).index = index
        await self._draw_markers()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Keep the highlighted row on screen.

        The columns take the page's remaining height and the lists scroll inside
        them, as the similar list does, so on a normal terminal this does nothing
        and the page - and with it the artist's name - stays put. On a very short
        terminal the columns cannot fit, and only the page can bring them into
        view; `min-height` keeps them readable in exchange.
        """
        if event.item is not None:
            self.query_one("#content", VerticalScroll).scroll_to_widget(event.item, animate=False)
        # Resolve what the cursor is on, so Enter plays it without the wait. The
        # player cancels the last one, so skimming costs one extraction, and it
        # ignores the albums tab and the radio's "show more" row for us.
        if event.list_view.id == "tracks":
            index = event.list_view.index
            seed = self._visible_playable(index) if index is not None else None
            if seed is not None:
                self.player.prefetch_track(*seed)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "tags" and self.tags:
            self.app.push_screen(
                TagScreen(self.tags[event.index], self.client, self.player, self.options)
            )
            return
        if event.list_view.id != "tracks":
            return
        if self._tab == "albums":  # albums open; every other tab plays
            albums = self.albums
            if event.index is not None and 0 <= event.index < len(albums):
                self.run_worker(
                    self._load_album(albums[event.index].name), group="album", exclusive=True
                )
            return
        if not self._queue():
            return
        # A press on a row is the user's newest wish: it overtakes a track
        # that is still resolving instead of being ignored until it ends.
        # `follow=False` leaves their cursor where they put it.
        self.run_worker(self._play_track(event.index, follow=False), exclusive=True)

    def _queue(self, tab: str | None = None) -> list:
        """The rows a tab shows: tracks, albums, an opened album, or the radio."""
        tab = self._tab if tab is None else tab
        if tab == "albums":
            return self.albums
        if tab == "album":
            return self.album_info.tracks if self.album_info else []
        if tab == "radio":
            return self.radio or []
        return self.tracks

    def _playback_queue(self) -> list:
        """The queue playback walks: the tab the loaded track was queued from."""
        return self._queue(self._queue_id)

    def _playable(self, index: int) -> tuple[str, str]:
        """(artist, track) for a playback-queue row; a radio row names its own."""
        entry = self._playback_queue()[index]
        if self._queue_id == "radio":
            return entry.artist, entry.name
        return self.artist, entry.name

    def _visible_playable(self, index: int) -> tuple[str, str] | None:
        """(artist, track) for the highlighted row of the active tab, if playable."""
        if self._tab == "albums" or not 0 <= index < len(self._queue()):
            return None
        entry = self._queue()[index]
        if self._tab == "radio":
            return entry.artist, entry.name
        return self.artist, entry.name

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self._mark_window_focus()

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        self._mark_window_focus()

    def _mark_window_focus(self) -> None:
        """Only the window the keyboard is in is lit: its tab bar, or its heading."""
        tabs = self.query_one("#tracks-tabs", Tabs)
        tabs.set_class(self.query_one("#tracks-column", Vertical).has_focus_within, "in-focus")
        tags = self.query_one("#tags-label", Static)
        tags.set_class(self.query_one("#tags-column", Vertical).has_focus_within, "in-focus")

    def _light_window(self, window: str) -> None:
        """Light the window the keyboard is going to, before the focus message lands.

        `focus()` is a message, so `has_focus_within` still answers for the old window
        when the tab bar is redrawn: a switch out of the tags window waited a message
        for its accent. The action knows where the keyboard is going; say so now.
        """
        tabs = self.query_one("#tracks-tabs", Tabs)
        tabs.set_class(window == "tracks", "in-focus")
        self.query_one("#tags-label", Static).set_class(window == "tags", "in-focus")

    def _cycle_targets(self) -> list[str]:
        """The page in Tab order: the track window's tabs, then the tags window."""
        tabs = self.query_one("#tracks-tabs", Tabs)
        targets = [tab.id or "" for tab in tabs.query(Tab)]
        if self.query_one("#tags", ListView).display:
            targets.append("tags")
        return targets

    def _cycle(self, delta: int) -> None:
        """Walk the page: the track window's tabs, the tags window, round again."""
        targets = self._cycle_targets()
        tags = self.query_one("#tags", ListView)
        here = "tags" if tags.has_focus else self._tab
        try:
            at = targets.index(here)
        except ValueError:
            at = 0
        where = targets[(at + delta) % len(targets)]
        if where == "tags":
            self._light_window("tags")
            tags.focus()
            return
        tabs = self.query_one("#tracks-tabs", Tabs)
        tabs.active = where
        # In step at once: a key press can beat the TabActivated message. The
        # draw and the load go through workers so the handler returns started.
        self._tab = where
        if where == "albums" and not self._albums_loaded:
            # The album worker renders as soon as the list arrives, then again with the
            # counts. A render here would race it and could wipe the rows it mounted.
            self._ensure_albums()
        else:
            self.run_worker(self._render_queue_rows(), group="render", exclusive=True)
        self._light_window("tracks")
        self.query_one("#tracks", ListView).focus()

    def action_cycle_tab(self) -> None:
        """Tab: through the track window's tabs, then the tags window, and round.

        Switching the window's tabs used to be all Tab did, which left the tags
        list unreachable from the keyboard; the cycle ends with it instead.
        """
        self._cycle(1)

    async def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab = event.tab.id or ""
        if not tab or tab == self._tab:  # a switch we made ourselves: already drawn
            return
        self._tab = tab
        self._ensure_albums()
        await self._render_queue_rows()

    def _ensure_albums(self) -> None:
        """The albums tab asks Last.fm once; clicking it or tabbing to it both count."""
        if self._albums_loaded:
            return
        self.run_worker(self._load_albums(), group="album", exclusive=True)

    async def _add_tab(self, tab_id: str, label: str) -> None:
        """Give the window a tab and show it; a label change reuses the old tab."""
        tabs = self.query_one("#tracks-tabs", Tabs)
        if tab_id not in {tab.id for tab in tabs.query(Tab)}:
            # a Text, not the raw string: Tab parses markup, so an album named
            # "Untrue [Live]" lost the bracketed part and came out styled
            await tabs.add_tab(Tab(plain(label), id=tab_id))
        self._tab = tab_id
        tabs.active = tab_id
        await self._render_queue_rows()

    async def _drop_tab(self, tab_id: str, fallback: str = "tracks") -> None:
        """Take a tab away, falling back to the artist's own tracks.

        Leaving first matters: removing the *active* tab makes Tabs activate a
        neighbour on its own, which would race this method's own bookkeeping.
        """
        tabs = self.query_one("#tracks-tabs", Tabs)
        if self._tab == tab_id:
            self._tab = fallback
            tabs.active = fallback  # an inactive removal activates nothing
            await self._render_queue_rows()
        await tabs.remove_tab(tab_id)

    async def _load_albums(self) -> None:
        """The albums tab's rows, fetched the first time the tab is opened."""
        if self._albums_loaded:
            return
        self._albums_loaded = True  # a failure is not worth refetching on every Tab
        try:
            self.albums = (await self.client.top_albums(self.artist))[:TRACKS_IN_LIST]
        except (LastfmError, httpx.HTTPError) as exc:
            self.notify(f"albums: {exc}", severity="error", timeout=8, markup=False)
            return
        if self._tab == "albums":
            await self._render_queue_rows()  # rows now, counts when they arrive
        await self._count_album_tracks()

    async def _count_album_tracks(self) -> None:
        """How many songs each album has — the list call cannot tell us.

        One album.getInfo per row shown (the deep-cuts pattern: background,
        capped, 30-day cached), and the counts land on the rows as they arrive.
        """

        async def one(album: Album) -> None:
            try:
                info = await self.client.album_info(self.artist, album.name)
            except (LastfmError, httpx.HTTPError):
                return  # no count beats a wrong one; another visit retries it
            self._album_counts[album.name] = len(info.tracks)

        await asyncio.gather(*(one(album) for album in self.albums[:TRACKS_IN_LIST]))
        if self._tab == "albums":  # once, together: concurrent renders interleave badly
            await self._render_queue_rows()

    async def _load_album(self, name: str) -> None:
        """Fetch an album and give it its tab; its tracklist is the queue."""
        try:
            info = await self.client.album_info(self.artist, name)
        except (LastfmError, httpx.HTTPError) as exc:
            self.notify(f"album: {exc}", severity="error", timeout=8, markup=False)
            return
        if not info.tracks:
            self.notify("no tracklist for this album", severity="warning", timeout=4)
            return
        if self._queue_id == "album":  # a queue left on the old album has no home
            self._queue_index = None
            self._queue_id = "tracks"
        self.album_info = info
        await self._drop_tab("album")  # the replaced album's tab, if it had one
        await self._add_tab("album", truncated(info.name or name, 24))

    def action_radio(self) -> None:
        """`w`: a radio from the highlighted track — or, if one is on, off again."""
        if self.radio is not None:
            self.run_worker(self._stop_radio(), exclusive=True)
            return
        view = self.query_one("#tracks", ListView)
        index = view.index if view.index is not None else self._queue_index
        seed = self._visible_playable(index) if index is not None else None
        if seed is None:
            return
        self.run_worker(self._start_radio(*seed), exclusive=True)

    async def _start_radio(self, artist: str, track: str) -> None:
        """A fresh radio from this track; `_extend_radio` keeps it going."""
        self._seen = {(artist.casefold(), track.casefold())}
        if not await self._extend_radio(artist, track):
            self.notify("nothing similar to play", severity="warning", timeout=4)
            return
        await self._add_tab("radio", "Radio")
        await self._play_track(0)

    async def _stop_radio(self) -> None:
        """Off: the artist's own tracks come back; what is playing finishes in peace."""
        self.radio = None
        self._seen.clear()
        self._pool, self._pool_seed = [], None
        self._queue_index = None  # the playing song is not in the restored list
        self._queue_id = "tracks"
        await self._drop_tab("radio")
        await self.refresh_bar()

    async def _extend_radio(self, artist: str, track: str) -> bool:
        """Append what is similar to this track, minus anything already queued."""
        try:
            results = await self.client.similar_tracks(artist, track, limit=TRACKS_IN_LIST)
            if not results:
                # Last.fm's track-to-track data is sparse — empty for most songs —
                # so the radio drifts artist-wise instead of dying on the spot
                results = self._own_tracks(artist) or await self._neighbours(artist)
        except (LastfmError, httpx.HTTPError) as exc:
            self.notify(f"radio: {exc}", severity="error", timeout=8, markup=False)
            return False
        fresh = []
        for result in results:
            key = (result.artist.casefold(), result.name.casefold())
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(result)
        if not fresh:
            return False
        self.radio = [*(self.radio or []), *fresh]
        await self._render_queue_rows()
        return True

    def _own_tracks(self, artist: str) -> list[SimilarTrack]:
        """More from this artist: already on the page, so it costs no calls."""
        if artist.casefold() != self.artist.casefold():
            return []
        return [
            SimilarTrack(artist=self.artist, name=t.name)
            for t in self.tracks
            if (self.artist.casefold(), t.name.casefold()) not in self._seen
        ][:RADIO_BATCH]

    async def _neighbours(self, artist: str) -> list[SimilarTrack]:
        """The artist's closest neighbours, one batch at a time.

        The pool re-seeds from whoever is playing, so the radio keeps drifting
        rather than circling the seed artist's own corner.
        """
        if self._pool_seed != artist:
            found = await self.client.similar(artist, limit=TRACKS_IN_LIST)
            self._pool = [
                a.name
                for a in found.similarartists.artist
                if a.name.casefold() != artist.casefold()
            ]
            self._pool_seed = artist
        while self._pool:
            neighbour = self._pool.pop(0)
            tracks = await self.client.top_tracks(neighbour)
            fresh = [
                SimilarTrack(artist=neighbour, name=t.name)
                for t in tracks
                if (neighbour.casefold(), t.name.casefold()) not in self._seen
            ]
            if fresh:
                return fresh[:RADIO_BATCH]
        return []

    async def _render_queue_rows(self) -> None:
        """Re-draw the list for the active tab, keeping the highlight."""
        view = self.query_one("#tracks", ListView)
        previous = view.index or 0
        if self._tab == "albums":
            rows = [album_row(album, self._album_counts.get(album.name)) for album in self.albums]
        elif self._tab == "album":
            rows = (
                [album_track_row(track) for track in self.album_info.tracks]
                if self.album_info
                else []
            )
        elif self._tab == "radio":
            rows = [radio_row(entry) for entry in self.radio or []]
        else:
            rows = [track_row(track) for track in self.tracks]
        self._status_labels = [marker for _, marker in rows]
        # Each tab's list is built fresh: claim the queue back so the tab that owns the
        # playing row gets its marker, whether this render is the mount or a tab switch.
        self._claim_queue()
        # One paint for the whole swap, and the new rows go in before the old ones
        # come out: clearing first let a frame show the window empty, so a tab switch
        # looked like the list taking a moment to fill.
        stale = list(view.children)
        with self.app.batch_update():
            await view.mount(*[item for item, _ in rows])
            for item in stale:
                await item.remove()
        view.index = min(previous, len(rows) - 1) if rows else None
        # ten blank rows read as a bug: hide the window and say why it is empty
        view.display = bool(rows)
        hint = self.query_one("#empty-hint", Static)
        hint.display = not rows
        # Painted once the rows are mounted: asking the player for the paused
        # state waits on mpv, which delayed the list by that much.
        await self._draw_markers()
        if not rows:
            loading = self._tab == "albums" and not self._albums_loaded
            hint.update(plain("Looking for albums…" if loading else EMPTY_HINTS[self._tab]))
        meta = self.query_one("#album-meta", Static)
        meta.display = self._tab == "album" and self.album_info is not None
        if self.album_info is not None:
            meta.update(album_meta_line(self.album_info))

    def action_similar(self) -> None:
        self.app.push_screen(
            SimilarScreen(
                self.artist,
                self.client,
                self.player,
                self.options,
                seed_listeners=self._seed_listeners,
            )
        )

    def action_deep_cuts(self) -> None:
        """The same list, digging: `l` then `d` in one key."""
        self.app.push_screen(
            SimilarScreen(
                self.artist,
                self.client,
                self.player,
                self.options,
                deep_cuts=True,
                seed_listeners=self._seed_listeners,
            )
        )

    def action_focus_tags(self) -> None:
        if self.tags:
            self.query_one("#tags", ListView).focus()

    def action_pick_source(self) -> None:
        """Choose which YouTube result plays for the highlighted track.

        The cursor, not the queue's index: a queue belongs to the tab it was queued
        from, so on another tab that index is a row number for a different list. It
        picked the source for the wrong track there, and then marked that one.
        """
        view = self.query_one("#tracks", ListView)
        index = view.index
        seed = self._visible_playable(index) if index is not None else None
        if seed is None or index is None:
            return
        self.app.push_screen(
            SourceScreen(
                *seed, self.player, on_played=lambda: self._note_source_played(index, seed)
            )
        )

    def _note_source_played(self, index: int, seed: tuple[str, str]) -> None:
        """The picker started this row: give it the marker and the queue.

        Only `_play_track` used to record what is playing, and a source is picked
        rather than played, so the row stayed bare and the queue never knew its index -
        which also left `n`/`p` and auto-advance with nowhere to go from.
        """
        self._queue_id = self._tab
        self._queue_index = index
        self.lfm.playing_artist = seed[0]  # the row's artist: a radio row names its own
        self.lfm.playing_index = index
        self.lfm.playing_album = self._queue_album()
        self._update_markers(paused=False)
        self.run_worker(self._start_spectrum())  # the chosen source is playing: bring the bars up

    async def tick_bar(self, status: PlayerStatus | None = None) -> None:
        await self.refresh_bar(status)  # the queue's owner: this also advances tracks

    async def refresh_bar(self, status: PlayerStatus | None = None) -> None:
        status = status if status is not None else await self.player.status()
        self.lfm.count_play(status)
        # the bar's right half is shared state: every screen's bar reads it
        note: list[str] = []
        if status.playing and self._queue_index is not None and self._playback_queue():
            position = f"{self._queue_index + 1}/{len(self._playback_queue())}"
            if self._queue_id == "album" and self.album_info is not None:
                position += f" · {self.album_info.name}"
            elif self._queue_id == "radio":
                position += " · radio"
            note.append(position)
        self.lfm.queue_note = "  ·  ".join(note)
        self.lfm.repeat_on = self._repeat
        await self.refresh_playback_bar(status)
        if not status.playing:
            await self._stop_spectrum()
            if status.finished:
                await self._advance()  # it played through, so the next one may follow
            elif self._queue_index is not None:
                # it died mid-track: stop here rather than skipping on, and say so
                self._queue_index = None
                self.notify(
                    "playback stopped  ·  y to pick a source",
                    severity="error",
                    timeout=8,
                    markup=False,
                )
        self._update_markers(bool(status.paused))

    def action_cycle_tab_back(self) -> None:
        """Shift+Tab: the same walk, backwards."""
        self._cycle(-1)

    def reveal_playing(self) -> None:
        """`f`: show the tab the queue lives in and bring its row into view."""
        if self._queue_index is None:
            return
        tabs = self.query_one("#tracks-tabs", Tabs)
        if self._queue_id in {tab.id for tab in tabs.query(Tab)}:
            tabs.active = self._queue_id  # its handler redraws that tab
        self.call_after_refresh(self._scroll_to_playing)

    def _scroll_to_playing(self) -> None:
        if self._queue_index is None:
            return
        view = self.query_one("#tracks", ListView)
        rows = view.children
        if 0 <= self._queue_index < len(rows):
            view.index = self._queue_index
            view.scroll_to_widget(rows[self._queue_index], animate=False)
        view.focus()

    async def _draw_markers(self) -> None:
        """Paint the playing marker now, rather than on the next bar tick."""
        status = await self.player.status()
        self._update_markers(bool(status.paused))

    def _update_markers(self, paused: bool) -> None:
        """`MARK` on the loaded track, blank everywhere else, classed by state.

        The same glyph either way: the colour is what says whether it is going, and
        the class is what carries that colour.
        """
        for index, marker in enumerate(self._status_labels):
            loaded = (
                self._is_the_playing_album(index)
                if self._tab == "albums"
                else index == self._queue_index and self._queue_is_on_screen()
            )
            marker.update(MARK if loaded else "")
            classes = ["status"]
            if loaded:
                classes.append("status-paused" if paused else "status-playing")
            marker.set_classes(classes)

    async def _advance(self) -> None:
        """A track ended: repeat it, play the next one, or stop at the end."""
        if self._switching or self._queue_index is None:
            return
        index = self._queue_index
        self._queue_index = None
        if self._repeat:
            await self._play_track(index, from_tab=self._queue_id)
        elif index + 1 < len(self._playback_queue()):
            await self._play_track(index + 1, from_tab=self._queue_id)
        elif self._queue_id == "radio" and self.radio is not None:
            # the queue ran dry: ask the track that just played where to drift next
            artist, track = self._playable(index)
            if await self._extend_radio(artist, track):
                await self._play_track(index + 1, from_tab="radio")
            else:
                set_terminal_title(self.app, "lfm")
        else:
            set_terminal_title(self.app, "lfm")

    def _queue_album(self) -> str:
        """The album this queue came from, or nothing when it did not come from one.

        Only an album tab knows it; an album guessed from the artist would put
        the wrong one on the profile, so there is no guessing.
        """
        if self._queue_id == "album" and self.album_info is not None:
            return self.album_info.name
        return ""

    def cancel_play(self) -> None:
        """`s` while a track is resolving: let the play land, then stop it."""
        self._stop_pending = True
        self.lfm.resolving = ""

    async def _play_track(
        self, index: int, from_tab: str | None = None, follow: bool = True
    ) -> None:
        """Play one row of the current queue.

        `follow` brings the list cursor onto the track once it is playing. That is
        right for the TUI's own moves — next/previous, auto-advance, the radio —
        and wrong for a row the user asked for: the cursor is already there, and
        moving it would take back a selection they made while the track was still
        resolving.
        """
        # Auto-advance keeps the queue it was in; a key press queues from the
        # active tab. Either way the queue and the index must agree.
        self._queue_id = from_tab if from_tab is not None else self._tab
        artist, name = self._playable(index)
        # While a track resolves nothing is playing, and the old queue index
        # would make the 0.5s poller start another track: clear it, mark the
        # switch with a token, and only the newest play lifts the mark.
        self._switch_token += 1
        token = self._switch_token
        self._stop_pending = False  # a new play is a new intent
        self._switching = True
        self._queue_index = None
        self.lfm.resolving = f"{artist} — {name}"
        try:
            result = await self.player.play(artist, name)
        except Exception as exc:  # a broken player must not take the TUI down
            result = PlayResult(False, str(exc))
        finally:
            if self._switch_token == token:
                self._switching = False
            # Only clear the note if it is still ours: pressing Enter on another
            # track sets its own while this one is being resolved.
            if self.lfm.resolving == f"{artist} — {name}":
                self.lfm.resolving = ""
        if self._stop_pending and self._switch_token == token:
            # A stop landed while this play was in flight. The player already has the
            # track, so stopping it here is the only way the key can win; and the
            # track must not be marked as playing.
            self._stop_pending = False
            if result.ok:
                await self.player.stop()
            await self.refresh_bar()
            return
        if not result.ok and not result.superseded:  # success is visible in the list and the bar
            message = result.message
            if message.startswith("could not"):  # a bad match, not a stop
                message += "  ·  y to pick a source"
            self.notify(message, severity="error", timeout=8, markup=False)
        if result.ok:
            self._queue_index = index
            self.lfm.playing_artist = artist
            self.lfm.playing_index = index
            self.lfm.playing_album = self._queue_album()
            self.lfm.start_play(artist, name, self._queue_album())
            if follow and self._tab == self._queue_id:  # the row is only visible in its tab
                self.query_one("#tracks", ListView).index = index
            set_terminal_title(self.app, f"{artist} — {name}")
            await self._start_spectrum()
        await self.refresh_bar()

    def action_next_track(self) -> None:
        if not self._switching:
            self.run_worker(self._step(1), exclusive=True)

    async def action_toggle_repeat(self) -> None:
        self._repeat = not self._repeat
        self.lfm.repeat_on = self._repeat
        # The flash belongs to the app: it is about the player's state, not this page's.
        if self._repeat:
            self.lfm.fade_repeat_in()
        else:
            self.lfm.clear_repeat_flash()
        await self.refresh_bar()

    def action_previous_track(self) -> None:
        if not self._switching:
            self.run_worker(self._step(-1), exclusive=True)

    async def _step(self, delta: int) -> None:
        """Walk the playing queue, or the active tab's when nothing is loaded."""
        if self._tab == "albums":  # albums open rather than play
            return
        queue = self._playback_queue() if self._queue_index is not None else self._queue()
        origin = self._queue_index if self._queue_index is not None else -delta
        index = origin + delta
        if 0 <= index < len(queue):
            await self._play_track(
                index, from_tab=self._queue_id if self._queue_index is not None else None
            )

    async def _start_spectrum(self) -> None:
        if self._visualizer_off:
            return
        factory = make_spectrum_factory(self.app)
        if factory is None or self._spectrum is not None or self._spectrum_unavailable:
            return
        # Fill the row: one bar per column, minus the padding either side.
        bars = max(8, min(MAX_BARS, (self.size.width or 80) - 4))
        spectrum = factory(bars)
        if await spectrum.start():
            self._spectrum = spectrum
            self._spectrum_widget = self.query_one("#spectrum", Static)
        else:
            # No cava: say so once instead of retrying on every track.
            self._spectrum_unavailable = True
            self.notify("visualiser off — cava did not start", severity="warning", timeout=4)

    async def action_toggle_visualizer(self) -> None:
        """`v`: the EQ strip on or off — and off means off for later tracks too."""
        if self._spectrum is not None:
            self._visualizer_off = True
            await self._stop_spectrum()
            return
        self._visualizer_off = False
        if self._spectrum_unavailable:
            self.notify("visualiser off — cava did not start", severity="warning", timeout=4)
        elif (await self.player.status()).playing:
            await self._start_spectrum()

    async def _stop_spectrum(self) -> None:
        if self._spectrum is None:
            return
        await self._spectrum.stop()
        self._spectrum = None
        self._spectrum_widget = None
        # On unmount the widget is already gone, so clearing it is best effort.
        with suppress(NoMatches):
            self.query_one("#spectrum", Static).update("")

    async def _tick_spectrum(self) -> None:
        widget = self._spectrum_widget
        if self._spectrum is None or widget is None:
            return
        levels = await self._spectrum.levels()
        widget.update(spectrum_text(levels, self.lfm.spectrum_palette()))

    async def on_unmount(self) -> None:
        set_terminal_title(self.app, "lfm")
        await self._stop_spectrum()
        refresh = getattr(self.app, "refresh_recent", None)
        if callable(refresh):
            refresh()
