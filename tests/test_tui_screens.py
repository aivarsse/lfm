"""The screens it pushes: similar artists, a tag, the source picker."""

from textual.widgets import (
    Label,
    ListView,
    Static,
)
from tuihelpers import (
    COUNTS,
    AskedPlayer,
    CountedClient,
    FakeClient,
    FakePlayer,
    make_app,
    opened_detail,
    opened_similar,
    played_first_track,
    text_of,
)

from lfm.api.client import LastfmError
from lfm.api.models import (
    SimilarResponse,
    TagArtist,
)
from lfm.playback.player import PlayerStatus, YtDlpError, YtResult
from lfm.ui.screens import (
    SimilarScreen,
    SourceScreen,
    TagScreen,
)


def row_text(view) -> list[str]:
    return [str(row.query_one(Label).content) for row in view.children]


async def test_the_similar_screen_explains_the_highlighted_artist():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SimilarScreen)
        await app.workers.wait_for_complete()
        # Burial and Kode9 share uk garage
        assert text_of(screen.query_one("#why", Static)) == "shared: uk garage"

        await pilot.press("down")  # Kode9 -> Zomby, who shares dubstep instead
        await app.workers.wait_for_complete()
        assert text_of(screen.query_one("#why", Static)) == "shared: dubstep"


async def test_y_opens_the_source_picker_and_the_choice_is_remembered():
    player = FakePlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("y")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SourceScreen)
        assert text_of(screen.query_one("#meta", Static)) == "2 results"
        assert len(screen.query_one("#sources", ListView).children) == 2

        await pilot.press("down")  # the second candidate
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert player.played_urls == [("Archangel (rework)", "https://youtu.be/bbb")]
        assert not isinstance(app.screen, SourceScreen)  # back on the artist

        from lfm.store.sources import Sources

        chosen = Sources().chosen("Burial", "Black Sun")
        assert chosen is not None and chosen.url == "https://youtu.be/bbb"


async def test_the_picker_marks_the_source_already_chosen():
    from lfm.store.sources import Sources

    Sources().remember("Burial", "Black Sun", title="Archangel", url="https://youtu.be/aaa")
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("y")
        await pilot.pause()
        rows = app.screen.query_one("#sources", ListView).children
        texts = [str(row.query_one(Label).content) for row in rows]
        assert texts[0].startswith("▶")  # the remembered one
        assert texts[1].startswith("  ")


async def test_a_failed_source_search_says_why():
    class FailingSearch(FakePlayer):
        async def find_sources(self, artist: str, track: str) -> list[YtResult]:
            raise YtDlpError("Sign in to confirm you are not a bot")

    app = make_app(FailingSearch(), artist="Burial")
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("y")
        await pilot.pause()
        assert "Sign in to confirm" in text_of(app.screen.query_one("#meta", Static))
        assert "b to go back" in text_of(app.screen.query_one("#meta", Static))
        assert app.screen.query_one("#source-label").display is False
        assert app.screen.query_one("#sources", ListView).display is False


async def test_the_similar_screen_reports_a_failed_lookup():
    class BrokenSimilar(FakeClient):
        async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
            raise LastfmError("the API is unhappy")

    app = make_app(FakePlayer(), artist="Burial", client=BrokenSimilar())
    async with app.run_test() as pilot:
        await opened_detail(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, SimilarScreen)
        assert (
            text_of(app.screen.query_one("#meta", Static))
            == "error: the API is unhappy  ·  b to go back"
        )


async def test_the_tag_screen_reports_a_failed_lookup():
    class BrokenTags(FakeClient):
        async def tag_top_artists(self, tag: str, limit: int = 30) -> list[TagArtist]:
            raise LastfmError("the API is unhappy")

    app = make_app(FakePlayer(), artist="Burial", client=BrokenTags())
    async with app.run_test() as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("t")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TagScreen)
        assert (
            text_of(app.screen.query_one("#meta", Static))
            == "error: the API is unhappy  ·  b to go back"
        )
        assert detail is not app.screen


async def test_d_cycles_how_deep_the_cuts_go():
    app = make_app(FakePlayer(), artist="Burial", client=CountedClient(COUNTS))
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await opened_similar(app, pilot)
        view = screen.query_one("#artists", ListView)
        meta = screen.query_one("#meta", Static)
        assert len(view.children) == 4

        await pilot.press("d")  # under the seed's listeners over ten: 200,000
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(view.children) == 3
        assert not any("Kode9" in text for text in row_text(view))  # 900k is no deep cut
        assert "180,000" in row_text(view)[0]  # the count that qualified it
        assert text_of(meta) == "3 deep cuts under 200,000 listeners"

        await pilot.press("d")  # over thirty: 66,666
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(view.children) == 2
        assert text_of(meta) == "2 deep cuts under 66,666 listeners"

        await pilot.press("d")  # and off again
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(view.children) == 4
        assert text_of(meta) == "4 artists"


async def test_nothing_is_looked_up_before_deep_cuts_is_asked_for():
    client = CountedClient(COUNTS)
    app = make_app(FakePlayer(), artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_similar(app, pilot)
        await pilot.pause()
    # the artist card is looked up on the way in; no *candidate* is
    assert client.looked_up == ["Burial"]


async def test_deep_cuts_says_when_nothing_is_obscure_enough():
    app = make_app(FakePlayer(), artist="Burial", client=CountedClient({"Kode9": 900_000}))
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await opened_similar(app, pilot)
        await pilot.press("d")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(screen.query_one("#artists", ListView).children) == 0
        assert (
            text_of(screen.query_one("#meta", Static))
            == "no deep cuts under 200,000 listeners — press d to widen"
        )


async def test_d_on_the_artist_screen_opens_the_similar_list_digging():
    app = make_app(FakePlayer(), artist="Burial", client=CountedClient(COUNTS))
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        for _ in range(14):  # the tracks list holds the keyboard
            await pilot.press("down")
        await pilot.press("d")
        await pilot.pause()  # let the pushed screen mount and start its worker
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert detail is not app.screen
        assert isinstance(app.screen, SimilarScreen)
        assert "deep cuts under 200,000 listeners" in text_of(app.screen.query_one("#meta", Static))


async def test_deep_cuts_turn_off_when_the_seed_has_no_listener_data():
    app = make_app(FakePlayer(), artist="Burial", client=CountedClient(COUNTS, fail_seed=True))
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await opened_similar(app, pilot)
        await pilot.press("d")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(screen.query_one("#meta", Static)) == (
            "no listener data for Burial — deep cuts off"
        )
        assert len(screen.query_one("#artists", ListView).children) == 4  # back to the whole list


async def test_deep_cuts_says_how_many_it_could_not_judge():
    # Zomby's 180,000 would qualify, but nobody can say so: it is counted, not claimed
    client = CountedClient(COUNTS, missing=frozenset({"Zomby"}))
    app = make_app(FakePlayer(), artist="Burial", client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await opened_similar(app, pilot)
        await pilot.press("d")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert text_of(screen.query_one("#meta", Static)) == (
            "2 deep cuts under 200,000 listeners  ·  1 unknown"
        )
        rows = row_text(screen.query_one("#artists", ListView))
        assert not any("Zomby" in text for text in rows)


async def test_the_picker_searches_for_the_artist_and_names_it():
    """A song title alone finds covers; the artist has to go with it."""
    player = AskedPlayer()
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await played_first_track(app, pilot)
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.asked[-1] == ("Burial", "Black Sun")  # artist and track
        assert text_of(detail.app.screen.query_one("#title", Static)) == (
            "Sources for Burial — Black Sun"
        )


async def test_a_tick_reads_the_player_once_for_every_bar(monkeypatch):
    """Every bar is showing the same moment, and mpv sits at the far end of a socket."""
    player = AskedPlayer()
    seen: list[None] = []
    inherited = AskedPlayer.status

    async def counting(self) -> PlayerStatus:
        seen.append(None)
        return await inherited(self)

    monkeypatch.setattr(AskedPlayer, "status", counting)
    app = make_app(player, artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await played_first_track(app, pilot)
        await pilot.press("y")  # the picker: a second screen that also has a bar
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SourceScreen)  # two bars on the stack
        seen.clear()
        await app._tick_bars()
        assert len(seen) == 1  # one look at the player, not one per bar
