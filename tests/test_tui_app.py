"""The app itself: the landing prompt, themes, playback keys, scrobbling."""

import asyncio

from rich.cells import cell_len
from rich.text import Text
from textual.theme import BUILTIN_THEMES
from textual.widgets import (
    Button,
    Input,
    Label,
    ListView,
    Static,
)
from tuihelpers import (
    COUNTS,
    CountedClient,
    FakeClient,
    FakePlayer,
    bar_info,
    make_app,
    marker_texts,
    opened_detail,
    opened_similar,
    played_first_track,
    settled,
    text_of,
)

from lfm.api.models import (
    SimilarResponse,
)
from lfm.playback.player import PlayerStatus
from lfm.playback.scrobble import Drain, Play
from lfm.ui.app import (
    LfmApp,
)
from lfm.ui.artist import (
    MARK,
    ArtistDetail,
)
from lfm.ui.options import TuiOptions
from lfm.ui.render import (
    volume_glyph,
)
from lfm.ui.screens import (
    SimilarScreen,
)
from lfm.ui.theme import (
    LFM_MIDNIGHT,
    THEMES,
)
from lfm.ui.widgets import (
    WORDMARK_ROWS,
    remember_volume,
)


class FakeScrobbling:
    """Stands in for scrobble.Scrobbling: records plays, sends nothing."""

    def __init__(self, drained: Drain | None = None) -> None:
        self.queued: list[Play] = []
        self.playing: list[tuple[str, str]] = []
        self.drains = 0
        self._drained = drained if drained is not None else Drain(accepted=1)

    def queue(self, play: Play) -> None:
        self.queued.append(play)

    async def now_playing(self, play: Play) -> None:
        self.playing.append((play.artist, play.track))

    async def drain(self) -> Drain:
        self.drains += 1
        return self._drained


class FakeSpectrum:
    """Records lifecycle calls; supplies two loud bands and silence after."""

    def __init__(self, bars: int) -> None:
        self.bars = bars
        self.started = False
        self.stopped = False

    async def start(self) -> bool:
        self.started = True
        return True

    async def levels(self) -> list[float]:
        return [0.0, 0.5, 1.0] + [0.0] * (self.bars - 3)

    async def stop(self) -> None:
        self.stopped = True


class ManyArtists(FakeClient):
    """More similar artists than the screen shows at once."""

    def __init__(self, total: int = 25) -> None:
        super().__init__()
        self.total = total

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
        return SimilarResponse.model_validate(
            {
                "similarartists": {
                    "artist": [
                        {"name": f"Artist {n:02d}", "match": "0.5"} for n in range(self.total)
                    ],
                    "@attr": {"artist": artist},
                }
            }
        )


async def test_the_explanation_says_when_nothing_is_shared():
    class Unrelated(FakeClient):
        async def top_tags(self, artist: str) -> list[str]:
            return ["polka"] if artist == "Burial" else ["drum and bass"]

    app = make_app(FakePlayer(), artist="Burial", client=Unrelated())
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert text_of(app.screen.query_one("#why", Static)) == "no shared tags"


async def test_a_long_similar_list_scrolls_without_hiding_the_title():
    class Many(FakeClient):
        async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
            return SimilarResponse.model_validate(
                {
                    "similarartists": {
                        "artist": [{"name": f"Artist {i:02d}", "match": "0.5"} for i in range(30)],
                        "@attr": {"artist": artist},
                    }
                }
            )

    app = make_app(FakePlayer(), artist="Burial", client=Many())
    async with app.run_test(size=(80, 20)) as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        view = screen.query_one("#artists", ListView)
        title = screen.query_one("#title", Static)

        # opening the list takes focus without scrolling the heading away
        assert title.region.y >= 0
        assert view.size.height < 30  # the list scrolls itself rather than growing

        for _ in range(20):
            await pilot.press("down")
        await pilot.pause()
        assert view.scroll_offset.y > 0  # the view followed the selection
        assert title.region.y >= 0


async def test_the_prompt_lights_its_border_for_the_keyboard():
    """Focus is the app's accent signal, so the first control should show it."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#artist", Input)
        assert prompt.has_focus
        lit = prompt.styles.border.top[1]

        app.query_one("#recent", ListView).focus()
        await pilot.pause()
        quiet = prompt.styles.border.top[1]

        assert (lit.a, quiet.a) == (1.0, 0.5)  # the same accent, at full and half


async def test_space_toggles_pause():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        assert str(detail.query_one("#toggle", Button).label).strip() == "Pause"
        await pilot.press("space")
        await app.workers.wait_for_complete()
        assert player.paused is True
        assert str(detail.query_one("#toggle", Button).label).strip() == "Start"


async def test_arrow_keys_seek_fifteen_seconds():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.press("right")
        await app.workers.wait_for_complete()
        await pilot.press("left")
        await app.workers.wait_for_complete()
        assert player.seeks == [15, -15]


async def test_shift_arrows_seek_thirty_and_change_volume():
    """Shift makes the arrows the coarse control: half-minute seeks and volume."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.press("shift+right")
        await app.workers.wait_for_complete()
        await pilot.press("shift+left")
        await app.workers.wait_for_complete()
        assert player.seeks == [30, -30]

        await pilot.press("shift+up")
        await app.workers.wait_for_complete()
        assert player.volume == 105.0
        await pilot.press("shift+down")
        await app.workers.wait_for_complete()
        assert player.volume == 100.0


async def test_brackets_change_volume_and_it_is_shown():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        assert "vol ▇ 100%" in text_of(detail.query_one("#bar-info", Static))
        await pilot.press("]")
        await app.workers.wait_for_complete()
        assert player.volume == 105.0
        assert "105%" in text_of(detail.query_one("#bar-info", Static))
        await pilot.press("[")
        await app.workers.wait_for_complete()
        assert player.volume == 100.0


async def test_quit_stops_playback():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.press("q")
        await pilot.pause()
    assert player.stop_calls >= 1


async def test_polling_does_not_advance_while_switching_tracks(monkeypatch):
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)  # index 0 is playing
        gate = asyncio.Event()
        real_play = player.play

        async def slow_play(artist: str, track: str):
            await gate.wait()  # stand in for the resolve taking a second
            return await real_play(artist, track)

        monkeypatch.setattr(player, "play", slow_play)
        worker = asyncio.create_task(detail._step(1))
        await asyncio.sleep(0.01)
        await detail.refresh_bar()  # what the 0.5s interval does meanwhile
        gate.set()
        await worker
        assert player.played == [("Burial", "Black Sun"), ("Burial", "Sine")]


async def test_choosing_another_track_while_one_resolves_is_not_dropped(monkeypatch):
    """The owner's report: Enter, then Enter on another song, and it waits.

    The second press used to be ignored outright while the first was resolving —
    `_switching` short-circuited the selection — so nothing happened at all until
    the first track finished, which reads exactly like waiting for its resolve.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        gate = asyncio.Event()
        real_play = player.play
        started: list[str] = []

        async def slow_first(artist: str, track: str):
            started.append(track)
            if track == "Black Sun":
                await gate.wait()  # stand in for the first track still resolving
            return await real_play(artist, track)

        monkeypatch.setattr(player, "play", slow_first)
        tracks = detail.query_one("#tracks", ListView)
        tracks.index = 0
        await pilot.press("enter")  # the first track starts resolving
        await pilot.pause()
        assert started == ["Black Sun"]

        tracks.index = 1  # the user moves to another song
        await pilot.press("enter")  # and presses Enter on it
        await pilot.pause()
        assert started == ["Black Sun", "Sine"], (
            "the second press was dropped while the first track was resolving"
        )
        gate.set()
        await app.workers.wait_for_complete()


async def test_a_track_that_loads_after_the_user_scrolled_away_leaves_the_cursor_alone(
    monkeypatch,
):
    """The owner's second report: the highlight jumped back to the playing song.

    Choosing the row is what put the cursor there, so a play finishing later must
    not move it. Auto-advance still follows the playing track: that is the TUI's
    own move rather than the user's.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        gate = asyncio.Event()
        real_play = player.play

        async def slow_play(artist: str, track: str):
            await gate.wait()  # the resolve the user scrolled away during
            return await real_play(artist, track)

        monkeypatch.setattr(player, "play", slow_play)
        tracks = detail.query_one("#tracks", ListView)
        tracks.index = 0
        await pilot.press("enter")
        await pilot.pause()

        tracks.index = 1  # the user scrolls to another song
        gate.set()  # ... and the track they left loads
        await app.workers.wait_for_complete()
        assert tracks.index == 1, "the cursor jumped back to the track that loaded"


async def test_repeat_keeps_looping_the_last_track():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("n")  # move to the last track
        await app.workers.wait_for_complete()
        await pilot.press("r")
        await app.workers.wait_for_complete()

        for _ in range(2):
            player.finish()
            await detail.refresh_bar()
        assert player.played == [
            ("Burial", "Black Sun"),
            ("Burial", "Sine"),
            ("Burial", "Sine"),
            ("Burial", "Sine"),
        ]


async def test_the_loaded_track_is_marked_playing_then_paused():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        assert marker_texts(detail) == [MARK, ""]

        await pilot.press("space")
        await app.workers.wait_for_complete()
        assert marker_texts(detail) == [MARK, ""]

        await pilot.press("s")  # stopping clears the marker again
        await app.workers.wait_for_complete()
        assert marker_texts(detail) == ["", ""]


async def test_a_finished_track_auto_advances():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        player.finish()  # mpv reached the end of the track
        await detail.refresh_bar()
        assert player.played == [("Burial", "Black Sun"), ("Burial", "Sine")]


async def test_stop_during_a_resolve_stops_the_track_that_arrives():
    class SlowPlayer(FakePlayer):
        async def play(self, artist: str, track: str):
            await asyncio.sleep(0.25)
            return await super().play(artist, track)

    player = SlowPlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("enter")
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert player.active is False  # the play landed, and the stop won
        assert text_of(detail.query_one("#now-playing", Static)) == "Nothing playing"

        await pilot.press("enter")  # and the stale stop does not kill the next play
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert player.active is True
        assert text_of(detail.query_one("#now-playing", Static)) == "Black Sun"


async def test_stop_does_not_auto_advance():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("s")
        await app.workers.wait_for_complete()
        player.active = False
        await detail.refresh_bar()
        assert len(player.played) == 1


async def test_volume_changes_are_persisted():
    saved: list[int] = []
    app = LfmApp(FakeClient(), FakePlayer(), TuiOptions(artist="Burial", save_volume=saved.append))
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.press("]")
        await app.workers.wait_for_complete()
        assert saved == [105]


async def test_playing_starts_the_visualizer_and_idle_stops_it():
    made: list[FakeSpectrum] = []

    def factory(bars: int) -> FakeSpectrum:
        spectrum = FakeSpectrum(bars)
        made.append(spectrum)
        return spectrum

    player = FakePlayer()
    app = make_app(player, artist="Burial", spectrum=factory)
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        assert made and made[0].started is True

        await detail._tick_spectrum()
        assert text_of(detail.query_one("#spectrum", Static)).startswith("▁")

        player.active = False  # playback ended
        await detail.refresh_bar()
        assert made[0].stopped is True
        assert text_of(detail.query_one("#spectrum", Static)) == ""


async def test_the_visualizer_returns_with_the_page():
    made: list[FakeSpectrum] = []

    def factory(bars: int) -> FakeSpectrum:
        spectrum = FakeSpectrum(bars)
        made.append(spectrum)
        return spectrum

    player = FakePlayer()
    app = make_app(player, artist="Burial", spectrum=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        assert made and made[0].started is True

        await pilot.press("b")  # back to the landing; the track keeps playing
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.active is True

        app.query_one("#artist", Input).value = "Burial"
        await pilot.press("enter")  # the same page again
        await app.workers.wait_for_complete()
        await pilot.pause(0.3)
        assert isinstance(app.screen, ArtistDetail)
        assert len(made) >= 2 and made[-1].started is True
        await app.screen._tick_spectrum()
        assert text_of(app.screen.query_one("#spectrum", Static)).startswith("▁")


async def test_quitting_with_the_visualizer_running_is_clean():
    made: list[FakeSpectrum] = []

    def factory(bars: int) -> FakeSpectrum:
        spectrum = FakeSpectrum(bars)
        made.append(spectrum)
        return spectrum

    app = make_app(FakePlayer(), artist="Burial", spectrum=factory)
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.press("q")  # unmounts the screen while cava is still running
        await pilot.pause()
    assert made[0].stopped is True


async def test_visualizer_is_optional():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        assert text_of(detail.query_one("#spectrum", Static)) == ""


async def test_missing_cava_is_not_retried_for_every_track():
    created: list[FakeSpectrum] = []

    class NoCava(FakeSpectrum):
        async def start(self) -> bool:
            return False

    def factory(bars: int) -> FakeSpectrum:
        spectrum = NoCava(bars)
        created.append(spectrum)
        return spectrum

    app = make_app(FakePlayer(), artist="Burial", spectrum=factory)
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.press("enter")  # play again
        await app.workers.wait_for_complete()
        assert len(created) == 1


async def test_a_volume_change_is_handed_to_the_saver():
    saved: list[int] = []
    player = FakePlayer()
    app = LfmApp(FakeClient(), player, TuiOptions(artist="Burial", save_volume=saved.append))
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("]")
        await app.workers.wait_for_complete()
        await pilot.press("[")
        await app.workers.wait_for_complete()
    assert saved == [105, 100]


async def test_a_volume_that_cannot_be_saved_says_so(monkeypatch):
    said: list[str] = []
    # the volume actions live on the app now, so the warning is the app's too
    monkeypatch.setattr(LfmApp, "notify", lambda self, message, **kwargs: said.append(message))
    player = FakePlayer()
    app = LfmApp(
        FakeClient(),
        player,
        TuiOptions(artist="Burial", save_volume=lambda level: "cannot write /ro/config.toml"),
    )
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("]")
        await app.workers.wait_for_complete()
        # the level still applies; only the promise to remember it is broken
        assert player.volume == 105.0
        assert "105%" in text_of(detail.query_one("#bar-info", Static))
    assert said == ["volume will not be remembered: cannot write /ro/config.toml"]


def test_remember_volume_reports_a_saver_that_fails():
    class Broken:
        save_volume = staticmethod(lambda level: (_ for _ in ()).throw(OSError("read-only")))

    class Quiet:
        save_volume = staticmethod(lambda level: None)

    assert remember_volume(Broken(), 90) == "read-only"
    assert remember_volume(Quiet(), 90) is None
    assert remember_volume(FakePlayer(), 90) is None  # no saver wired up at all


async def test_the_shared_tags_line_sits_at_the_bottom_of_the_screen():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        why = screen.query_one("#why", Static)
        assert why.region.y >= 0
        assert why.region.y + why.region.height <= screen.size.height  # nothing below it now


async def test_deep_cuts_look_each_artist_up_once():
    client = CountedClient(COUNTS)
    app = make_app(FakePlayer(), artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_similar(app, pilot)
        for _ in range(3):  # on, deeper, off
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
        await pilot.press("d")  # and on again: the counts are kept
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert client.looked_up.count("Burial") == 1  # handed over by the artist screen
    assert sorted(client.looked_up.count(name) for name in COUNTS) == [1, 1, 1, 1]


async def test_escape_closes_the_shortcuts_too():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("f1")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is detail


async def test_there_is_no_footer_anywhere():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        assert not detail.query("Footer")  # the keys live in the `h` sheet
        await pilot.press("f1")
        await pilot.pause()
        assert not app.screen.query("Footer")


async def test_play_and_pause_are_centred_in_the_button():
    """Equal five-cell labels in a nine-cell button: two blanks either side of each."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        toggle = detail.query_one("#toggle", Button)
        pauses = toggle.render_line(0).text
        await pilot.press("space")
        await app.workers.wait_for_complete()
        assert player.paused is True
        starts = toggle.render_line(0).text

    for line, word in ((pauses, "Pause"), (starts, "Start")):
        left = line.index(word)
        right = len(line) - (left + len(word))
        assert left == right == 2  # centred, not merely aligned


async def test_the_play_button_sits_between_the_seek_buttons():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        ids = [button.id for button in detail.query(Button)]
    assert ids[:3] == ["seek-back", "toggle", "seek-forward"]


async def test_c_copies_the_artists_link():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        assert "last.fm/music/Burial" in app.clipboard


async def test_c_on_the_source_screen_copies_the_video_link():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)
        await pilot.press("y")
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert app.clipboard == "https://youtu.be/aaa"


async def test_playback_keys_work_from_every_screen():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)  # Black Sun, playing
        await pilot.press("l")  # the similar screen: it has a bar too now
        await app.workers.wait_for_complete()
        await pilot.pause()
        similar = app.screen
        assert isinstance(similar, SimilarScreen)
        assert text_of(similar.query_one("#now-playing", Static)) == "Black Sun"

        await pilot.press("space")  # and the keys are here too
        await pilot.pause()
        assert player.paused
        await pilot.press("right")
        await pilot.pause()
        assert player.seeks[-1] == 15
        await pilot.press("]")
        await pilot.pause()
        assert player.volume == 105.0
        await pilot.press("plus")
        await pilot.pause()
        assert player.volume == 110.0  # the alias does the same thing
        await pilot.press("minus")
        await pilot.pause()
        assert player.volume == 105.0


async def test_mute_and_back_again():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("m")
        await pilot.pause()
        assert player.volume == 0.0
        assert bar_info(detail).endswith("muted")  # not a broken `vol 0%`
        await pilot.press("m")
        await pilot.pause()
        assert player.volume == 100.0
        assert "vol ▇ 100%" in bar_info(detail)


async def test_a_digit_seeks_to_that_tenth_of_the_track():
    player = FakePlayer()  # 100s long, at 0:00
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("5")
        await pilot.pause()
        assert player.seeks[-1] == 50  # half of 100s, from the start
        assert player.position == 50.0


async def test_the_bars_buttons_work_from_another_screen():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("l")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#toggle")  # the app handles presses, wherever they are
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.paused


async def test_the_wordmark_needs_a_wide_terminal_and_follows_the_theme():
    app = make_app(FakePlayer())
    async with app.run_test(size=(70, 24)) as pilot:
        await pilot.pause()
        assert not app.screen.query_one("#wordmark", Static).display
        assert app.screen.query_one("#home-title", Static).display

    app = make_app(FakePlayer())
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        wordmark = app.screen.query_one("#wordmark", Static)
        assert wordmark.display
        content = wordmark.content
        assert isinstance(content, Text)
        rows = str(content).splitlines()
        assert len(rows) == len(WORDMARK_ROWS)  # one row of art per row
        assert rows[0].startswith("   ▄▀▄")
        assert rows[-1].startswith("▀▄▄▀▀▄▄▀")
        assert rows[5].startswith("█ ▓▓▓▓▓")  # the art's own `▓` body is drawn
        assert "▒" in "".join(rows)  # and its own shadow band below it
        assert len(content.spans) == 1  # one colour: the glyphs carry the depth
        shaded = {str(span.style) for span in content.spans}

        app.screen.query_one("#recent", ListView).focus()  # so T is not typed
        await pilot.press("T")  # and the theme re-shades it
        await pilot.pause()
        content = wordmark.content
        assert isinstance(content, Text)
        assert {str(span.style) for span in content.spans} != shaded


async def test_v_turns_the_visualizer_off_for_good_and_back_on():
    made: list[FakeSpectrum] = []

    def factory(bars: int) -> FakeSpectrum:
        spectrum = FakeSpectrum(bars)
        made.append(spectrum)
        return spectrum

    player = FakePlayer()
    app = make_app(player, artist="Burial", spectrum=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)  # playing, so one is running
        assert len(made) == 1 and not made[0].stopped

        await pilot.press("v")
        await pilot.pause()
        assert made[0].stopped
        assert text_of(detail.query_one("#spectrum", Static)) == ""

        await pilot.press("n")  # the next track keeps quiet: off meant off
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(made) == 1

        await pilot.press("v")  # and back on again
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(made) == 2 and not made[1].stopped


async def test_the_similar_screen_reveals_more_of_what_it_already_has():
    app = LfmApp(ManyArtists(25), FakePlayer(), TuiOptions(artist="Burial", limit=5))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("l")  # from the artist to the similar list
        await app.workers.wait_for_complete()
        await pilot.pause()
        similar = app.screen
        assert isinstance(similar, SimilarScreen)
        view = similar.query_one("#artists", ListView)
        rows = [str(r.query(Label)[-1].content) for r in view.children]
        assert len(rows) == 6 and rows[-1] == "↓  show 10 more"
        assert text_of(similar.query_one("#meta", Static)) == "5 of 25 artists"

        for _ in range(5):  # down to the reveal row
            await pilot.press("down")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(view.children) == 16  # ten more, plus another row to reveal
        assert text_of(similar.query_one("#meta", Static)) == "15 of 25 artists"

        await similar._show_more()  # at the end there is none left
        await pilot.pause()
        assert len(view.children) == 25
        assert all("more" not in str(r.query(Label)[-1].content) for r in view.children)
        assert text_of(similar.query_one("#meta", Static)) == "25 artists"


async def test_settled_waits_for_a_colour_transition_to_finish(monkeypatch):
    """The wait follows the animation instead of guessing how long it takes.

    The transition is widened to a second here, so a fixed pause lands part-way
    through it every time — which is what a loaded machine did to #124, only now
    it is deterministic rather than a coin flip.
    """
    monkeypatch.setattr(LfmApp, "CSS", LfmApp.CSS.replace("150ms", "1s"))
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        label = detail.query_one("#tags-label", Static)
        grey = label.styles.color

        detail.query_one("#tags", ListView).focus()
        await pilot.pause(0.05)  # part-way through a one-second transition
        assert label.styles.color != grey  # mid-interpolation, right now

        await settled(pilot)
        arrived = label.styles.color
        assert arrived != grey
        await pilot.pause(1.5)  # long enough that nothing can still be moving
        assert label.styles.color == arrived  # so settled() really did wait it out


async def test_T_walks_the_three_themes_and_wraps():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        assert app.theme == "lfm-obsidian"  # warm ink by default
        assert {"lfm-obsidian", "lfm-cream"} <= set(app.available_themes)

        seen = [app.theme]
        for _ in range(3):  # ink, paper, the black one, and round again
            await pilot.press("T")
            await pilot.pause()
            seen.append(app.theme)
        assert seen == ["lfm-obsidian", "lfm-cream", THEMES["midnight"], "lfm-obsidian"]


async def test_the_configured_theme_is_the_one_it_starts_in():
    app = LfmApp(FakeClient(), FakePlayer(), TuiOptions(artist="Burial", theme="cream"))
    async with app.run_test(size=(100, 30)) as pilot:
        assert app.theme == "lfm-cream"
        await pilot.press("T")  # and the key still takes it on from there
        await pilot.pause()
        assert app.theme == THEMES["midnight"]


async def test_the_black_theme_is_textuals_own_dark_one():
    """It is Textual's dark palette, with our name on it and a few variables changed.

    It used to be Textual's own theme object, which is what the name asserted. The
    copy exists so we can set variables at all: a registered theme that reuses a
    built-in's name cannot add one, because the stylesheet still compiles against the
    built-in. Every field below is Textual's, unchanged, and the variables are Textual's
    except the wordmark and the four entries its palette gets wrong here.
    """
    _builtin = BUILTIN_THEMES["textual-dark"]
    for field in (
        "primary",
        "secondary",
        "accent",
        "foreground",
        "background",
        "surface",
        "panel",
        "boost",
        "success",
        "warning",
        "error",
        "dark",  # Theme.dark, not a theme name
    ):
        assert getattr(LFM_MIDNIGHT, field) == getattr(_builtin, field), field
    assert LFM_MIDNIGHT is not None  # the constant is optional only for a Textual without it
    assert LFM_MIDNIGHT.variables == {
        **_builtin.variables,
        "logo": "#ffffff",
        # the entries of Textual's palette that are wrong for this app
        "block-cursor-background": "#4d3718",
        "block-cursor-foreground": "#e0e0e0",
        "warning": "#ffd166",
        "error": "#ff6b6b",
    }

    app = LfmApp(FakeClient(), FakePlayer(), TuiOptions(artist="Burial", theme="midnight"))
    async with app.run_test(size=(100, 30)):
        assert app.theme == THEMES["midnight"]


async def test_an_unknown_theme_name_falls_back_to_the_dark_one():
    app = LfmApp(FakeClient(), FakePlayer(), TuiOptions(artist="Burial", theme="neon"))
    async with app.run_test(size=(100, 30)):
        assert app.theme == "lfm-obsidian"


async def test_the_wordmark_is_not_clipped_and_has_air_under_it():
    """The art, plus a row of padding above and below: a fixed height clipped
    its last row, which is invisible until someone looks."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        wordmark = app.screen.query_one("#wordmark", Static)
        assert wordmark.display
        art = str(wordmark.content).splitlines()
        assert len(art) == len(WORDMARK_ROWS)
        assert wordmark.size.height == len(art)  # every row of the art is shown
        assert wordmark.outer_size.height == len(art) + 2  # a row of air either side


async def test_choosing_a_prefetched_row_plays_the_resolved_url():
    player = FakePlayer()
    player.streams = {"https://youtu.be/aaa": "https://stream.example/audio"}
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played_urls[-1] == ("Archangel", "https://stream.example/audio")


async def test_the_spectrum_holds_the_row_it_draws_into(monkeypatch):
    """The tick runs 30 times a second, so it must not resolve its row each time."""
    player = FakePlayer()
    made: list[FakeSpectrum] = []

    def factory(bars: int) -> FakeSpectrum:
        spectrum = FakeSpectrum(bars)
        made.append(spectrum)
        return spectrum

    app = make_app(player, artist="Burial", spectrum=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        widget = detail.query_one("#spectrum", Static)
        assert detail._spectrum_widget is widget  # a handle, taken once

        looked_up: list[str] = []
        original = ArtistDetail.query_one

        def counting(self, selector, *args, **kwargs):
            looked_up.append(str(selector))
            return original(self, selector, *args, **kwargs)

        monkeypatch.setattr(ArtistDetail, "query_one", counting)
        await detail._tick_spectrum()
        assert "#spectrum" not in looked_up  # and not looked up again on every tick
        assert text_of(widget).startswith("▁")  # still drawn


async def test_the_logo_is_white_on_the_black_theme():
    """The accent is what the app points with; a logo wearing it reads as a link."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        app.theme = THEMES["midnight"]
        await pilot.pause()
        styles = " ".join(str(span.style) for span in app.welcome_text().spans)
    assert "#ffffff" in styles


async def test_the_wordmark_is_drawn_once_and_never_repainted():
    """The mark is still: one flat span, and no frame redraws it.

    It glistened until the animation was removed. `welcome_text` is the still frame a
    resize or a theme switch draws, so nothing should be feeding it a clock.
    """
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        text = app.welcome_text()
        assert len(text.spans) == 1  # one colour, no per-cell lift
        assert str(text) == "\n".join(WORDMARK_ROWS)
        wordmark = app.query_one("#wordmark", Static)
        painted = wordmark.content
        for _ in range(4):  # several frames' worth of the app's own clock
            await pilot.pause()
        assert wordmark.content is painted  # and nothing has repainted it


async def test_the_logo_keeps_the_accent_on_our_own_themes():
    """The wordmark wears the accent, so it follows the theme when the accent moves."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        styles = " ".join(str(span.style) for span in app.welcome_text().spans)
    assert "#7aa2d6" in styles  # the dark theme's accent, which is its blue
    assert "#ffffff" not in styles


async def test_the_wordmark_follows_a_resize_both_ways(isolated_env):
    """`on_resize` used to pass the stale size, so it only ever hid on a fresh page."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        wordmark = app.screen.query_one("#wordmark")
        assert wordmark.display
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        assert not wordmark.display  # narrower than WORDMARK_MIN_WIDTH
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert wordmark.display


async def test_the_bar_gives_up_its_seek_buttons_when_there_is_no_room(isolated_env):
    """At 46 columns the time was clipped to `0:00 / 0` and the progress line vanished."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(46, 20)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app._tick_bars()
        detail = app.screen
        assert not detail.query_one("#seek-back", Button).display
        assert not detail.query_one("#stop", Button).display
        assert text_of(detail.query_one("#playing", Static)) == "0:00 / 0:00"  # not cut

    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app._tick_bars()
        assert app.screen.query_one("#seek-back", Button).display


async def test_the_volume_meter_shows_the_height_a_low_level_is_measured_against():
    """The glyph alone shrinks from `▁` to `█`: nothing beside it says how tall it is."""
    player = FakePlayer()
    player.volume = 20.0
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)
        await app._tick_bars()
        content = app.screen.query_one("#bar-info", Static).content
        assert isinstance(content, Text)
        assert "vol" in content.plain
        assert volume_glyph(20.0) in content.plain  # the level, not a full block
        assert any(span.style and "on " in str(span.style) for span in content.spans)


async def test_a_play_that_reaches_half_its_length_is_queued_once():
    scrobbling = FakeScrobbling()
    app = make_app(FakePlayer(), scrobbling=scrobbling)
    async with app.run_test() as pilot:
        app.start_play("Burial", "Archangel", "Untrue")
        app.count_play(PlayerStatus(playing=True, position=30.0, duration=239.0))
        assert scrobbling.queued == []  # not yet: nowhere near half

        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        assert [(p.artist, p.track, p.album) for p in scrobbling.queued] == [
            ("Burial", "Archangel", "Untrue")
        ]
        # The bar repaints a few times a second: the rest of the track adds nothing.
        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        assert len(scrobbling.queued) == 1

        await pilot.pause()  # the send runs off the bar's tick
        assert scrobbling.drains == 1


async def test_starting_a_track_says_what_is_now_playing():
    scrobbling = FakeScrobbling()
    app = make_app(FakePlayer(), scrobbling=scrobbling)
    async with app.run_test() as pilot:
        app.start_play("Burial", "Archangel", "Untrue")
        await pilot.pause()
        assert scrobbling.playing == [("Burial", "Archangel")]


async def test_a_second_track_is_not_credited_with_the_first_ones_listening():
    """The session follows the track: position readings after a new track starts
    must not count towards the one that was playing before it."""
    scrobbling = FakeScrobbling()
    app = make_app(FakePlayer(), scrobbling=scrobbling)
    async with app.run_test():
        app.start_play("Burial", "Archangel")
        app.start_play("Burial", "Near Dark")  # the first never reached half
        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        assert [p.track for p in scrobbling.queued] == ["Near Dark"]


async def test_a_track_played_by_title_is_not_counted():
    """The source picker plays a YouTube title, which is not metadata: the spec
    says as much, so the count stops there rather than guessing from it."""
    scrobbling = FakeScrobbling()
    app = make_app(FakePlayer(), scrobbling=scrobbling)
    async with app.run_test():
        app.start_play("Burial", "Archangel")
        app.forget_play()  # what choosing a source does
        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        assert scrobbling.queued == []


async def test_nothing_at_all_is_counted_when_scrobbling_is_not_set_up():
    app = make_app(FakePlayer())  # the default: no write path handed to the app
    async with app.run_test():
        app.start_play("Burial", "Archangel")
        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        assert app._session is None  # nothing is watching, so nothing is queued


async def test_a_failed_send_is_reported_without_stopping_playback(monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(LfmApp, "notify", lambda self, message, **kwargs: said.append(message))
    scrobbling = FakeScrobbling(Drain(error="last.fm is offline", waiting=1))
    app = make_app(FakePlayer(), scrobbling=scrobbling)
    async with app.run_test() as pilot:
        app.start_play("Burial", "Archangel")
        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        await pilot.pause()
        assert scrobbling.drains == 1
        assert len(said) == 1 and "offline" in said[0]  # said once, and it says why
        assert app.is_running  # a refused scrobble is not the app's problem


async def test_a_scrobble_that_worked_is_not_announced(monkeypatch):
    """Nothing on screen for a play that made it: it is on the profile, and there
    is nothing to do about it. A toast per track is chrome for something the user
    already knows they played."""
    said: list[str] = []
    monkeypatch.setattr(LfmApp, "notify", lambda self, message, **kwargs: said.append(message))
    scrobbling = FakeScrobbling()  # accepted, as usual
    app = make_app(FakePlayer(), scrobbling=scrobbling)
    async with app.run_test() as pilot:
        app.start_play("Burial", "Archangel")
        app.count_play(PlayerStatus(playing=True, position=239.0, duration=239.0))
        await pilot.pause()
        assert scrobbling.drains == 1  # it did send
        assert said == []  # and said nothing about it


async def test_pausing_marks_the_row_in_a_different_colour():
    """The shape says which state it is; the colour has to say it too.

    Both states used to be `$success` green, so a paused row read as "still fine"
    and the only difference was one small glyph.
    """
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        playing = detail.query_one(".status-playing", Label).styles.color
        await pilot.press("space")
        await app.workers.wait_for_complete()
        paused = detail.query_one(".status-paused", Label).styles.color
        assert paused != playing


def test_both_markers_are_the_same_one_cell_glyph():
    """The states share a glyph, so the cell it sits in is the whole layout risk.

    `●` is East-Asian Ambiguous, which is survivable because the marker's cell is
    fixed: a terminal drawing it two cells wide fills that cell rather than shifting
    the row. A glyph with an emoji presentation would not be survivable at all -- it
    renders in its own colour and the theme's never appears, which is what `⏸` did.
    """
    assert cell_len(MARK) == 1


async def test_the_seek_bar_is_not_the_same_colour_in_obsidian_and_midnight():
    """The two share an accent, so the bar used to be identical in both.

    Obsidian names a `seek` colour of its own — the blue it already carries — and the bar
    prefers it. Midnight keeps the accent: it is Textual's palette and did not choose one.
    """

    async def colour_in(theme: str) -> str:
        app = make_app(FakePlayer(), artist="Burial")
        async with app.run_test() as pilot:
            app.theme = theme
            await pilot.pause()
            return app.progress_colour()

    obsidian = await colour_in(THEMES["obsidian"])
    midnight = await colour_in(THEMES["midnight"])
    assert obsidian.lower() == "#7aa2d6"
    assert obsidian != midnight


async def test_the_equalizer_strip_is_shaded_from_the_themes_own_colour():
    """One voice colour, not a traffic light.

    The ramp is success -> accent -> error. That was coherent while every theme's accent was
    orange, and became a green-to-red strip running through a blue theme. A theme naming a
    `spectrum` colour gets the strip shaded from that colour instead.
    """
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        app.theme = THEMES["obsidian"]
        await pilot.pause()
        quiet, middle, loud = app.spectrum_palette()
        assert middle.lower() == "#7aa2d6"  # the theme's blue, unmixed, in the middle
        assert len({quiet.lower(), middle.lower(), loud.lower()}) == 3  # three shades
        assert all(colour.startswith("#") for colour in (quiet, middle, loud))

        app.theme = THEMES["cream"]  # names no spectrum: keeps the old ramp
        await pilot.pause()
        assert app.spectrum_palette() != (quiet, middle, loud)
