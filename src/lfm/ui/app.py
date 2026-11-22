"""The application: screens, bindings, theme, and the key sheet that reads them."""

import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Resize
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import MountError
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from ..api.protocols import (
    DetailClient,
)
from ..playback.player import (
    SEEK_SECONDS,
    VOLUME_STEP,
    PlayerStatus,
)
from ..playback.scrobble import Play, Session
from ..playback.visualizer import SPECTRUM_COLOURS
from .artist import (
    ArtistDetail,
)
from .options import TuiOptions
from .protocols import (
    Player,
)
from .render import (
    blend,
    ease_in_out_cubic,
)
from .shortcuts import (
    shortcut_colours,
    shortcut_column,
    shortcut_columns,
)
from .theme import (
    CSS,
    LFM_CREAM,
    LFM_MIDNIGHT,
    LFM_OBSIDIAN,
    THEMES,
)
from .widgets import (
    HISTORY_HINT,
    WORDMARK_MIN_WIDTH,
    WORDMARK_ROWS,
    PromptInput,
    PromptSuggester,
    RowLabel,
    WithPlayerBar,
    header_band,
    plain,
    remember_volume,
    set_terminal_title,
)

LONG_SEEK_SECONDS = 30  # shift + arrow: the half-minute jump
REPEAT_FADE = 0.15  # the tab bar's `transition: color 150ms`, so the mark moves alike
REPEAT_FRAME = 1 / 60  # how often the bar redraws while the fade runs


class ShortcutsScreen(ModalScreen[None]):
    """Every key lfm answers to, in a popup. `h`, `F1` or Escape closes it.

    A ModalScreen, so the page behind is dimmed rather than hidden: the sheet is a
    thing you look at and dismiss, not a page you are on.
    """

    BINDINGS = [
        Binding("f1", "app.shortcuts", "Close", show=False),
        Binding("escape", "app.shortcuts", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="shortcuts-panel"):
            app = self.app
            assert isinstance(app, LfmApp)  # `Screen.app` is typed as the base App
            with Horizontal(id="shortcut-columns"):
                for column in shortcut_columns(app.size.width):
                    yield Static(
                        shortcut_column(column, **shortcut_colours(app)),
                        classes="shortcut-column",
                    )


class LfmApp(App[None]):
    """Home screen prompts for an artist; screens stack from there."""

    TITLE = "lfm"
    BINDINGS = [
        Binding("q", "stop_and_quit", "Quit"),
        # F1, not `h`: on the landing page the prompt holds focus, so an `h` is a letter
        # and the key could not open the sheet there. One key for it is enough.
        # "Exit shortcuts": read this sheet while it is open, and the row tells you how
        # to leave. It opens the sheet too — the action toggles — but that is the app's
        # hint on the landing page's job.
        Binding("f1", "shortcuts", "Exit shortcuts"),
        # playback keys live here so every screen has them; the home Input
        # swallows the typeable ones while it is focused, so nothing collides
        Binding("space", "toggle_pause", "Play/pause"),
        Binding("left", "seek_back", "-15s", show=False),
        Binding("right", "seek_forward", "+15s", show=False),
        Binding("shift+left", "seek_back_long", "-30s", show=False),
        Binding("shift+right", "seek_forward_long", "+30s", show=False),
        Binding("shift+up", "volume_up", "Vol+", show=False),
        Binding("shift+down", "volume_down", "Vol-", show=False),
        Binding("left_square_bracket", "volume_down", "Vol-", show=False),
        Binding("right_square_bracket", "volume_up", "Vol+", show=False),
        Binding("plus", "volume_up", "Vol+", show=False),
        Binding("minus", "volume_down", "Vol-", show=False),
        Binding("m", "mute", "Mute"),
        Binding("T", "toggle_theme", "Theme"),
        Binding("f", "jump_playing", "Jump to playing"),
        Binding("s", "stop", "Stop", show=False),
        *[Binding(str(n), f"seek_percent({n})", show=False) for n in range(10)],
        # shown in the home hint rather than the footer, since it only applies there
        Binding("delete", "forget_recent", "Forget", show=False),
    ]
    ENABLE_COMMAND_PALETTE = False
    CSS = CSS

    def __init__(
        self,
        client: DetailClient,
        player: Player,
        options: TuiOptions | None = None,
    ) -> None:
        super().__init__()
        options = options or TuiOptions()
        for one in (LFM_OBSIDIAN, LFM_CREAM):
            self.register_theme(one)
        if LFM_MIDNIGHT is not None:
            self.register_theme(LFM_MIDNIGHT)  # shadows Textual's own dark theme
        self.theme = THEMES.get(options.theme, LFM_OBSIDIAN.name)  # unknown means dark
        self.client = client
        self.suggester = PromptSuggester(client)  # the prompt completes through this
        self.player = player
        # The screens are built with this rather than with its fields, so the app
        # is the only place that unpacks it.
        self.options = options
        self.artist = options.artist
        self.limit = options.limit
        self.min_match = options.min_match
        self.spectrum = options.spectrum
        self.save_volume = options.save_volume
        # The write path, or nothing at all. The CLI builds this only when
        # scrobbling is switched on *and* both credentials are present, so a
        # play is never queued on a machine that could not send it.
        self.scrobbling = options.scrobbling
        self._session: Session | None = None  # the track whose play is being counted
        self.queue_note = ""  # "2/11 · Untrue": which queue is playing
        self.resolving = ""  # what the player is fetching, while it fetches it
        # Which row of which artist, and of which of their lists, is playing, so coming
        # back to the same artist does not look like a page with nothing playing.
        self.playing_artist: str | None = None
        self.playing_index: int | None = None
        self.playing_album: str = ""  # "" for a top-tracks queue
        self.repeat_on = False
        self.muted_volume: int | None = None
        # The repeat mark fades in when it is switched on: a start time the bar reads,
        # one redraw interval, and a token so an earlier press's timer cannot end a
        # later fade.
        self._repeat_fade_start = 0.0
        self._repeat_fade_token = 0
        self._repeat_fade_timer: Timer | None = None
        self._repeat_fade_interval: Timer | None = None

    # --- scrobbling ---------------------------------------------------------
    # On the app rather than a screen: the state is about the *player*, and
    # the player outlives any one screen. Every method below is a no-op
    # when `scrobbling` is None, which is the default.

    def start_play(self, artist: str, track: str, album: str = "") -> None:
        """Begin counting one track's play, and say what is playing."""
        if self.scrobbling is None:
            self._session = None
            return
        self._session = Session(artist=artist, track=track, album=album)
        self.run_worker(self._say_now_playing(self._session.opening()), group="nowplaying")

    def forget_play(self) -> None:
        """Stop counting: what is playing no longer answers to the open session."""
        self._session = None

    def count_play(self, status: PlayerStatus) -> None:
        """Feed one status reading to the open session, and queue the play on the
        reading where it first counts as one.

        The bar repaints a few times a second, so the spec's rule is checked
        here — which is why a track that counted has already been queued by the
        time it is skipped, stopped, or the app is quit. Nothing has to run on
        the way out to catch it.
        """
        if self._session is None or self.scrobbling is None:
            return
        play = self._session.observe(status.position, status.duration)
        if play is None:
            return
        self.scrobbling.queue(play)
        self.run_worker(self._send_scrobbles(), group="scrobble")

    async def _say_now_playing(self, play: Play) -> None:
        """Send one now-playing line; it is decoration, and fails quietly."""
        if self.scrobbling is not None:
            await self.scrobbling.now_playing(play)

    async def _send_scrobbles(self) -> None:
        """Drain the outbox in the background, and report only a failure.

        A scrobble that worked needs no announcement: it is on the profile, there
        is nothing to do about it, and a toast per track is chrome for something
        the user already knows they played. A *failure* is worth one warning,
        because a setup that has quietly stopped working leaves plays piling up
        in the outbox, and silence would hide that.
        """
        assert self.scrobbling is not None  # count_play checked this
        drained = await self.scrobbling.drain()
        if not drained.ok:
            self.notify(
                f"scrobble: {drained.summary()}", severity="warning", timeout=8, markup=False
            )

    def compose(self) -> ComposeResult:
        # Filled in by _refresh_recent, so there is one code path for the first
        # render and every return to this screen.
        yield from header_band("home")
        # One column, centred: the wordmark, the prompt, and what you looked at
        # last. The instructions are docked to the foot of the window instead,
        # out of the way of what you are reading.
        with Vertical(id="landing"):
            yield Static("", id="wordmark")
            yield Static("Search an artist to explore", id="home-title")
            yield PromptInput(placeholder="Artist name", id="artist")
            yield Static("Recent", id="recent-title", classes="section")
            yield ListView(id="recent")
        yield Static("", id="hint")

    def refresh_recent(self) -> None:
        """Screens call this when history may have changed.

        ScreenResume cannot be used for this: it is posted to the screen and does
        not bubble, so the app never sees it.
        """
        if not self.is_running:  # shutting down; the home screen may be gone
            return
        self.run_worker(self._refresh_recent(), exclusive=True)

    async def _refresh_recent(self) -> None:
        """Re-read history, so an artist viewed in the TUI appears without a restart."""
        from ..store.history import History

        recent = History().names()  # every one of them: the list scrolls
        with suppress(NoMatches, MountError):
            await self._render_recent(recent)

    async def _render_recent(self, recent: list[str]) -> None:
        artist_input = self.query_one("#artist", Input)
        app = self.app
        assert isinstance(app, LfmApp)  # `Screen.app` is typed as the base App
        app.suggester.set_recents(recent)
        artist_input.suggester = app.suggester  # remembered names, and looked-up ones
        self.query_one("#hint", Static).update(
            "Type a name and press Enter"
            + (HISTORY_HINT if recent else "")
            + "  ·  F1 for shortcuts"
        )
        # a hidden list is skipped by the focus chain, so Tab does nothing odd
        for widget_id in ("#recent-title", "#recent"):
            self.query_one(widget_id).display = bool(recent)
        view = self.query_one("#recent", ListView)
        keep = view.index or 0  # the cursor holds its place across a refresh
        await view.clear()
        if recent:
            await view.mount(*[ListItem(RowLabel(plain(name), classes="name")) for name in recent])
            # Forgetting one entry must not send the cursor back to the top, which
            # looked like the list jumping up from the bottom. Set twice: clearing
            # the list posts a highlight for the new first row, so the assignment
            # below lands first and is then undone.
            view.index = min(keep, len(recent) - 1)
            self.call_after_refresh(lambda: setattr(view, "index", min(keep, len(recent) - 1)))

    async def action_forget_recent(self) -> None:
        """Del on the Recent list forgets the highlighted artist."""
        view = self.query_one("#recent", ListView)
        if self.focused is not view or view.index is None or not view.children:
            return
        name = str(view.children[view.index].query_one(Label).content)
        from ..store.history import History

        History().remove(name)
        await self._refresh_recent()
        if view.display and view.children:
            view.index = 0
            view.focus(scroll_visible=False)
        else:
            self.query_one("#artist", Input).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a recent artist fills the prompt, ready to search or edit."""
        if event.list_view.id != "recent":
            return
        chosen = event.item.query_one(Label).content
        artist_input = self.query_one("#artist", Input)
        artist_input.value = str(chosen)
        artist_input.focus()

    async def on_mount(self) -> None:
        await self._refresh_recent()
        if self.artist:
            self.open_artist(self.artist)
        # one clock drives every mounted bar; covered screens keep their
        # queues advancing while you browse elsewhere
        self.set_interval(0.5, self._tick_bars)
        self._show_wordmark()

    async def _tick_bars(self) -> None:
        try:
            bars = [screen for screen in self.screen_stack if isinstance(screen, WithPlayerBar)]
        except ScreenStackError:
            return  # the app is going down; there is nothing left to draw
        if not bars:
            return  # the landing page has no bar: nothing to draw and nothing to advance
        # One look at the player for the whole tick. mpv is a process on the far end of
        # a socket, and every bar on the stack is showing the same moment of it.
        status = await self.player.status()
        for screen in bars:
            with suppress(Exception):  # a dying bar must not take the clock down
                await screen.tick_bar(status)

    def fade_repeat_in(self) -> None:
        """Fade the repeat mark from grey to the accent, as a tab takes the keyboard.

        Same 150 ms and same curve as the tab bar's `transition: color 150ms`. The bar
        is redrawn at REPEAT_FRAME while the fade runs, because the half-second tick is
        far too coarse for 150 ms of movement.

        One interval and one end timer are kept, however often this is called: a new
        press only moves the start, and the end timer checks a token before changing
        anything, so a timer from an earlier press cannot cut a later fade short.
        """
        self._repeat_fade_token += 1
        token = self._repeat_fade_token
        self._repeat_fade_start = time.monotonic()
        if self._repeat_fade_interval is None:
            self._repeat_fade_interval = self.set_interval(REPEAT_FRAME, self._fade_frame)
        if self._repeat_fade_timer is not None:
            self._repeat_fade_timer.stop()
        self._repeat_fade_timer = self.set_timer(REPEAT_FADE, lambda: self._end_repeat_fade(token))

    def clear_repeat_flash(self) -> None:
        """End the fade with repeat: off means off, even mid-fade."""
        self._repeat_fade_token += 1
        self._repeat_fade_start = 0.0
        if self._repeat_fade_timer is not None:
            self._repeat_fade_timer.stop()
            self._repeat_fade_timer = None
        self._stop_fade_interval()

    def repeat_fade(self) -> float:
        """How far through the fade the mark is: 0 at grey, 1 at the accent, eased."""
        if not self._repeat_fade_start:
            return 1.0
        elapsed = max(0.0, time.monotonic() - self._repeat_fade_start)
        return ease_in_out_cubic(min(1.0, elapsed / REPEAT_FADE))

    def repeat_fading(self) -> bool:
        return self.repeat_fade() < 1.0

    async def _fade_frame(self) -> None:
        if not self.repeat_fading():
            self._stop_fade_interval()  # the end timer should have stopped it; be sure
            return
        await self._tick_bars()

    def _stop_fade_interval(self) -> None:
        if self._repeat_fade_interval is not None:
            self._repeat_fade_interval.stop()
            self._repeat_fade_interval = None

    async def _end_repeat_fade(self, token: int) -> None:
        if token != self._repeat_fade_token:
            return  # a later press owns the mark now
        self._repeat_fade_timer = None
        self._repeat_fade_start = 0.0
        self._stop_fade_interval()
        await self._tick_bars()  # the last frame, at the settled colour

    async def action_toggle_pause(self) -> None:
        paused = await self.player.toggle_pause()
        if paused is None:
            self.notify("nothing is playing", severity="warning", timeout=3)
        await self._tick_bars()

    async def action_seek_back(self) -> None:
        await self.player.seek(-SEEK_SECONDS)
        await self._tick_bars()

    async def action_seek_forward(self) -> None:
        await self.player.seek(SEEK_SECONDS)
        await self._tick_bars()

    async def action_seek_back_long(self) -> None:
        await self.player.seek(-LONG_SEEK_SECONDS)
        await self._tick_bars()

    async def action_seek_forward_long(self) -> None:
        await self.player.seek(LONG_SEEK_SECONDS)
        await self._tick_bars()

    async def action_seek_percent(self, tenth: int) -> None:
        """`0`-`9`: land at 0-90% of the track; long mixes finally move fast."""
        status = await self.player.status()
        if status.duration:
            await self.player.seek(int(status.duration * tenth / 10) - int(status.position))
            await self._tick_bars()

    async def action_volume_up(self) -> None:
        self._remember_volume(await self.player.change_volume(VOLUME_STEP))
        await self._tick_bars()

    async def action_volume_down(self) -> None:
        self._remember_volume(await self.player.change_volume(-VOLUME_STEP))
        await self._tick_bars()

    async def action_mute(self) -> None:
        status = await self.player.status()
        if status.volume > 0:
            self.muted_volume = int(status.volume)
            self.player.volume = 0.0
        elif self.muted_volume is not None:  # m: back to where it was
            self.player.volume = float(self.muted_volume)
            self.muted_volume = None
        await self._tick_bars()

    async def action_stop(self) -> None:
        for screen in self.screen_stack:  # the queue's owner forgets it
            if isinstance(screen, ArtistDetail):
                screen._queue_index = None
                screen.cancel_play()
        await self.player.stop()
        set_terminal_title(self, "lfm")
        await self._tick_bars()

    def welcome_text(self) -> Text:
        """The wordmark: the art in the theme's logo colour.

        One flat colour, because the art carries its own shading in the glyphs
        (`▓` over `▒`, arcs above and below); a drawn drop shadow filled the gaps
        between them and read as dirt.
        Colours come from the theme object rather than the CSS variables: those
        follow the styles and lag a beat behind a theme switch, so the wordmark
        would wear the colours of the theme you just left.
        """
        theme = self.get_theme(self.theme)
        accent = str(theme.accent) if theme else "#ffa62b"
        if not accent.startswith("#"):
            accent = "#ffa62b"  # a theme that hands us no hex
        logo = theme.variables.get("logo") if theme else None
        colour = logo if isinstance(logo, str) and logo.startswith("#") else accent
        # One span over the whole art, so the tests (and a theme re-shade) can read
        # the colour back off the spans rather than a base style.
        text = Text()
        text.append("\n".join(WORDMARK_ROWS), style=f"bold {colour}")
        return text

    def _show_wordmark(self, width: int | None = None) -> None:
        """The flourish needs room: a narrow terminal gets the plain title."""
        with suppress(NoMatches):
            wordmark = self.query_one("#wordmark", Static)
            wordmark.display = (self.size.width if width is None else width) >= (WORDMARK_MIN_WIDTH)
            if wordmark.display:
                wordmark.update(self.welcome_text())

    def on_resize(self, event: Resize) -> None:
        self._show_wordmark(event.size.width)  # self.size is still the old size here

    def progress_colour(self) -> str:
        """The seek bar's colour: the theme's own `seek` if it names one, else the accent.

        Obsidian gives its bar the blue it already carries, because obsidian and midnight
        share an accent and the bar was otherwise identical in both. A theme that names no
        `seek` keeps the accent, which is where the bar's gradient used to end.
        """
        variables = self.get_css_variables()
        colour = str(variables.get("seek") or variables.get("accent", ""))
        if not colour.startswith("#"):  # a theme that hands us no hex: use the app's orange
            colour = "#ffa62b"
        return colour

    def theme_colour(self, name: str, fallback: str) -> str:
        """A colour taken from the theme object, not from the resolved CSS variables.

        `get_css_variables()` lags a theme switch by a beat: while the styles are being
        re-applied it still answers with the theme you just left. Anything drawn per frame
        from it — the volume's track, the equalizer's colours — therefore showed the old
        theme's palette for up to a bar tick, which is the glitch on `T`. The wordmark has
        taken its colours from the theme object for exactly this reason; these do now.

        A theme's own `variables` come first (that is where a custom colour like `spectrum`
        or `logo` lives), then its typed fields. The resolved variables remain the last
        word for what only they carry, such as a composited `text-muted`.
        """
        theme = self.get_theme(self.theme)
        if theme is not None:
            for candidate in (theme.variables.get(name), getattr(theme, name, None)):
                if isinstance(candidate, str) and candidate.startswith("#"):
                    return candidate
        resolved = str(self.get_css_variables().get(name, ""))
        return resolved if resolved.startswith("#") else fallback

    def volume_track(self) -> str:
        """The volume meter's background, so a low level still shows its full height.

        The glyph alone runs from `▁` to `█`: at low volume there was nothing beside it
        to say how tall the scale is. A step between the page and the body text, so the
        fill still reads as the brighter thing.
        """
        return blend(
            self.theme_colour("background", "#232332"),
            self.theme_colour("foreground", "#e8e3da"),
            0.25,
        )

    def spectrum_palette(self) -> tuple[str, str, str]:
        """The strip's colours, quiet to loud.

        A theme may name a `spectrum` colour, and then the strip is that one colour shaded
        into the background for the quiet bars and into the foreground for the loud ones —
        obsidian gives it its blue, so the strip belongs to the theme instead of running
        green to red through it. A theme naming none keeps the old ramp: its success,
        accent and error, which is what the strip used when every theme was orange.
        """
        voice = self.theme_colour("spectrum", "")
        if voice:
            return (
                blend(voice, self.theme_colour("background", "#232332"), 0.35),
                voice,
                blend(voice, self.theme_colour("foreground", "#e8e3da"), 0.35),
            )
        return (
            self.theme_colour("success", SPECTRUM_COLOURS[0]),
            self.theme_colour("accent", SPECTRUM_COLOURS[1]),
            self.theme_colour("error", SPECTRUM_COLOURS[2]),
        )

    def action_toggle_theme(self) -> None:
        """`T`: the next theme of the three, wrapping round.

        The config file decides which one lfm starts in; this is the session's.
        """
        rotation = [name for name in THEMES.values() if name in self.available_themes]
        self.theme = (
            rotation[(rotation.index(self.theme) + 1) % len(rotation)]
            if self.theme in rotation
            else rotation[0]
        )
        # the theme's variables change with the styles, so shade after they have
        self.call_after_refresh(self._show_wordmark)
        # and redraw the bars now: their palette follows the theme object, but they would
        # otherwise keep the old one until the clock next came round, half a second later
        self.run_worker(self._tick_bars(), group="theme")

    def action_jump_playing(self) -> None:
        """`f`: go to the screen that owns the playing queue and show its row."""
        for screen in reversed(self.screen_stack):
            if isinstance(screen, ArtistDetail) and screen._queue_index is not None:
                while self.screen is not screen:
                    self.pop_screen()
                screen.reveal_playing()
                return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """The bar's buttons work on whichever screen is hosting them."""
        actions: dict[str, Callable[[], Awaitable[None]]] = {
            "toggle": self.action_toggle_pause,
            "stop": self.action_stop,
            "seek-back": self.action_seek_back,
            "seek-forward": self.action_seek_forward,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            self.run_worker(action(), exclusive=True)

    def _remember_volume(self, level: int) -> None:
        """A level nobody can save is worth one warning, not silence."""
        problem = remember_volume(self, level)
        if problem:
            self.notify(
                f"volume will not be remembered: {problem}",
                severity="warning",
                timeout=6,
                markup=False,
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.open_artist(event.value)

    def open_artist(self, name: str) -> None:
        name = name.strip()
        if not name:
            self.notify("enter an artist name", severity="warning", timeout=3)
            return
        self.query_one("#artist", Input).value = ""
        self.push_screen(ArtistDetail(name, self.client, self.player, self.options))

    def action_shortcuts(self) -> None:
        """`h` opens the key sheet, and `h` again (or Escape) closes it."""
        if isinstance(self.screen, ShortcutsScreen):
            self.pop_screen()
        else:
            self.push_screen(ShortcutsScreen())

    async def action_stop_and_quit(self) -> None:
        await self.player.stop()
        self.exit()

    async def on_unmount(self) -> None:
        await self.player.stop()
