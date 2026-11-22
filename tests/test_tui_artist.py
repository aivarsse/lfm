"""The artist page: its card, tracks, tags, albums, radio and playback."""

import asyncio

from rich.cells import cell_len
from textual import events
from textual.color import Color
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import (
    Input,
    Label,
    ListView,
    Static,
    Tab,
    Tabs,
)
from tuihelpers import (
    AskedPlayer,
    FakeClient,
    FakePlayer,
    bar_info,
    make_app,
    marker_texts,
    opened_detail,
    played_first_track,
    settled,
    text_of,
)

from lfm.api.client import LastfmError
from lfm.api.models import (
    Album,
    AlbumInfo,
    ArtistInfo,
    SimilarResponse,
    TagArtist,
    Track,
)
from lfm.playback.player import PlayerStatus, PlayResult
from lfm.ui.artist import (
    MARK,
    ArtistDetail,
)
from lfm.ui.screens import (
    SimilarScreen,
    SourceScreen,
    TagScreen,
)

TRACKS = [Track(name="Black Sun", playcount=5000), Track(name="Sine", playcount=4000)]


class ManyTracks(FakeClient):
    """Twenty tracks and thirty tags: taller than any sane terminal."""

    async def top_tracks(self, artist: str) -> list[Track]:
        return [Track(name=f"Track {i:02d}", playcount=1000 - i) for i in range(20)]

    async def top_tags(self, artist: str) -> list[str]:
        return [f"tag{i:02d}" for i in range(30)]


def row_is_visible(view, content, row) -> bool:
    """A row counts as visible only if it is inside both its list and the page."""
    top = max(view.region.y, content.region.y)
    bottom = min(view.region.y + view.region.height, content.region.y + content.region.height)
    return top <= row.region.y < bottom


class Bare(FakeClient):
    """An artist Last.fm knows almost nothing about."""

    async def info(self, artist: str) -> ArtistInfo:
        return ArtistInfo.model_validate({"name": artist, "stats": {"listeners": "17"}})

    async def top_tags(self, artist: str) -> list[str]:
        return []

    async def top_tracks(self, artist: str) -> list[Track]:
        return []


def active_tab(detail) -> str:
    """The id of the tab the track window is showing."""
    return detail.query_one("#tracks-tabs", Tabs).active


class SparseClient(FakeClient):
    """The real world: track.getSimilar is empty for most songs."""

    def __init__(self) -> None:
        super().__init__()
        self.top_calls: list[str] = []

    async def similar_tracks(self, artist: str, track: str, limit: int = 30):
        return []

    async def top_tracks(self, artist: str) -> list[Track]:
        self.top_calls.append(artist)
        if artist == "Burial":
            return list(TRACKS)
        return [Track(name="Neighbour Song", playcount=10)]


async def opened_album(app, pilot) -> ArtistDetail:
    """Tab to Top albums, Enter on the first one: the album's tab, loaded."""
    detail = await opened_detail(app, pilot)
    await pilot.press("tab")
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()
    return detail


class GatedPlayer(FakePlayer):
    """Stops answering while its status gate is closed."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.gate.set()

    async def status(self) -> PlayerStatus:
        await self.gate.wait()
        return await super().status()


class GatedAlbumClient(FakeClient):
    """Counts that will not answer until the test says so."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def album_info(self, artist: str, album: str) -> AlbumInfo:
        await self.gate.wait()
        return await super().album_info(artist, album)


async def test_home_screen_asks_for_an_artist_then_shows_its_details():
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        artist_input = app.query_one("#artist", Input)
        assert artist_input.has_focus
        artist_input.value = "Burial"
        await pilot.press("enter")
        detail = await opened_detail(app, pilot)
        assert text_of(detail.query_one("#title", Static)) == "Burial"
        assert text_of(detail.query_one("#meta", Static)) == "1,000 listeners · 2,000 plays"
        tag_rows = [
            str(item.query_one(Label).content)
            for item in detail.query_one("#tags", ListView).children
        ]
        assert tag_rows == ["dubstep", "uk garage"]
        assert "is a producer." in text_of(detail.query_one("#bio", Static))
        assert len(detail.query_one("#tracks", ListView).children) == 2


async def test_empty_input_is_ignored():
    app = make_app(FakePlayer())
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ArtistDetail)


async def test_artist_argument_opens_its_details_directly():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        assert text_of(detail.query_one("#title", Static)) == "Burial"


async def test_corrected_name_replaces_the_typed_one():
    class CorrectingClient(FakeClient):
        async def info(self, artist: str) -> ArtistInfo:
            return (await super().info(artist)).model_copy(update={"name": "Burial"})

    app = make_app(FakePlayer(), artist="burrial", client=CorrectingClient())
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        assert text_of(detail.query_one("#title", Static)) == "Burial (corrected)"


async def test_b_returns_to_the_prompt():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("b")
        await pilot.pause()
        assert not isinstance(app.screen, ArtistDetail)
        assert app.query_one("#artist", Input).has_focus


async def test_l_opens_similar_artists_and_enter_drills_into_one():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, SimilarScreen)
        assert len(app.screen.query_one("#artists", ListView).children) == 2
        # the loading placeholder must be replaced by the real count
        assert text_of(app.screen.query_one("#meta", Static)) == "2 artists"

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ArtistDetail)
        assert text_of(app.screen.query_one("#title", Static)) == "Kode9"


async def test_a_list_without_focus_shows_no_highlighted_row():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        tag_row = detail.query_one("#tags ListItem")
        assert tag_row.styles.background.a == 0  # tracks have focus, so tags stay plain

        await pilot.press("t")
        await pilot.pause()
        assert tag_row.styles.background.a > 0  # now the tags are the active list


async def test_the_title_hugs_the_artist_name():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        title = detail.query_one("#title", Static)
        # the name plus one cell of padding either side, not a full-width band
        assert title.outer_size.width == len("Burial") + 2


async def test_the_most_played_list_holds_twenty(monkeypatch):
    class ManyTracks(FakeClient):
        async def top_tracks(self, artist: str) -> list[Track]:
            return [Track(name=f"Track {i}", playcount=i) for i in range(30)]

    app = make_app(FakePlayer(), artist="Burial", client=ManyTracks())
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        assert len(detail.query_one("#tracks", ListView).children) == 20


async def test_a_successful_play_says_nothing(monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(
        ArtistDetail, "notify", lambda self, message, **kwargs: said.append(message)
    )
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        assert said == []  # the marker and the bar are the feedback


async def test_a_failed_play_still_reports(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        ArtistDetail,
        "notify",
        lambda self, message, **kwargs: calls.append({"message": message, **kwargs}),
    )
    app = make_app(FakePlayer(raise_on_play=True), artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        assert [call["message"] for call in calls] == ["no player"]
        # track names can contain brackets, so markup parsing stays off
        assert calls[0]["markup"] is False


async def test_a_bad_match_points_at_the_picker(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        ArtistDetail,
        "notify",
        lambda self, message, **kwargs: calls.append({"message": message, **kwargs}),
    )

    class NoResolve(FakePlayer):
        async def play(self, artist: str, track: str) -> PlayResult:
            return PlayResult(False, f"could not find {track}: no track")

    app = make_app(NoResolve(), artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        assert len(calls) == 1
        assert "y to pick a source" in calls[0]["message"]
        assert calls[0]["markup"] is False


async def test_stop_button_stops_playback():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        await pilot.click("#stop")
        await app.workers.wait_for_complete()
        assert player.stop_calls >= 1
        assert player.active is False


async def test_tab_switches_the_track_window_and_t_focuses_tags():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        tags = detail.query_one("#tags", ListView)
        assert tracks.has_focus
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) == "albums"  # Tab switches tabs now
        await pilot.press("t")  # the tags list keeps its own key
        await pilot.pause()
        assert tags.has_focus


async def test_t_focuses_the_tag_list():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("t")
        await pilot.pause()
        assert app.screen.query_one("#tags", ListView).has_focus


async def test_choosing_a_tag_opens_its_top_artists():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TagScreen)
        assert text_of(app.screen.query_one("#title", Static)) == "Tag: dubstep"
        assert text_of(app.screen.query_one("#meta", Static)) == "2 artists"
        rows = app.screen.query_one("#artists", ListView).children
        assert [str(item.query_one(Label).content).strip() for item in rows] == [
            "Kode9",  # the row used to lead with a per-tag count, which the API no longer sends
            "Zomby",
        ]

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ArtistDetail)
        assert text_of(app.screen.query_one("#title", Static)) == "Kode9"


async def test_broken_player_does_not_crash_the_app():
    app = make_app(FakePlayer(raise_on_play=True), artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        assert isinstance(app.screen, ArtistDetail)


async def test_next_before_anything_plays_starts_at_the_top():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("n")
        await app.workers.wait_for_complete()
        assert player.played == [("Burial", "Black Sun")]


async def test_the_end_of_the_queue_stops_instead_of_looping():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("n")
        await app.workers.wait_for_complete()
        player.finish()  # the last track played through
        await detail.refresh_bar()
        assert len(player.played) == 2  # nothing after it to play


async def test_a_track_that_dies_does_not_advance_to_the_next(monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(
        ArtistDetail, "notify", lambda self, message, **kwargs: said.append(message)
    )
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await played_first_track(app, pilot)
        player.active = False  # mpv died: no clean finish
        await detail.refresh_bar()

        assert player.played == [("Burial", "Black Sun")]  # no skipping on
        assert marker_texts(detail) == ["", ""]  # and nothing is marked as loaded
        assert said == ["playback stopped  ·  y to pick a source"]


async def test_terminal_title_follows_the_track(monkeypatch):
    titles: list[str] = []

    # both the artist page and the app itself set it (the app's own stop key)
    def remember(app, text: str) -> None:
        titles.append(text)

    monkeypatch.setattr("lfm.ui.artist.set_terminal_title", remember)
    monkeypatch.setattr("lfm.ui.app.set_terminal_title", remember)
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        assert titles[-1] == "Burial — Black Sun"
        await pilot.press("s")
        await app.workers.wait_for_complete()
        assert titles[-1] == "lfm"


async def test_a_bracketed_artist_name_and_bio_are_shown_whole():
    class Bracketed(FakeClient):
        async def info(self, artist: str) -> ArtistInfo:
            return ArtistInfo.model_validate(
                {
                    "name": "Ali[as]",
                    "bio": {"summary": "Raised on [citation needed] notes."},
                    "stats": {},
                }
            )

    app = make_app(FakePlayer(), artist="Burial", client=Bracketed())
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        assert "Ali[as]" in text_of(detail.query_one("#title", Static))
        assert "[citation needed]" in text_of(detail.query_one("#bio", Static))


async def test_the_similar_screen_says_when_there_is_nothing_similar():
    class NothingSimilar(FakeClient):
        async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
            return SimilarResponse.model_validate({"similarartists": {"artist": []}})

    app = make_app(FakePlayer(), artist="Burial", client=NothingSimilar())
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        assert "No similar artists found for Burial" in text_of(
            app.screen.query_one("#meta", Static)
        )


async def test_the_similar_screen_shows_a_corrected_seed_name():
    class Corrected(FakeClient):
        async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
            payload = await super().similar(artist, limit)
            assert payload.similarartists.attr is not None
            payload.similarartists.attr.artist = "Burial (corrected)"
            return payload

    app = make_app(FakePlayer(), artist="Burial", client=Corrected())
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # the correction, and how much of the list is on screen
        assert (
            text_of(app.screen.query_one("#meta", Static)) == "→ Burial (corrected)  ·  2 artists"
        )


async def test_the_tag_screen_says_when_a_tag_has_no_artists():
    class EmptyTags(FakeClient):
        async def tag_top_artists(self, tag: str, limit: int = 30) -> list[TagArtist]:
            return []

    app = make_app(FakePlayer(), artist="Burial", client=EmptyTags())
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("t")
        await pilot.press("enter")
        await pilot.pause()
        assert "No artists found for #dubstep" in text_of(app.screen.query_one("#meta", Static))


async def test_a_long_track_list_scrolls_inside_its_column():
    app = make_app(FakePlayer(), artist="Burial", client=ManyTracks())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        content = detail.query_one("#content", VerticalScroll)

        for _ in range(19):
            await pilot.press("down")
        await pilot.pause()

        assert tracks.index == 19
        assert tracks.scroll_offset.y > 0  # the list moved, like the similar one
        assert row_is_visible(tracks, content, tracks.children[tracks.index])
        assert content.scroll_offset.y == 0  # the page did not
        assert detail.query_one("#title", Static).region.y >= 0  # so the name is still there


async def test_the_wheel_at_the_end_of_the_songs_does_not_move_the_page():
    """Overscroll belongs to the list: scrolling the songs must not carry the page."""

    class LongBio(ManyTracks):
        async def info(self, artist: str) -> ArtistInfo:
            return ArtistInfo.model_validate(
                {
                    "name": artist,
                    "stats": {"listeners": "1000", "playcount": "2000"},
                    "bio": {"summary": "A long story. " * 80},
                }
            )

    app = make_app(FakePlayer(), artist="Burial", client=LongBio())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        content = detail.query_one("#content", VerticalScroll)
        assert content.max_scroll_y > 0  # the page can give, so this test means something
        x, y = tracks.region.x + 2, tracks.region.y + 2

        # The list still takes the wheel itself, not only its overscroll.
        app.screen._forward_event(
            events.MouseScrollDown(None, x, y, 0, 1, 0, False, False, False, x, y)
        )
        await pilot.pause()
        assert tracks.scroll_y > 0  # the list moved
        assert content.scroll_y == 0  # and the page did not

        tracks.scroll_end(animate=False)
        await pilot.pause()
        for _ in range(3):
            app.screen._forward_event(
                events.MouseScrollDown(None, x, y, 0, 1, 0, False, False, False, x, y)
            )
            await pilot.pause()
        assert content.scroll_y == 0  # at the end, the wheel stayed in the list

        tracks.scroll_home(animate=False)  # back to the top
        await pilot.pause()
        app.screen._forward_event(
            events.MouseScrollUp(None, x, y, 0, -1, 0, False, False, False, x, y)
        )
        await pilot.pause()
        assert tracks.scroll_y == 0  # nothing above the first row
        assert content.scroll_y == 0  # and the page did not take that wheel either

        bio = detail.query_one("#bio", Static)
        for _ in range(2):
            app.screen._forward_event(
                events.MouseScrollDown(
                    None, 4, bio.region.y + 1, 0, 1, 0, False, False, False, 4, bio.region.y + 1
                )
            )
            await pilot.pause()
        assert content.scroll_y > 0  # off the lists, the page still answers the wheel


async def test_both_columns_show_ten_rows_before_scrolling():
    app = make_app(FakePlayer(), artist="Burial", client=ManyTracks())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        tags = detail.query_one("#tags", ListView)
        assert tracks.size.height == 10  # ten tracks, the rest scrolls
        assert tags.size.height == 10  # the tags window matches it
        assert tracks.virtual_size.height > tracks.size.height  # and it does scroll


async def test_a_row_stays_visible_when_the_columns_cannot_fit():
    # 12 rows is too short for the columns, so the page has to give as well
    app = make_app(FakePlayer(), artist="Burial", client=ManyTracks())
    async with app.run_test(size=(80, 12)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        content = detail.query_one("#content", VerticalScroll)

        for _ in range(19):
            await pilot.press("down")
            await pilot.pause()
            index = tracks.index
            assert index is not None
            assert row_is_visible(tracks, content, tracks.children[index])


async def test_a_short_list_does_not_scroll():
    app = make_app(FakePlayer(), artist="Burial")  # this fake has two tracks
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)

        await pilot.press("down")
        await pilot.pause()

        assert tracks.virtual_size.height <= tracks.size.height
        assert tracks.scroll_offset.y == 0
        assert detail.query_one("#content", VerticalScroll).scroll_offset.y == 0


async def test_a_bio_never_touches_the_player_bar():
    # the fake's bio is short, so the page fits and the gap is the styling's
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        bio = detail.query_one("#bio", Static)
        bar = detail.query_one("#player-bar")
        assert bar.region.y - (bio.region.y + bio.region.height) >= 1


async def test_scrollbars_take_a_single_cell():
    app = make_app(FakePlayer(), artist="Burial", client=ManyTracks())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        # Textual's default vertical scrollbar is two cells wide, and the style is
        # not inherited: it has to reach each scrolling widget
        assert tracks.styles.scrollbar_size_vertical == 1
        assert detail.query_one("#content").styles.scrollbar_size_vertical == 1


async def test_empty_sections_are_hidden_rather_than_left_blank():
    app = make_app(FakePlayer(), artist="Burial", client=Bare())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        assert not detail.query_one("#about-label", Static).display
        assert not detail.query_one("#tags-label", Static).display
        assert not detail.query_one("#tags", ListView).display
        # the tab bar stays: tabs are navigation, not content


async def test_the_tui_shortens_big_numbers_and_keeps_small_ones_exact():
    class Played(FakeClient):
        async def info(self, artist: str) -> ArtistInfo:
            return ArtistInfo.model_validate(
                {"name": artist, "stats": {"listeners": "6683700", "playcount": "1140897124"}}
            )

        async def top_tracks(self, artist: str) -> list[Track]:
            return [
                Track(name="Hey Jude", playcount=573_048),
                Track(name="Huge", playcount=13_850_808),
            ]

    app = make_app(FakePlayer(), artist="The Beatles", client=Played())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        assert text_of(detail.query_one("#meta", Static)) == "6.7M listeners · 1.1B plays"
        rows = [
            # the row's own label: the first is the ▶/⏸ marker
            str(row.query(Label)[-1].content)
            for row in detail.query_one("#tracks", ListView).children
        ]
    assert "573,048" in rows[0]  # under a million: every digit
    assert "13.9M" in rows[1]


async def test_w_starts_a_radio_from_the_highlighted_track():
    player, client = FakePlayer(), FakeClient()
    app = make_app(player, artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()
        view = detail.query_one("#tracks", ListView)
        assert active_tab(detail) == "radio"
        assert [str(row.query(Label)[-1].content) for row in view.children] == [
            "Jamie Woon — Night Air"  # the seed it was asked about is dropped
        ]
        # and it plays the radio's artist, not the one whose page we are on
        assert player.played[-1] == ("Jamie Woon", "Night Air")
        assert client.radio_seeds == [("Burial", "Black Sun")]


async def test_the_radio_drifts_on_when_its_queue_runs_dry():
    player, client = FakePlayer(), FakeClient()
    app = make_app(player, artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()

        # the only queued track finished: the next hop comes from *it*, not the seed
        player.finished = True
        player.active = False
        await detail.refresh_bar()
        await app.workers.wait_for_complete()
        await pilot.pause()

        rows = detail.query_one("#tracks", ListView).children
        assert len(rows) == 2
        assert "Four Tet — Angel Echoes" in str(rows[1].query(Label)[-1].content)
        assert client.radio_seeds[-1] == ("Jamie Woon", "Night Air")
        assert player.played[-1] == ("Four Tet", "Angel Echoes")


async def test_the_radio_drifts_artistwise_when_lastfm_has_no_track_similar():
    """No track-to-track data: more from the artist, then its closest neighbour."""
    player, client = FakePlayer(), SparseClient()
    app = make_app(player, artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)  # Black Sun
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()
        view = detail.query_one("#tracks", ListView)
        assert [str(row.query(Label)[-1].content) for row in view.children] == [
            "Burial — Sine"  # the artist's own other track, at no call cost
        ]
        assert player.played[-1] == ("Burial", "Sine")

        # Sine finishes: own tracks are spent, so the nearest neighbour is drawn on
        player.finished = True
        player.active = False
        await detail.refresh_bar()
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = view.children
        assert len(rows) == 2
        assert "Kode9 — Neighbour Song" in str(rows[1].query(Label)[-1].content)
        assert player.played[-1] == ("Kode9", "Neighbour Song")
        # one neighbour per dry-out: Zomby is not fetched until the next one
        assert client.top_calls.count("Kode9") == 1
        assert "Zomby" not in client.top_calls


async def test_w_toggles_the_radio_off():
    player, client = FakePlayer(), FakeClient()
    app = make_app(player, artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Jamie Woon", "Night Air")

        await pilot.press("w")  # off again
        await app.workers.wait_for_complete()
        await pilot.pause()
        view = detail.query_one("#tracks", ListView)
        assert active_tab(detail) == "tracks"
        assert len(view.children) == len(TRACKS)  # the artist's own list is back
        assert any("Sine" in str(r.query(Label)[-1].content) for r in view.children)
        assert detail.radio is None
        # the playing song was left alone, and nothing is queued after it
        assert player.played[-1] == ("Jamie Woon", "Night Air")
        assert detail._queue_index is None

        player.finished = True  # it ends: radio off means no successor
        player.active = False
        await detail.refresh_bar()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Jamie Woon", "Night Air")

        await pilot.press("w")  # and it cycles: on again from the highlighted row
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) == "radio"


async def test_tab_shows_albums_and_enter_opens_one():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        view = detail.query_one("#tracks", ListView)
        assert active_tab(detail) == "albums"
        assert [str(r.query(Label)[-1].content) for r in view.children] == [
            " 95,000  Untrue  ·  3 tracks",  # the count arrives after the list
            " 40,000  Kindred  ·  3 tracks",
        ]

        await pilot.press("enter")  # an album opens; it does not play
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) == "album"
        rows = [str(r.query(Label)[-1].content) for r in view.children]
        assert rows[0] == " 1  Untitled  ·  1:15"  # rank, name, then Last.fm's duration
        assert rows[1] == " 2  Archangel  ·  3:59"
        # The name sits two cells from its rank, the same gap the other lists leave
        # between their count and theirs; the duration trails, out of the way.
        assert rows[0].index("Untitled") == 4
        assert rows[2] == "    Near Dark"  # no duration, no dot, the name alone
        meta = text_of(detail.query_one("#album-meta", Static))
        assert "Released" not in meta  # album.getInfo no longer sends a release date
        assert "1.2M listeners" in meta
        assert "5:14" in meta  # 75 + 239 seconds, the unknown one left out


async def test_album_tracks_play_in_album_order_and_advance():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_album(app, pilot)
        await pilot.press("enter")  # Untitled, the album's first track
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Burial", "Untitled")

        player.finished = True
        player.active = False
        await detail.refresh_bar()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Burial", "Archangel")  # rank order, not plays


async def test_switching_tabs_keeps_the_album_queue_playing():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_album(app, pilot)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("tab")  # wander off mid-album: tags window, then a tab
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) != "album"

        player.finished = True
        player.active = False
        await detail.refresh_bar()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Burial", "Archangel")  # the queue survives


async def test_opening_another_album_replaces_the_tab_and_ends_its_queue():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_album(app, pilot)  # Untrue
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Burial", "Untitled")

        for _ in range(3):  # album -> tags -> tracks -> Top albums
            await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")  # Kindred
        await app.workers.wait_for_complete()
        await pilot.pause()
        tabs = detail.query_one("#tracks-tabs", Tabs)
        assert len(tabs.query(Tab)) == 3  # tracks, albums, and one album tab
        assert active_tab(detail) == "album"
        assert text_of(detail.query_one("#album-meta", Static)).startswith("1.2M listeners")

        player.finished = True  # Untitled ends: the replaced queue has no successor
        player.active = False
        await detail.refresh_bar()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played[-1] == ("Burial", "Untitled")


async def test_a_long_artist_name_ellipsizes_and_leaves_the_stats_room():
    class LongName(FakeClient):
        async def info(self, artist: str) -> ArtistInfo:
            return ArtistInfo.model_validate(
                {
                    "name": "The Extremely Long Artist Name That Goes On And On And On",
                    "stats": {"listeners": "1000", "playcount": "2000"},
                    "bio": {"summary": ""},
                }
            )

    app = make_app(FakePlayer(), artist="Burial", client=LongName())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.pause()
        title = text_of(detail.query_one("#title", Static))
        assert title.endswith("…")
        assert detail.query_one("#title", Static).region.width <= 62
        assert "1,000 listeners" in text_of(detail.query_one("#meta", Static))


async def test_a_bracketed_album_name_survives_in_its_tab():
    class BracketAlbum(FakeClient):
        async def top_albums(self, artist: str, limit: int = 30) -> list[Album]:
            return [Album(name="Untrue [Live]", playcount=1)]

    app = make_app(FakePlayer(), artist="Burial", client=BracketAlbum())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        tabs = detail.query_one("#tracks-tabs", Tabs)
        assert str(tabs.query(Tab)[-1].label) == "Untrue [Live]"


async def test_a_long_album_name_gets_a_short_tab():
    class LongAlbum(FakeClient):
        async def top_albums(self, artist: str, limit: int = 30) -> list[Album]:
            return [
                Album(
                    name="A Very Long Album Name That Will Not Fit In The Tabs Bar",
                    playcount=1,
                )
            ]

    app = make_app(FakePlayer(), artist="Burial", client=LongAlbum())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")  # Top albums
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # open the album
        await app.workers.wait_for_complete()
        await pilot.pause()
        tabs = detail.query_one("#tracks-tabs", Tabs)
        label = str(tabs.query(Tab)[-1].label)
        assert label.endswith("…")
        assert cell_len(label) <= 24
        assert detail.album_info is not None
        assert detail.album_info.name.startswith("A Very Long Album")  # the full name is kept


async def test_album_counts_skip_albums_lastfm_cannot_answer():
    class HalfBroken(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.counted: list[str] = []

        async def album_info(self, artist: str, album: str) -> AlbumInfo:
            self.counted.append(album)
            if album == "Kindred":
                raise LastfmError("boom")
            return await super().album_info(artist, album)

    client = HalfBroken()
    app = make_app(FakePlayer(), artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = [
            str(r.query(Label)[-1].content) for r in detail.query_one("#tracks", ListView).children
        ]
        assert rows[0].endswith("·  3 tracks")  # Untrue counted
        assert not rows[1].endswith("tracks")  # Kindred failed: no count, no crash
        assert client.counted == ["Untrue", "Kindred"]  # one call per row, no retries


async def test_the_bar_names_the_queue_it_is_playing():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_album(app, pilot)
        await pilot.press("enter")  # Untitled, of three
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "1/3 · Untrue" in bar_info(detail)

        await pilot.press("l")  # the note travels with the app, not the screen
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "1/3 · Untrue" in bar_info(app.screen)


async def test_f_goes_back_to_the_tab_that_is_playing():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("l")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SimilarScreen)

        await pilot.press("f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is detail
        assert active_tab(detail) == "tracks"  # where the queue lives
        assert detail.query_one("#tracks", ListView).index == 0  # and its row


async def test_tab_walks_the_tabs_then_the_tags_window():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tracks = detail.query_one("#tracks", ListView)
        tags = detail.query_one("#tags", ListView)
        assert tracks.has_focus
        assert active_tab(detail) == "tracks"

        await pilot.press("tab")  # the window's other tab
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) == "albums"

        await pilot.press("tab")  # past the last tab: the tags window
        await pilot.pause()
        assert tags.has_focus

        await pilot.press("tab")  # and round to the first tab again
        await pilot.pause()
        assert tracks.has_focus
        assert active_tab(detail) == "tracks"


async def test_shift_tab_walks_the_same_cycle_backwards():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tags = detail.query_one("#tags", ListView)
        await pilot.press("shift+tab")  # from the first tab, back to the last stop
        await pilot.pause()
        assert tags.has_focus

        await pilot.press("shift+tab")  # and on to the last tab
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) == "albums"


async def test_the_active_tab_goes_grey_when_the_keyboard_leaves_the_window():
    """The accent marks the window you are in, but the active tab keeps its shape."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")  # Top albums is the active tab from here on
        await app.workers.wait_for_complete()
        await settled(pilot)
        tabs = detail.query_one("#tracks-tabs", Tabs)

        def colours() -> dict[str, object]:
            return {tab.id: tab.styles.color for tab in tabs.query(Tab)}

        def backgrounds() -> dict[str, object]:
            return {tab.id: tab.styles.background for tab in tabs.query(Tab)}

        assert tabs.has_class("in-focus")
        assert colours()["albums"] != colours()["tracks"]  # the active one stands out
        assert backgrounds()["albums"] != backgrounds()["tracks"]  # and has a shape

        detail.query_one("#tags", ListView).focus()
        await settled(pilot)
        assert not tabs.has_class("in-focus")
        assert colours()["albums"] == colours()["tracks"]  # all grey in the tags window
        assert backgrounds()["albums"] != backgrounds()["tracks"]  # the block stays

        detail.query_one("#tracks", ListView).focus()
        await settled(pilot)
        assert tabs.has_class("in-focus")
        assert colours()["albums"] != colours()["tracks"]


async def test_the_window_lights_in_the_switch_call_itself():
    """A switch out of the tags window must not wait for the focus message to light up."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        tabs = detail.query_one("#tracks-tabs", Tabs)
        tags_label = detail.query_one("#tags-label", Static)
        detail.query_one("#tags", ListView).focus()
        await settled(pilot)
        assert not tabs.has_class("in-focus")

        detail.action_cycle_tab()  # tags -> tracks: lit before `focus()` has landed
        assert tabs.has_class("in-focus")
        assert not tags_label.has_class("in-focus")

        await settled(pilot)
        detail.action_cycle_tab()  # tracks -> albums: still lit
        await settled(pilot)
        detail.action_cycle_tab()  # albums -> tags: dark before `focus()` has landed
        assert not tabs.has_class("in-focus")
        assert tags_label.has_class("in-focus")


async def test_the_title_band_fades_in_on_mount():
    """The name's accent band is added after the first frame, so it can fade in."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.pause(0.2)  # the band has finished fading
        title = detail.query_one("#title", Static)
        assert title.has_class("lit")
        assert title.styles.background.a > 0.2


async def test_the_stats_follow_a_theme_switch():
    """The counts bake in a theme colour, so switching themes must redraw them."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:

        def as_rgb(colour: str) -> str:
            red, green, blue = Color.parse(colour).rgb
            return f"rgb({red},{green},{blue})"

        detail = await opened_detail(app, pilot)
        before_colour = as_rgb(app.theme_colour("foreground", ""))
        before = detail.query_one("#meta", Static).render()
        assert isinstance(before, Content)
        assert any(before_colour in str(span.style) for span in before.spans)

        await pilot.press("T")  # obsidian -> cream
        await pilot.pause()
        after_colour = as_rgb(app.theme_colour("foreground", ""))
        after = detail.query_one("#meta", Static).render()
        assert isinstance(after, Content)
        assert after_colour != before_colour
        assert any(after_colour in str(span.style) for span in after.spans)
        assert not any(before_colour in str(span.style) for span in after.spans)


async def test_the_stats_put_the_counts_in_body_ink():
    """The numbers are the part that changes; the words carry the muted ink."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        meta = detail.query_one("#meta", Static).render()
        assert isinstance(meta, Content)
        styled = [meta.plain[span.start : span.end] for span in meta.spans]
        assert any("1,000" in part for part in styled)
        assert any("2,000" in part for part in styled)
        assert not any("listeners" in part for part in styled)  # the words stay muted


async def test_the_tags_window_fills_its_ten_rows():
    class ManyTags(FakeClient):
        async def top_tags(self, artist: str) -> list[str]:
            return [f"tag {n}" for n in range(14)]

    app = make_app(FakePlayer(), artist="Burial", client=ManyTags())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        assert detail.tags == [f"tag {n}" for n in range(10)]  # ten, like the window
        assert len(detail.query_one("#tags", ListView).children) == 10


async def test_an_empty_window_says_why_instead_of_going_blank():
    class Nothing(FakeClient):
        async def top_tracks(self, artist: str) -> list[Track]:
            return []

        async def top_albums(self, artist: str, limit: int = 30) -> list[Album]:
            return []

    app = make_app(FakePlayer(), artist="Burial", client=Nothing())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        hint = detail.query_one("#empty-hint", Static)
        assert hint.display
        assert text_of(hint) == "No top tracks on Last.fm for this artist"
        assert not detail.query_one("#tracks", ListView).display

        await pilot.press("tab")  # the albums tab, which is empty too
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(hint) == "No albums on Last.fm for this artist"
        assert not detail.query_one("#tracks", ListView).display


async def test_clicking_a_tab_loads_it_and_a_waiting_window_says_so():
    app = make_app(FakePlayer(), artist="Burial", client=FakeClient())
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.click("#albums")  # the pointer path, not the key path
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert active_tab(detail) == "albums"
        assert len(detail.query_one("#tracks", ListView).children) == 2

        # a window whose list is still on its way says that, not "nothing here"
        detail._albums_loaded = False
        detail.albums = []
        await detail._render_queue_rows()
        assert text_of(detail.query_one("#empty-hint", Static)) == "Looking for albums…"


async def test_the_tags_heading_lights_up_only_with_the_keyboard_in_it():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        label = detail.query_one("#tags-label", Static)
        assert not label.has_class("in-focus")
        grey = label.styles.color

        detail.query_one("#tags", ListView).focus()
        await settled(pilot)
        assert label.has_class("in-focus")
        assert label.styles.color != grey

        detail.query_one("#tracks", ListView).focus()
        await settled(pilot)
        assert not label.has_class("in-focus")
        assert label.styles.color == grey


async def test_a_radio_row_is_searched_with_its_own_artist():
    """The queue's rows carry their artist, and the picker has to use it."""
    player = AskedPlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("w")  # the radio: rows by other artists
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.asked[-1] == ("Jamie Woon", "Night Air")


async def test_the_picker_prefetches_the_row_under_the_cursor():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("y")  # the picker opens on its first row
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.prefetched == ["https://youtu.be/aaa"]

        await pilot.press("down")  # and follows the cursor
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.prefetched[-1] == "https://youtu.be/bbb"


async def test_a_row_whose_resolution_failed_plays_the_watch_url():
    """The prefetch is best effort: no stream URL means the page plays as before."""
    player = FakePlayer()  # nothing in streams, so prefetch returns None
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.played_urls[-1] == ("Archangel", "https://youtu.be/aaa")


async def test_picking_a_source_marks_the_row_it_played():
    """`y` then Enter played a source, and the row kept no marker and no queue index.

    Only `_play_track` recorded what was playing; a picked source went straight to mpv,
    so the page never learned which row it was for.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # the first source plays and the picker closes
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is detail  # back on the page it was asked from
        assert detail._queue_index == 0  # the row that was picked, not None
        assert app.playing_index == 0
        assert app.playing_artist == "Burial"
        assert str(detail._status_labels[0].content) == MARK
        assert marker_texts(detail)[1:] == [""]  # and only that row


async def test_a_source_picked_on_the_album_tab_marks_that_row():
    """The album's own tracklist: `y` on a row that is not the queue's used to pick the wrong track.

    The picker preferred the queue's index to the cursor, and a queue belongs to the tab
    it was queued from, so on the album tab that index named a different track: the
    source was fetched for it, and the marker then landed on it too.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)  # a queue from the tracks tab: row 0
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # open the album: its own three tracks
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")  # the third track, not the playing row
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SourceScreen)
        assert picker.artist == "Burial"  # the source asked for the row under the cursor
        assert picker.track == "Near Dark"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert (detail._queue_id, detail._queue_index) == ("album", 2)
        assert app.playing_album == "Untrue"
        assert marker_texts(detail) == ["", "", MARK]


async def test_the_artist_screen_prefetches_the_row_under_the_cursor():
    """Enter should not wait for yt-dlp when the row has been under the cursor."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)
        for _ in range(3):
            await pilot.pause()
        assert ("Burial", "Black Sun") in player.prefetched_tracks

        await pilot.press("down")
        for _ in range(3):
            await pilot.pause()
        assert ("Burial", "Sine") in player.prefetched_tracks


async def test_reopening_an_artist_remembers_what_is_playing():
    """A new page is built every visit; the marker has to come back with it."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)  # Black Sun, row 0
        assert detail._queue_index == 0

        await pilot.press("b")  # home
        await pilot.pause()
        app.screen.query_one("#artist", Input).value = "Burial"  # the prompt is empty
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        again = app.screen
        assert isinstance(again, ArtistDetail)
        assert again is not detail  # a new page, as den saw
        assert again._queue_index == 0  # but it knows what is playing
        marker = again._status_labels[0]
        assert str(marker.content) == MARK


async def test_switching_tabs_never_empties_the_list(monkeypatch):
    """The window used to be cleared first, so a frame showed it empty before the rows came.

    Tabbing therefore looked like the list taking a moment to fill. The swap now mounts
    the new rows before it takes the old ones out, in one batch.
    """
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)
        await app.workers.wait_for_complete()
        await pilot.pause()
        held: list[int] = []  # what the window held as each new list went in
        mount = ListView.mount

        def watched(self, *children):
            held.append(len(self.children))
            return mount(self, *children)

        monkeypatch.setattr(ListView, "mount", watched)
        await pilot.press("tab")  # the top albums
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert held  # the switch did draw a new list
        assert all(count > 0 for count in held)  # and never over an empty window


async def test_the_playing_marker_is_drawn_with_the_rows(monkeypatch):
    """It used to wait for the next bar tick, which read as the ▶ loading slowly."""
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        drawn: list[bool] = []
        inherited = ArtistDetail._update_markers

        def counting(self, paused: bool) -> None:
            drawn.append(paused)
            inherited(self, paused)

        monkeypatch.setattr(ArtistDetail, "_update_markers", counting)
        await detail._render_queue_rows()  # as a tab change or a fresh page does
        assert drawn  # the rows drew the marker themselves, not the clock
        assert str(detail._status_labels[0].content) == MARK


async def test_a_busy_player_does_not_hold_up_the_list():
    """The paused state waits on mpv; the rows must not wait with it."""
    player = GatedPlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        view = detail.query_one("#tracks", ListView)
        rows = len(view.children)
        assert rows

        player.gate.clear()  # the player is busy and will not answer
        worker = asyncio.create_task(detail._render_queue_rows())
        for _ in range(5):
            await asyncio.sleep(0)  # as far as it can get before the status lands
        assert len(view.children) >= rows  # the list is up regardless, and never emptied
        assert worker.done() is False  # still waiting on the player, as expected

        player.gate.set()  # let it answer
        await worker
        assert str(detail._status_labels[0].content) == MARK


async def test_the_albums_tab_shows_its_rows_before_the_counts(isolated_env):
    """The rows used to wait for every count, and could be wiped by the tab's own render."""
    client = GatedAlbumClient()
    app = make_app(FakePlayer(), client=client, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("tab")  # the albums tab
        view = app.screen.query_one("#tracks", ListView)
        for _ in range(20):  # the counts are still gated, the rows should still arrive
            await pilot.pause()
            if view.children:
                break
        assert len(view.children) == 2  # both albums, before a single count came back
        assert "track" not in " ".join(str(text_of(w)) for w in view.query(Static))

        client.gate.set()  # let the counts answer
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "3 tracks" in " ".join(str(text_of(w)) for w in view.query(Static))


async def test_a_play_that_was_replaced_is_not_reported_as_an_error(monkeypatch):
    """Choosing a second track supersedes the first, and the first is not a
    failure the screen should announce: the user had already moved on."""
    said: list[str] = []
    monkeypatch.setattr(
        ArtistDetail, "notify", lambda self, message, **kwargs: said.append(message)
    )

    class Overtaken(FakePlayer):
        async def play(self, artist: str, track: str) -> PlayResult:
            self.played.append((artist, track))
            return PlayResult(False, "another track was chosen", superseded=True)

    app = make_app(Overtaken(), artist="Burial")
    async with app.run_test() as pilot:
        await played_first_track(app, pilot)
        assert said == []  # nothing to say about a request the user abandoned


async def test_reopening_the_playing_album_still_marks_the_track():
    """The owner's steps: play from top albums, leave, come back to that album.

    `_restore_playing` put the queue back only for the top-tracks tab -- it hard-coded
    `_queue_id = "tracks"` and bounds-checked the index against `self.tracks` -- so an album
    queue was never restored and its rows were never marked.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")  # into top albums
        await app.workers.wait_for_complete()
        await pilot.press("enter")  # into the first album
        await app.workers.wait_for_complete()
        await pilot.press("enter")  # play its first track
        await app.workers.wait_for_complete()
        assert player.played, "the album's track never played"
        markers = marker_texts(detail)
        assert len(markers) > 1, "the album's tracks are not on screen"
        assert sum(1 for m in markers if m) == 1, "nothing marked where the track was played"

        # Back to home, then in again: the artist page is built fresh on re-entry, which is
        # what lost the marker. Same app -- the playing state lives on it, not on the page.
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        assert app.screen is not detail, "escape did not leave the artist page"
        prompt = app.query_one("#artist", Input)
        prompt.value = "Burial"
        prompt.focus()
        await pilot.press("enter")  # the prompt opens a fresh artist page
        await app.workers.wait_for_complete()
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")  # into top albums
        await app.workers.wait_for_complete()
        await pilot.press("enter")  # and into the album that is playing
        await app.workers.wait_for_complete()
        markers = marker_texts(detail)
        assert len(markers) > 1, "the album's tracks are not on screen"
        assert sum(1 for m in markers if m) == 1, "the play indicator vanished on reopening"


async def test_a_row_of_another_album_never_wears_the_marker():
    """The index is a row number, so the album has to match too.

    With an album queue playing and a *different* album open, that row number would
    otherwise be marked. `_queue_is_on_screen` is what stops it.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert detail._tab == "album"
        assert detail._queue_is_on_screen(), "the playing album's own list should be marked"

        playing = app.playing_album
        assert detail.album_info is not None
        detail.album_info.name = playing + " (another record)"
        assert not detail._queue_is_on_screen(), "another album's same row was marked"

        # and an album queue is not marked on the top-tracks tab either
        detail._tab = "tracks"
        assert not detail._queue_is_on_screen()


async def test_the_albums_tab_marks_the_playing_album():
    """The list is where you choose an album, so it answers "which one is playing".

    Its rows always carried a marker; nothing ever lit it, because the condition only
    compared a row index against the playing queue's own list.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")  # top albums
        await app.workers.wait_for_complete()
        await pilot.press("enter")  # into the first album
        await app.workers.wait_for_complete()
        await pilot.press("enter")  # play one of its tracks
        await app.workers.wait_for_complete()
        assert app.playing_album, "no album was recorded for the queue"

        for _ in range(4):  # back to the albums list
            if detail._tab == "albums":
                break
            await pilot.press("tab")
            await app.workers.wait_for_complete()
        assert detail._tab == "albums", "did not get back to the albums list"

        markers = marker_texts(detail)
        assert len(markers) == len(detail.albums) > 0, "not one marker per album row"
        marked = [detail.albums[i].name for i, m in enumerate(markers) if m]
        assert marked == [app.playing_album], f"marked {marked}, expected just the playing album"


async def test_the_playing_album_marker_ignores_other_artists_and_bad_rows():
    """The two guards behind the albums-tab marker, which no rendered path reaches.

    A different artist's album of the same name must not light up, and a row index that
    does not exist must not match: the caller only ever passes real rows of the open
    artist, so these are asserted directly rather than driven through the screen.
    """
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("tab")
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.playing_album, "no album was recorded for the queue"

        app.playing_artist = "Someone Else"  # the same album name, another artist's
        assert not detail._is_the_playing_album(0)
        app.playing_artist = "Burial"
        assert detail._is_the_playing_album(0)

        assert not detail._is_the_playing_album(len(detail.albums)), "a row that is not there"
