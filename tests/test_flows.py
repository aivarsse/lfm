"""The flows are rules about Last.fm, so the rules are pinned here.

The module exists because the CLI and the TUI used to hold these separately and
drifted; these tests are what they are agreeing on.
"""

from __future__ import annotations

import pytest

from lfm.api.client import LastfmError
from lfm.api.flows import SIMILAR_LIMIT, artist_detail, listener_counts, similar_wide, walk
from lfm.api.models import ArtistInfo, SimilarArtist, SimilarResponse, Track


class Recorder:
    """A client that records what was asked of it, and answers with one of each."""

    def __init__(self, *, refuse: set[str] | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.refuse = refuse or set()

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
        self.calls.append(("similar", artist, limit))
        return SimilarResponse.model_validate(
            {"similarartists": {"artist": [{"name": "Sine", "listeners": "10"}]}}
        )

    async def info(self, artist: str) -> ArtistInfo:
        self.calls.append(("info", artist))
        if artist in self.refuse:
            raise LastfmError("not this one")
        return ArtistInfo.model_validate(
            {"name": artist, "stats": {"listeners": "40", "playcount": "2"}}
        )

    async def top_tags(self, artist: str) -> list[str]:
        self.calls.append(("top_tags", artist))
        return ["dubstep", "ambient"]

    async def top_tracks(self, artist: str) -> list[Track]:
        self.calls.append(("top_tracks", artist))
        return [Track(name="Archangel", playcount=1)]


async def test_similar_wide_asks_for_the_whole_list_not_the_page_wanted():
    """The rule the CLI, the blend and the TUI all depend on: rank the long list."""
    client = Recorder()

    await similar_wide(client, "Burial", 5)

    assert client.calls == [("similar", "Burial", SIMILAR_LIMIT)]


async def test_similar_wide_leaves_a_bigger_request_alone():
    client = Recorder()

    await similar_wide(client, "Burial", SIMILAR_LIMIT + 50)

    assert client.calls == [("similar", "Burial", SIMILAR_LIMIT + 50)]


async def test_artist_detail_fetches_the_three_lookups_together():
    client = Recorder()

    info, tags, tracks = await artist_detail(client, "Burial")

    assert info.name == "Burial"
    assert tags == ["dubstep", "ambient"]
    assert [track.name for track in tracks] == ["Archangel"]
    assert [call[0] for call in client.calls] == ["info", "top_tags", "top_tracks"]


async def test_listener_counts_are_folded_and_leave_out_an_unanswered_name():
    client = Recorder(refuse={"Kode9"})

    counts = await listener_counts(
        client, [SimilarArtist(name="Burial"), SimilarArtist(name="Kode9")]
    )

    assert counts == {"burial": 40}  # the one Last.fm could not answer for is absent


class Graph:
    """A client whose similar answers are a fixed graph of (name, match) edges."""

    def __init__(
        self, edges: dict[str, list[tuple[str, float]]], *, refuse: set[str] | None = None
    ) -> None:
        self.edges = edges
        self.refuse = refuse or set()
        self.calls: list[str] = []

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
        self.calls.append(artist)
        if artist in self.refuse:
            raise LastfmError("not this one")
        return SimilarResponse.model_validate(
            {
                "similarartists": {
                    "artist": [
                        {"name": name, "match": str(match)}
                        for name, match in self.edges.get(artist, [])
                    ]
                }
            }
        )

    # The rest of the Reader protocol, which a walk must not need: if one of these is
    # ever reached, the walk has started enriching itself and the cost has changed.
    async def info(self, artist: str) -> ArtistInfo:
        raise AssertionError("a walk reads similar lists and nothing else")

    async def top_tags(self, artist: str) -> list[str]:
        raise AssertionError("a walk reads similar lists and nothing else")

    async def top_tracks(self, artist: str) -> list[Track]:
        raise AssertionError("a walk reads similar lists and nothing else")


TWO_HOPS = {
    "Burial": [("Kode9", 1.0), ("Zomby", 0.5)],
    "Kode9": [("Digital Mystikz", 1.0), ("Burial", 1.0)],
    "Zomby": [("Digital Mystikz", 0.5)],
}


async def test_a_two_hop_walk_scores_every_path_that_reaches_an_artist():
    client = Graph(TWO_HOPS)

    results = await walk(client, "Burial", depth=2)

    assert [row.name for row in results] == ["Kode9", "Digital Mystikz", "Zomby"]
    scores = {row.name: row.score for row in results}
    assert scores["Kode9"] == 0.5  # a perfect match, halved for being one hop out
    assert scores["Digital Mystikz"] == 0.25 + 0.0625  # reached from Kode9 and from Zomby
    assert [row.hops for row in results] == [1, 2, 1]  # the closest path is the one shown
    assert "Burial" not in scores  # the seed is what was asked for, not an answer
    assert client.calls == ["Burial", "Kode9", "Zomby"]  # each artist asked about once


async def test_a_walk_at_one_hop_is_the_seed_s_own_list():
    client = Graph(TWO_HOPS)

    results = await walk(client, "Burial", depth=1)

    assert client.calls == ["Burial"]
    assert {row.name: row.hops for row in results} == {"Kode9": 1, "Zomby": 1}


async def test_a_second_hop_costs_one_call_per_artist_followed_and_no_more():
    edges = {"Burial": [(f"artist-{i}", 1.0 - i / 100) for i in range(50)]}
    edges.update({f"artist-{i}": [("Elsewhere", 1.0)] for i in range(50)})
    client = Graph(edges)

    await walk(client, "Burial", depth=2, fan_out=20)

    assert len(client.calls) == 1 + 20  # the seed, then the strongest twenty — not fifty


async def test_a_walk_ends_when_there_is_nothing_left_to_ask_about():
    client = Graph({"Burial": [("Kode9", 1.0)], "Kode9": []})

    results = await walk(client, "Burial", depth=2)

    assert [row.name for row in results] == ["Kode9"]
    assert client.calls == ["Burial", "Kode9"]  # the empty answer is not asked again


async def test_a_neighbour_last_fm_cannot_answer_for_does_not_lose_the_walk():
    client = Graph(TWO_HOPS, refuse={"Zomby"})

    results = await walk(client, "Burial", depth=2)

    assert "Kode9" in [row.name for row in results]


async def test_the_seed_is_the_one_failure_a_walk_reports():
    """A neighbour with nothing to say is not worth an error; the name you asked for is."""
    client = Graph(TWO_HOPS, refuse={"Burial"})

    with pytest.raises(LastfmError):
        await walk(client, "Burial", depth=2)
