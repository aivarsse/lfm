"""Shared pieces: row builders, the prompt, the player bar, small helpers."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from textual.app import ScreenStackError
from textual.color import Color
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.style import Style
from textual.widgets import (
    Input,
    Label,
    ListView,
    Static,
)
from tuihelpers import (
    Beatles,
    FakeClient,
    FakePlayer,
    make_app,
    opened_detail,
    played_first_track,
    text_of,
)

from lfm.api.models import (
    ArtistInfo,
    SearchArtist,
    Track,
)
from lfm.ui.app import REPEAT_FADE
from lfm.ui.render import (
    bio_text,
    blend,
    progress_bar,
    stats_line,
)
from lfm.ui.theme import LFM_CREAM, LFM_OBSIDIAN
from lfm.ui.widgets import set_terminal_title


class CountingSearchClient(FakeClient):
    """Records what the prompt asked Last.fm for."""

    def __init__(self) -> None:
        super().__init__()
        self.searches: list[str] = []

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
        self.searches.append(artist)
        return await super().search_matches(artist, limit)


def hits(*pairs: tuple[str, int]) -> list[SearchArtist]:
    """A ranked `artist.search` answer: (name, listeners) as the live API returns them."""
    return [SearchArtist(name=name, listeners=listeners) for name, listeners in pairs]


class Jacksons(FakeClient):
    """`artist.search` on "Michael Jackson" — the live numbers, the collaboration far behind."""

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
        return hits(
            ("Michael Jackson", 7_226_820),
            ("Michael Jackson & Janet Jackson", 83_428),
            ("Michael Jackson & Janet Jackson (Live)", 4_101),
        )


class Chaining(FakeClient):
    """Last.fm ranks the collaboration first as the query itself grows (#111's repro).

    Textual hands the suggester the value case-folded, so the answers are keyed that way.
    """

    ANSWERS = {
        "michael jack": [
            ("Michael Jackson", 7_226_820),
            ("Michael Jackson & Janet Jackson", 83_428),
        ],
        "michael jackson": [
            ("Michael Jackson & Janet Jackson", 83_428),
            ("Michael Jackson", 7_226_820),
        ],
        "michael jackson & janet jackson": [
            ("Michael Jackson & Janet Jackson (Live)", 4_101),
            ("Michael Jackson & Janet Jackson", 83_428),
        ],
    }

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
        return hits(*self.ANSWERS.get(artist.casefold(), []))


def test_bio_text_strips_html_and_read_more():
    summary = "<a href='#'>Burial</a> is great. <a href='#'>Read more on Last.fm</a>"
    assert bio_text(summary) == "Burial is great."


def test_bio_text_truncates():
    assert len(bio_text("x" * 1000)) == 600


def test_stats_line():
    info = ArtistInfo.model_validate(
        {"name": "B", "stats": {"listeners": "1000", "playcount": "2000"}}
    )
    assert stats_line(info) == "1,000 listeners · 2,000 plays"


async def test_the_prompt_completes_from_history_and_lists_recents():
    from lfm.store.history import History

    History().add("Burial")
    app = make_app(FakePlayer())
    async with app.run_test() as _pilot:
        suggester = app.query_one("#artist", Input).suggester
        assert suggester is not None
        assert await suggester.get_suggestion("bur") == "Burial"
        assert "Tab for recent" in text_of(app.query_one("#hint", Static))
        assert [str(row.query_one(Label).content) for row in app.query_one("#recent").children] == [
            "Burial"
        ]


async def test_tab_reaches_the_recent_list_and_enter_fills_the_prompt():
    from lfm.store.history import History

    History().add("Burial")
    History().add("Kode9")  # most recent, so it is the first row
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        artist_input = app.query_one("#artist", Input)
        assert artist_input.has_focus

        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#recent", ListView).has_focus

        await pilot.press("enter")  # select the highlighted recent artist
        await pilot.pause()
        assert artist_input.value == "Kode9"
        assert artist_input.has_focus


async def test_there_is_no_recent_list_without_history():
    app = make_app(FakePlayer())
    async with app.run_test() as _pilot:
        recent = app.query_one("#recent", ListView)
        assert recent.display is False  # hidden, so Tab skips it
        assert list(recent.children) == []
        assert "Tab for recent" not in text_of(app.query_one("#hint", Static))


async def test_del_forgets_the_selected_recent_artist():
    from lfm.store.history import History

    History().add("Burial")
    History().add("Kode9")  # first row, so it is the highlighted one
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        await pilot.press("tab")  # focus the recent list
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()

        assert History().names() == ["Burial"]
        recent = app.query_one("#recent", ListView)
        assert [str(r.query_one(Label).content) for r in recent.children] == ["Burial"]


async def test_forgetting_the_last_entry_hides_the_list():
    from lfm.store.history import History

    History().add("Burial")
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()

        assert History().names() == []
        assert app.query_one("#recent", ListView).display is False
        assert app.query_one("#artist", Input).has_focus


async def test_del_outside_the_recent_list_forgets_nothing():
    from lfm.store.history import History

    History().add("Burial")
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)  # focus is in the tracks list
        await pilot.press("delete")
        await pilot.pause()
        assert History().names() == ["Burial"]


async def test_del_in_the_search_box_forgets_nothing():
    from lfm.store.history import History

    History().add("Burial")
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        await pilot.press("delete")  # the input has focus and consumes the key
        await pilot.pause()
        assert History().names() == ["Burial"]


async def test_a_viewed_artist_shows_up_without_a_restart():
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        assert app.query_one("#recent", ListView).display is False

        artist_input = app.query_one("#artist", Input)
        artist_input.value = "Burial"
        await pilot.press("enter")
        await opened_detail(app, pilot)
        await pilot.press("b")
        await pilot.pause()

        recent = app.query_one("#recent", ListView)
        assert recent.display is True
        assert [str(row.query_one(Label).content) for row in recent.children] == ["Burial"]


async def test_opening_an_artist_remembers_it():
    from lfm.store.history import History

    app = make_app(FakePlayer(), artist="Kode9")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        assert History().names()[:1] == ["Kode9"]


async def test_selecting_track_plays_it_and_updates_the_bar():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        assert player.played == [("Burial", "Black Sun")]
        assert text_of(detail.query_one("#bar-info", Static)) == "1/2  ·  ↻  ·  vol ▇ 100%"
        assert text_of(detail.query_one("#playing", Static)) == "0:00 / 1:40"


async def test_progress_bar_tracks_position(monkeypatch):
    drawn: list[tuple[float, float, int, str]] = []

    def spy(position: float, duration: float, width: int, colour: str):
        drawn.append((position, duration, width, colour))
        return progress_bar(position, duration, width, colour)

    monkeypatch.setattr("lfm.ui.widgets.progress_bar", spy)
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        player.position = 40.0
        await detail.refresh_bar()
        position, duration, width, colour = drawn[-1]
        assert (position, duration) == (40.0, 100.0)
        assert width > 0  # the screen hands it the width to draw across
        assert colour.startswith("#")  # one colour, not a gradient's two ends


async def test_the_seek_buttons_return_only_when_the_progress_has_room():
    for width, shown in ((67, False), (68, True)):
        app = make_app(FakePlayer(), artist="Burial")
        async with app.run_test(size=(width, 30)) as pilot:
            detail = await played_first_track(app, pilot)
            await pilot.pause(0.2)
            buttons = detail.query("#seek-back, #seek-forward, #stop")
            assert all(button.display is shown for button in buttons)
            progress = detail.query_one("#progress", Static).size.width
            assert progress >= (12 if shown else 20)


async def test_detail_shows_api_error_and_suggestions():
    app = make_app(FakePlayer(), artist="Burial", client=FakeClient(failing_info=True))
    async with app.run_test() as pilot:
        await pilot.pause()
        detail = app.screen
        meta = text_of(detail.query_one("#meta", Static))
        assert "error: boom" in meta
        assert "Did you mean" in meta
        assert "b for the prompt" in meta
        # the empty page is hidden, not left as four blank windows
        assert detail.query_one("#columns").display is False
        assert detail.query_one("#about-label").display is False
        assert detail.query_one("#bio").display is False


def test_the_window_title_cannot_inject_terminal_sequences():
    class Driver:
        def __init__(self) -> None:
            self.written = ""

        def write(self, text: str) -> None:
            self.written = text

    driver = Driver()
    set_terminal_title(SimpleNamespace(driver=driver), "safe\x07\x1b]0;pwned\x07")
    assert driver.written == "\x1b]2;safe  ]0;pwned\x07"


async def test_a_newline_in_the_title_does_not_grow_the_bar():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        player.current_title = "Line one\nLine two"
        await detail.refresh_bar()
        await pilot.pause()
        playing = detail.query_one("#now-playing", Static)
        assert text_of(playing) == "Line one Line two"
        assert playing.region.height == 1


async def test_a_long_track_name_ellipsizes_instead_of_being_cut():
    class LongTracks(FakeClient):
        async def top_tracks(self, artist: str) -> list[Track]:
            return [Track(name="A Very Long Track Title " * 8, playcount=1)]

    app = make_app(FakePlayer(), artist="Burial", client=LongTracks())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.pause()
        rendered = detail.query_one("#tracks RowLabel").render()
        assert isinstance(rendered, Content)
        assert rendered.plain.endswith("…")
        assert "\n" not in rendered.plain


async def test_next_and_previous_walk_the_track_list():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("n")
        await app.workers.wait_for_complete()
        assert player.played[-1] == ("Burial", "Sine")
        assert text_of(detail.query_one("#bar-info", Static)) == "2/2  ·  ↻  ·  vol ▇ 100%"
        await pilot.press("p")
        await app.workers.wait_for_complete()
        # exactly one play per press: no duplicate from the poller seeing the
        # gap while the previous track was being replaced
        assert player.played == [
            ("Burial", "Black Sun"),
            ("Burial", "Sine"),
            ("Burial", "Black Sun"),
        ]


def repeat_style(detail) -> Style:
    """The style of the repeat mark's span in the bar."""
    rendered = detail.query_one("#bar-info", Static).render()
    assert isinstance(rendered, Content)
    start = rendered.plain.index("↻")
    span = next(span for span in rendered.spans if span.start <= start < span.end)
    assert isinstance(span.style, Style)
    return span.style


async def test_repeat_loops_the_current_track():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("r")
        await app.workers.wait_for_complete()
        assert "↻" in text_of(detail.query_one("#bar-info", Static))

        player.finish()  # the track ended
        await detail.refresh_bar()
        assert player.played == [("Burial", "Black Sun"), ("Burial", "Black Sun")]


async def test_repeat_can_be_turned_off():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.press("r")
        await app.workers.wait_for_complete()
        assert "↻" in text_of(detail.query_one("#bar-info", Static))  # the mark stays
        style = repeat_style(detail)
        assert style.foreground == Color.parse(app.theme_colour("text-muted", "#acacb5"))
        assert style.background is None

        player.finish()
        await detail.refresh_bar()
        assert player.played == [("Burial", "Black Sun"), ("Burial", "Sine")]


async def test_the_repeat_mark_is_grey_until_repeat_is_on():
    """It stays in the bar while off, in the bar's own muted ink."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        off = repeat_style(detail)
        assert off.bold
        assert off.foreground == Color.parse(app.theme_colour("text-muted", "#acacb5"))
        assert off.background is None


async def test_the_repeat_mark_is_bold_and_wears_the_accent():
    """Once the fade has landed, the mark is the accent, bold, with no filled cell.

    The glyph's own run carries the air after it: a run that starts with spaces has
    them dropped, which would leave the mark against the dot on its right.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause(REPEAT_FADE + 0.1)  # let the fade land
        assert app.repeat_fade() == 1.0
        on = repeat_style(detail)
        assert on.bold
        assert on.foreground == Color.parse(app.theme_colour("accent", ""))
        assert on.background is None
        rendered = detail.query_one("#bar-info", Static).render()
        assert isinstance(rendered, Content)
        start = rendered.plain.index("↻")
        assert rendered.plain[start : start + 3] == "↻  "  # the gap kept with it


async def test_turning_repeat_on_fades_the_mark_in():
    """The mark eases from the muted ink to the accent, on the tabs' curve."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause(REPEAT_FADE + 0.1)  # first fade settled
        app.fade_repeat_in()  # a fresh fade we can hold at a known point
        app._repeat_fade_start = time.monotonic() - REPEAT_FADE / 2  # halfway through
        await detail.refresh_bar()
        muted = Color.parse(app.theme_colour("text-muted", "#acacb5"))
        accent = Color.parse(app.theme_colour("accent", ""))
        mid = repeat_style(detail)
        assert mid.foreground not in (muted, accent)  # between the two, not at either
        assert mid.background is None

        await app._end_repeat_fade(app._repeat_fade_token)  # settle it, without waiting
        after = repeat_style(detail)
        assert after.foreground == accent
        assert after.background is None
        assert app.repeat_fade() == 1.0


async def test_a_stale_fade_timer_cannot_end_a_newer_fade():
    """A timer that fired just as `r` was pressed again must not cut the new fade."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("r")  # on: the first fade
        await app.workers.wait_for_complete()
        stale = app._repeat_fade_token

        await pilot.press("r")  # off, which stops that timer
        await pilot.press("r")  # on again: a fresh fade, a newer token
        await app.workers.wait_for_complete()
        app._repeat_fade_start = time.monotonic()  # hold the new fade at its start
        await detail.refresh_bar()
        assert app.repeat_fading()

        await app._end_repeat_fade(stale)  # the old timer's callback, arriving late
        assert app.repeat_fading()  # and it left the newer fade alone
        assert repeat_style(detail).foreground != Color.parse(app.theme_colour("accent", ""))


async def test_spamming_repeat_leaves_no_stuck_fade_or_wrong_state():
    """A stale press's timer or interval must not outlive the state."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        for _ in range(5):  # an odd number: on, and every press restarts the fade
            await pilot.press("r")
        await app.workers.wait_for_complete()
        assert app.repeat_on is True
        await pilot.pause(REPEAT_FADE + 0.1)
        style = repeat_style(detail)
        assert style.background is None
        assert style.foreground == Color.parse(app.theme_colour("accent", ""))
        assert app._repeat_fade_interval is None  # the redraw interval was stopped

        await pilot.press("r")  # off ends any fade at once
        await app.workers.wait_for_complete()
        assert app.repeat_on is False
        off = repeat_style(detail)
        assert off.background is None
        assert off.foreground == Color.parse(app.theme_colour("text-muted", "#acacb5"))
        assert app._repeat_fade_interval is None


async def test_the_recent_block_lines_up_with_the_rest_of_the_screen(isolated_env):
    """The list and the prompt share a left edge and a width, at any window size."""
    from lfm.store.history import History

    History().add("Burial")  # so the list has a region to measure
    for width in (100, 80):
        app = make_app(FakePlayer())
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            recent = app.screen.query_one("#recent", ListView).region
            prompt = app.screen.query_one("#artist", Input).region
            assert (recent.x, recent.width) == (prompt.x, prompt.width)


async def test_a_bracketed_youtube_title_survives_in_the_player_bar():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        player.active = True
        player.current_title = "Archangel [Official Video] [HD]"
        await detail.refresh_bar()
        # Textual would otherwise read the brackets as markup and drop them
        assert "[Official Video] [HD]" in text_of(detail.query_one("#now-playing", Static))


async def test_the_now_playing_line_keeps_air_above_the_controls():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        now = detail.query_one("#now-playing", Static)
        controls = detail.query_one("#controls")
        assert controls.region.y - (now.region.y + now.region.height) >= 1


async def test_the_bar_info_sits_at_the_right_end():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("r")  # repeat on, so the line has all three parts
        await app.workers.wait_for_complete()
        title = detail.query_one("#now-playing", Static)
        info = detail.query_one("#bar-info", Static)
        assert text_of(title).startswith("Black Sun")  # the title has the line to itself
        assert text_of(info) == "1/2  ·  ↻  ·  vol ▇ 100%"
        assert info.region.x > title.region.x  # the title has the left, the numbers the right
        assert info.region.x + info.region.width == detail.query_one("#player-bar").region.x + 98


async def test_the_prompt_has_no_bar_but_the_artist_page_keeps_its_own():
    """The first page is the welcome; playback is the other pages' business."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            app.screen.query_one("#player-bar")

    playing = make_app(FakePlayer(), artist="Burial")
    async with playing.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(playing, pilot)
        assert detail.query_one("#player-bar") is not None


async def test_the_bar_clock_survives_an_empty_screen_stack(monkeypatch):
    """A timer can fire while the app is going down; it must not raise."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)

        def empty(_app: object) -> list[Screen]:
            raise ScreenStackError("No screens on stack")

        monkeypatch.setattr(type(app), "screen_stack", property(empty))
        await app._tick_bars()  # no exception is the whole assertion


async def test_the_band_says_where_you_are():
    prompt = make_app(FakePlayer())  # nothing open yet: the prompt is the screen
    async with prompt.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert text_of(prompt.screen.query_one("#band-name", Static)) == "lfm"
        assert text_of(prompt.screen.query_one("#band-context", Static)) == "home"

    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        assert text_of(detail.query_one("#band-context", Static)) == "artist"

        await pilot.press("l")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app.screen.query_one("#band-context", Static)) == "similar artists"


async def test_the_prompt_shows_every_artist_it_remembers(isolated_env):
    from lfm.store.history import History

    history = History()
    for number in range(12):  # more stored than shown
        history.add(f"Artist {number}")
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        recent = app.query_one("#recent", ListView)
        rows = [str(row.query_one(Label).content) for row in recent.children]
        assert len(rows) == 12  # every one, newest first; the list scrolls
        assert rows == [f"Artist {n}" for n in range(11, -1, -1)]


async def test_the_landing_page_is_centred_with_the_instructions_at_the_foot(isolated_env):
    from lfm.store.history import History

    History().add("Burial")  # a recents list with a row, so it has a region to measure
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen

        def centre(selector: str) -> float:
            region = screen.query_one(selector).region
            return region.x + region.width / 2

        # one middle line for the heading, the prompt and the list
        assert [round(centre(one)) for one in ("#home-title", "#artist", "#recent")] == [
            round(app.size.width / 2)
        ] * 3

        hint = screen.query_one("#hint", Static)
        recent = screen.query_one("#recent", ListView)
        assert hint.region.y > recent.region.y  # below what you are reading
        assert hint.region.y == app.size.height - 1  # and on the window's last row


async def test_the_recents_show_what_fits_and_scroll_for_the_rest(isolated_env):
    from lfm.store.history import History

    history = History()
    for number in range(15):
        history.add(f"Artist {number}")
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        view = app.screen.query_one("#recent", ListView)
        assert len(view.children) == 15  # every memorised title is there to reach
        assert view.size.height == 10  # ten rows by default, so the page ends higher
        assert view.max_scroll_y == 5  # and the other five are behind a scrollbar


async def test_forgetting_an_entry_keeps_the_cursor_where_it_was(isolated_env):
    from lfm.store.history import History

    history = History()
    for name in ("Burial", "Kode9", "Zomby"):
        history.add(name)  # newest first, so the list reads Zomby, Kode9, Burial
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        view = app.screen.query_one("#recent", ListView)
        view.focus()
        view.index = 2  # the oldest, at the bottom of the list
        await pilot.press("delete")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [str(row.query_one(Label).content) for row in view.children] == ["Zomby", "Kode9"]
        assert view.index == 1  # still on the bottom row, not back at the top


async def test_the_landing_column_shrinks_with_the_terminal(isolated_env):
    """It used to keep its 60 and overflow: heading and list parted company."""
    from lfm.store.history import History

    History().add("Burial")
    app = make_app(FakePlayer())
    async with app.run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        title = app.screen.query_one("#recent-title")
        listing = app.screen.query_one("#recent", ListView)
        assert listing.region.width <= 50  # no run-off, and no sideways scrollbar
        assert listing.region.x == title.region.x  # the heading sits over its list

    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#recent", ListView).region.width == 60  # and stays 60


async def test_typing_suggests_an_artist_name_lastfm_knows():
    """A name the history has never heard of still completes: the search fills it in."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#artist", Input).suggester is app.suggester
        assert await app.suggester.get_suggestion("bur") == "Burial"


async def test_a_remembered_name_never_costs_a_lookup():
    client = CountingSearchClient()
    app = make_app(FakePlayer(), client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.suggester.set_recents(["Berlin"])
        assert await app.suggester.get_suggestion("ber") == "Berlin"
        assert client.searches == []
        assert await app.suggester.get_suggestion("b") is None  # one letter is a lottery


async def test_typing_quickly_asks_lastfm_once():
    """Every keystroke fires the suggester; only the last one should reach the API."""
    client = CountingSearchClient()
    app = make_app(FakePlayer(), client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await asyncio.gather(
            *(app.suggester.get_suggestion(value) for value in ("bu", "bur", "buri"))
        )
        assert client.searches == ["buri"]


async def test_the_right_arrow_accepts_the_suggestion(isolated_env):
    """The whole path: type, wait out the debounce, press ->, and the name is there."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#artist", Input)
        prompt.value = "bur"
        await pilot.pause(0.4)  # SUGGEST_DELAY plus the fake search
        await pilot.press("right")
        await pilot.pause()
        assert prompt.value == "Burial"


async def test_tab_takes_the_suggestion_and_the_next_one_reaches_the_history(isolated_env):
    """Tab completes while a name is offered; with it taken, Tab moves on."""
    from lfm.store.history import History

    History().add("Berlin")  # so there is a recent list to move on to
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#artist", Input)
        prompt.value = "bur"
        await pilot.pause(0.4)  # SUGGEST_DELAY plus the fake search

        await pilot.press("tab")
        await pilot.pause()
        assert prompt.value == "Burial"  # the suggestion, taken

        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#recent", ListView).has_focus  # and then the history


async def test_a_completion_does_not_bolt_a_second_name_on():
    """Typing a name Last.fm knows in full offered "The Beatles, The Beatles"."""
    app = make_app(FakePlayer(), client=Beatles())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("the Beatles") is None
        assert await app.suggester.get_suggestion("the Beatl") == "The Beatles"


async def test_a_finished_name_is_not_completed_further():
    """83k listeners for the collaboration against 7.2M for Michael Jackson is not an offer."""
    app = make_app(FakePlayer(), client=Jacksons())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("Michael Jackson") is None
        assert await app.suggester.get_suggestion("Michael Jack") == "Michael Jackson"


async def test_the_finished_name_reaches_the_history_on_the_first_tab(isolated_env):
    from lfm.store.history import History

    History().add("Berlin")  # so the recent list is on screen to move to
    app = make_app(FakePlayer(), client=Jacksons())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#artist", Input)
        prompt.value = "Michael Jackson"
        await pilot.pause(0.4)  # SUGGEST_DELAY plus the fake search
        await pilot.press("tab")
        await pilot.pause()
        assert prompt.value == "Michael Jackson"  # nothing to take
        assert app.query_one("#recent", ListView).has_focus


async def test_a_stub_artist_does_not_switch_the_completion_off():
    """An exact match used to mean no suggestion: Last.fm has one-word acts (742 listeners)."""

    class Stubs(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            return hits(("Burial", 1_314_054), ("Burl Ives", 318_742), ("Bur", 742))

    app = make_app(FakePlayer(), client=Stubs())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("bur") == "Burial"


async def test_a_stub_act_at_the_top_does_not_switch_the_completion_off():
    """The echo tops the ranking; the act you mean wins on listeners."""

    class StubFirst(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            return hits(
                ("Mich", 44_313), ("Michael Jackson", 7_226_820), ("Michael Bublé", 1_010_000)
            )

    app = make_app(FakePlayer(), client=StubFirst())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("mich") == "Michael Jackson"


async def test_the_more_listened_artist_completes_what_you_typed():
    """Live: "Michael" is an artist itself (68k), and "Michael Jackson" (7.2M) is the completion."""

    class Michaels(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            return hits(
                ("Michael", 68_219), ("Michael Jackson", 7_226_820), ("Michael Bublé", 1_010_000)
            )

    app = make_app(FakePlayer(), client=Michaels())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("Michael") == "Michael Jackson"


async def test_a_weaker_completion_leaves_a_typed_name_alone():
    """Live: everything starting with "mich" has fewer listeners than "Mich" itself."""

    class Tiny(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            return hits(("Mich", 44_313), ("Mich Gerber", 35_425), ("Mich Duval", 654))

    app = make_app(FakePlayer(), client=Tiny())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("mich") is None


async def test_the_best_match_leads_not_the_shortest_one():
    """`mich` completes to the act you mean, not to the one-word `Michael`."""

    class Michaels(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            return hits(
                ("Michael Jackson", 7_226_820), ("Michael", 68_219), ("Michael Bublé", 1_010_000)
            )

    app = make_app(FakePlayer(), client=Michaels())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("mich") == "Michael Jackson"


async def test_a_name_just_taken_is_not_completed_further():
    """Completing the name we just completed to is how the chain gets built (#114)."""
    app = make_app(FakePlayer(), client=Chaining())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("Michael Jack") == "Michael Jackson"
        assert await app.suggester.get_suggestion("Michael Jackson") is None


async def test_a_taken_suggestion_does_not_chain_and_the_next_tab_reaches_history(
    isolated_env,
):
    """Every Tab used to take a longer act, so the second one never reached the recent list."""
    from lfm.store.history import History

    History().add("Berlin")  # so there is a recent list to move on to
    app = make_app(FakePlayer(), client=Chaining())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#artist", Input)
        prompt.value = "Michael Jack"
        await pilot.pause(0.4)  # SUGGEST_DELAY plus the fake search

        await pilot.press("tab")
        taken = prompt.value
        assert taken == "Michael Jackson"  # the suggestion, taken

        await pilot.pause(0.4)  # and the suggester answers again for the value it produced
        await pilot.press("tab")
        await pilot.pause()
        assert prompt.value == taken  # the chain used to take "... & Janet Jackson" here
        assert app.query_one("#recent", ListView).has_focus  # and then the history


# --- scrobbling what the player plays ---------------------------------------


async def test_the_volume_glyph_does_not_move_when_the_number_gets_shorter():
    """`100%` and ` 95%` are both four cells wide.

    The readout is right-aligned as a block, so a narrower number used to slide the whole
    thing right -- including the glyph, which moved under the cursor while `[` was held.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        columns = []
        for volume in (100, 95, 5):
            player.volume = float(volume)
            await detail.refresh_bar()
            row = text_of(detail.query_one("#bar-info", Static))
            columns.append(row.index("vol ") + len("vol "))
            assert row.endswith(f"{volume:>3}%")  # padded, so the width never changes
        assert len(set(columns)) == 1, f"the glyph moved between volumes: {columns}"


async def test_an_ambiguous_prefix_completes_to_nothing():
    """#174: a prefix that fits several popular acts is not an answer to any of them."""

    class The(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            # the Beatles' count is live (it is in the suggester's own docstring); the rival
            # is illustrative, which is exactly why the margin is flagged for a live check.
            return hits(("The Beatles", 6_685_905), ("The Killers", 2_900_000))

    app = make_app(FakePlayer(), client=The())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("the") is None


async def test_a_clear_winner_still_completes():
    """The margin must not turn every prefix into nothing."""

    class Clear(FakeClient):
        async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
            return hits(("Massive Attack", 3_100_000), ("Massive Wagons", 90_000))

    app = make_app(FakePlayer(), client=Clear())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("Massive") == "Massive Attack"


async def test_a_two_letter_prefix_names_nothing():
    """`Ra` -> `Ra Ra Riot` was the other half of #174."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await app.suggester.get_suggestion("Ra") is None


async def test_the_bar_palette_follows_a_theme_switch_without_waiting_for_a_tick():
    """The resolved CSS variables lag a switch, so the volume's track kept the colours of
    the theme you just left until the bar clock next came round — the glitch on `T`."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.theme = LFM_OBSIDIAN.name
        await pilot.pause()
        before = app.volume_track()
        spectrum_before = app.spectrum_palette()

        app.theme = LFM_CREAM.name
        # No pause: the palette must already belong to the new theme.
        assert app.volume_track() != before
        background, foreground = LFM_CREAM.background, LFM_CREAM.foreground
        assert background is not None and foreground is not None
        assert app.volume_track() == blend(background, foreground, 0.25)
        assert app.spectrum_palette() != spectrum_before
