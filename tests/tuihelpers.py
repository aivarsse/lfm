"""The fakes, fixtures and factories the TUI tests share.

Imported by the test modules in this directory, including two that are not about
the TUI, so nothing here may depend on a test being under way.
"""

from textual.widgets import (
    Label,
    Static,
)

from lfm.api.client import LastfmError
from lfm.api.models import (
    Album,
    AlbumInfo,
    ArtistInfo,
    SearchArtist,
    SimilarResponse,
    SimilarTrack,
    TagArtist,
    Track,
)
from lfm.playback.player import PlayerStatus, PlayResult, YtResult
from lfm.ui.app import (
    LfmApp,
)
from lfm.ui.artist import (
    ArtistDetail,
)
from lfm.ui.options import TuiOptions
from lfm.ui.screens import (
    SimilarScreen,
)

TAGS_BY_ARTIST = {
    "Burial": ["dubstep", "uk garage"],
    "Kode9": ["uk garage", "ambient"],
    "Zomby": ["dubstep", "bass"],
}


class FakeClient:
    """Stands in for LastfmClient: canned responses, no network."""

    def __init__(self, *, failing_info: bool = False) -> None:
        self.failing_info = failing_info
        self.radio_seeds: list[tuple[str, str]] = []

    async def info(self, artist: str) -> ArtistInfo:
        if self.failing_info:
            raise LastfmError("boom")
        return ArtistInfo.model_validate(
            {
                "name": artist,
                "stats": {"listeners": "1000", "playcount": "2000"},
                "bio": {
                    "summary": "<a href='#'>Kode9</a> is a producer. "
                    "<a href='#'>Read more on Last.fm</a>"
                },
            }
        )

    async def top_tags(self, artist: str) -> list[str]:
        return list(TAGS_BY_ARTIST.get(artist, ["dubstep", "uk garage"]))

    async def top_tracks(self, artist: str) -> list[Track]:
        return list(TRACKS)

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
        return SimilarResponse.model_validate(
            {
                "similarartists": {
                    "artist": [
                        {"name": "Kode9", "match": "1.0"},
                        {"name": "Zomby", "match": "0.87"},
                    ],
                    "@attr": {"artist": artist},
                }
            }
        )

    async def tag_top_artists(self, tag: str, limit: int = 30) -> list[TagArtist]:
        # No count: tag.getTopArtists stopped sending one.
        return [TagArtist(name="Kode9"), TagArtist(name="Zomby")]

    async def search(self, artist: str, limit: int = 5) -> list[str]:
        return ["Burial", "Burl Ives"]

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
        """The hits, with the counts the live API returns.

        `artist.search` ranks by *relevance*, not by listeners, so a synthetic falloff per
        rank (1000, 990, …) is not the shape it sorts into: it made every pair look like a
        tie, which no completion rule with a margin can survive. Live, "Burial" (1,314,054)
        beats "Burl Ives" (318,742) by 4.1x.
        """
        return [
            SearchArtist(name="Burial", listeners=1_314_054),
            SearchArtist(name="Burl Ives", listeners=318_742),
        ]

    async def top_albums(self, artist: str, limit: int = 30) -> list[Album]:
        return [Album(name="Untrue", playcount=95000), Album(name="Kindred", playcount=40000)]

    async def album_info(self, artist: str, album: str) -> AlbumInfo:
        return AlbumInfo.model_validate(
            {
                "name": album,
                "artist": artist,
                "listeners": "1200000",
                "playcount": "24000000",
                "tracks": {
                    "track": [
                        {"@attr": {"rank": "1"}, "name": "Untitled", "duration": "75"},
                        {"@attr": {"rank": "2"}, "name": "Archangel", "duration": "239"},
                        {"name": "Near Dark", "duration": "0"},
                    ]
                },
            }
        )

    async def similar_tracks(self, artist: str, track: str, limit: int = 30) -> list[SimilarTrack]:
        self.radio_seeds.append((artist, track))
        if track == "Night Air":  # one hop further out, so drift can be tested
            return [SimilarTrack(name="Angel Echoes", artist="Four Tet", match=0.6)]
        return [
            SimilarTrack(name="Night Air", artist="Jamie Woon", match=0.87),
            SimilarTrack(name=track, artist=artist, match=1.0),  # the seed itself
        ]


class FakePlayer:
    """Stands in for MpvPlayer: records calls, no processes at all."""

    def __init__(self, *, raise_on_play: bool = False) -> None:
        self.played: list[tuple[str, str]] = []
        self.played_urls: list[tuple[str, str]] = []
        self.prefetched: list[str] = []
        self.prefetched_tracks: list[tuple[str, str]] = []
        self.streams: dict[str, str] = {}  # watch url -> direct audio url
        self.sources = [
            YtResult(
                title="Archangel", url="https://youtu.be/aaa", channel="Hyperdub", duration="3:58"
            ),
            YtResult(
                title="Archangel (rework)",
                url="https://youtu.be/bbb",
                channel="Dan E",
                duration="4:10",
            ),
        ]
        self.current_title = ""
        self.finished = False
        self.seeks: list[int] = []
        self.stop_calls = 0
        self.paused = False
        self.active = False
        self.position = 0.0
        self.duration = 100.0
        self.volume = 100.0
        self.raise_on_play = raise_on_play

    async def play(self, artist: str, track: str) -> PlayResult:
        if self.raise_on_play:
            raise RuntimeError("no player")
        self.played.append((artist, track))
        self.active = True
        self.finished = False
        self.current_title = track
        return PlayResult(True, f"Playing {track}")

    async def play_url(self, title: str, url: str) -> PlayResult:
        self.played_urls.append((title, url))
        self.active = True
        self.finished = False
        self.current_title = title
        return PlayResult(True, f"Playing {title}")

    async def find_sources(self, artist: str, track: str) -> list[YtResult]:
        return list(self.sources)

    async def prefetch(self, url: str) -> str | None:
        self.prefetched.append(url)
        return self.streams.get(url)

    def prefetch_track(self, artist: str, track: str) -> None:
        self.prefetched_tracks.append((artist, track))

    async def toggle_pause(self) -> bool | None:
        if not self.active:
            return None
        self.paused = not self.paused
        return self.paused

    async def seek(self, seconds: int) -> None:
        if self.active:
            self.seeks.append(seconds)
            self.position = max(0.0, self.position + seconds)

    async def change_volume(self, delta: int) -> int:
        self.volume = max(0.0, min(130.0, self.volume + delta))
        return int(self.volume)

    def finish(self) -> None:
        """Stand in for mpv reaching the end of a track."""
        self.active = False
        self.finished = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.active = False
        self.paused = False
        self.finished = False

    async def status(self) -> PlayerStatus:
        current = self.current_title if self.active else ""
        return PlayerStatus(
            playing=self.active,
            paused=self.paused,
            position=self.position,
            duration=self.duration if self.active else 0.0,
            volume=self.volume,
            title=current,
            finished=self.finished,
        )


async def settled(pilot) -> None:
    """Wait for colour transitions to finish before reading a widget's colour.

    `pilot.pause()` waits for the app to be idle, and an animation is not idle
    work: reading partway through a `transition: color` on the tabs or the Tags
    label gets an interpolated colour, which is #94. A fixed pause is a guess
    about the machine — 0.2s was measured as clear of a 150ms transition and
    still lost a loaded suite (#124) — so this waits on the animator, which is
    what drives those transitions, and comes back when they have finished.
    """
    await pilot.pause()  # let the focus change land, which starts the transition
    await pilot.app.animator.wait_for_idle()
    await pilot.pause()


def text_of(widget: Static) -> str:
    return str(widget.content)


def make_app(
    player: FakePlayer,
    artist: str | None = None,
    client=None,
    spectrum=None,
    scrobbling=None,
) -> LfmApp:
    options = TuiOptions(artist=artist, spectrum=spectrum, scrobbling=scrobbling)
    return LfmApp(client or FakeClient(), player, options)


async def opened_detail(app: LfmApp, pilot) -> ArtistDetail:
    await pilot.pause()
    assert isinstance(app.screen, ArtistDetail)
    return app.screen


async def played_first_track(app: LfmApp, pilot) -> ArtistDetail:
    detail = await opened_detail(app, pilot)
    await pilot.press("enter")  # the tracks list is focused on mount
    await app.workers.wait_for_complete()
    return detail


def marker_texts(detail) -> list[str]:
    return [str(marker.content) for marker in detail.query(".status") if isinstance(marker, Label)]


class CountedClient(FakeClient):
    """Similar artists with listener counts, and a record of every lookup made."""

    def __init__(
        self,
        counts: dict[str, int],
        *,
        seed: int = 2_000_000,
        fail_seed: bool = False,
        missing: frozenset[str] = frozenset(),
    ):
        self.counts = counts
        self.seed = seed
        self.fail_seed = fail_seed
        self.missing = missing
        self.looked_up: list[str] = []

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
        return SimilarResponse.model_validate(
            {
                "similarartists": {
                    "artist": [{"name": name, "match": "0.9"} for name in self.counts],
                    "@attr": {"artist": artist},
                }
            }
        )

    async def info(self, artist: str) -> ArtistInfo:
        self.looked_up.append(artist)
        if artist == "Burial":
            if self.fail_seed:
                raise LastfmError("no data")
            listeners = self.seed
        elif artist in self.missing:
            raise LastfmError("no data")
        else:
            listeners = self.counts[artist]
        return ArtistInfo.model_validate({"name": artist, "stats": {"listeners": str(listeners)}})


COUNTS = {"Kode9": 900_000, "Zomby": 180_000, "Darkstar": 40_000, "Tiny": 12_000}


async def opened_similar(app: LfmApp, pilot) -> SimilarScreen:
    await opened_detail(app, pilot)
    await pilot.press("l")
    await pilot.pause()
    assert isinstance(app.screen, SimilarScreen)
    return app.screen


def bar_info(screen) -> str:
    """What the bar says on the right: queue position, repeat, volume."""
    return text_of(screen.query_one("#bar-info", Static))


class AskedPlayer(FakePlayer):
    """Records what the picker asked YouTube for."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[tuple[str, str]] = []

    async def find_sources(self, artist: str, track: str) -> list[YtResult]:
        self.asked.append((artist, track))
        return await super().find_sources(artist, track)


class Beatles(FakeClient):
    """The junk entry that repeats the name it matches — really there, with 625 listeners."""

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
        return hits(("The Beatles", 6_685_905), ("the Beatles, The Beatles", 625))


TRACKS = [Track(name="Black Sun", playcount=5000), Track(name="Sine", playcount=4000)]


def hits(*pairs: tuple[str, int]) -> list[SearchArtist]:
    """A ranked `artist.search` answer: (name, listeners) as the live API returns them."""
    return [SearchArtist(name=name, listeners=listeners) for name, listeners in pairs]
