"""The Last.fm sequences both interfaces need, in one place.

Two kinds live here. One is a single call with a rule attached: ask
`artist.getSimilar` for its whole list and rank locally, rather than asking for the
page the user wanted, because the ranking, the deep cuts and the blending all need
the long list to choose from. The other is two or three calls that have to agree
with each other, like the artist page's card, tags and tracks.

The third is a fan-out — one call becomes twenty — and it belongs here for the same
reason: which twenty, and how far the fan-out is allowed to spread, is a rule, not a
detail of whoever is asking.

Neither is about the CLI or the TUI. Written in both, it is tuned in one — and the
tuning is what gets forgotten.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .models import ArtistInfo, SimilarArtist, SimilarResponse, Track
from .protocols import Reader
from .rank import WALK_DECAY, WALK_FAN_OUT, WalkedArtist, walk_level

# artist.getSimilar's maximum, in one cached call. See the note above: this is the
# whole reason the wrapper exists.
SIMILAR_LIMIT = 100


async def similar_wide(client: Reader, artist: str, limit: int) -> SimilarResponse:
    """Every similar artist one call can return, whatever page was asked for."""
    return await client.similar(artist, limit=max(limit, SIMILAR_LIMIT))


async def artist_detail(client: Reader, artist: str) -> tuple[ArtistInfo, list[str], list[Track]]:
    """The artist page's three lookups: the card, its tags, and its top tracks."""
    info, tags, tracks = await asyncio.gather(
        client.info(artist), client.top_tags(artist), client.top_tracks(artist)
    )
    return info, tags, tracks


async def listener_counts(client: Reader, artists: Sequence[SimilarArtist]) -> dict[str, int]:
    """Case-folded name -> listeners, for the candidates a deep-cut run will judge.

    One `artist.getInfo` per candidate, together: the client keeps at most four
    requests in flight and caches the answer for a week, so a repeat is free. A
    name Last.fm cannot answer for is left out rather than guessed at, so the
    caller can still count how many stayed unknown.
    """
    infos = await asyncio.gather(
        *(client.info(artist.name) for artist in artists), return_exceptions=True
    )
    return {
        artist.name.casefold(): info.stats.listeners
        for artist, info in zip(artists, infos, strict=True)
        if not isinstance(info, BaseException)
    }


async def walk(
    client: Reader,
    artist: str,
    *,
    depth: int = 2,
    fan_out: int = WALK_FAN_OUT,
    min_match: float = 0.0,
    decay: float = WALK_DECAY,
) -> list[WalkedArtist]:
    """The artists a hop or two out, each scored by the paths that reach it.

    One cached call per artist followed: the seed's list, then the strongest `fan_out`
    of those, and so on. `depth=2` therefore costs at most `1 + fan_out` calls whatever
    the lists in between are worth, and every artist is asked about once — this is a
    breadth-first search, not a flood. Two is the useful depth, and the CLI's maximum:
    a third hop multiplies the requests again for answers the decay has flattened.

    The seed's own failure is raised, because the caller knows what to say about a name
    Last.fm does not know; a neighbour's is not, since one artist with nothing to say is
    no reason to lose the rest of the walk.
    """
    seed = artist.casefold()
    totals: dict[str, float] = {}
    rows: dict[str, WalkedArtist] = {}
    seen = frozenset({seed})
    frontier: list[tuple[str, float]] = [(artist, 1.0)]
    for hops in range(1, depth + 1):
        responses = await asyncio.gather(
            *(similar_wide(client, name, SIMILAR_LIMIT) for name, _ in frontier),
            return_exceptions=True,
        )
        edges: dict[str, list[SimilarArtist]] = {}
        for (name, _), response in zip(frontier, responses, strict=True):
            if isinstance(response, SimilarResponse):
                edges[name] = list(response.similarartists.artist)
            elif hops == 1:
                raise response  # the seed is the one name this has to complain about
            else:
                edges[name] = []
        step = walk_level(
            frontier,
            edges,
            hops=hops,
            seen=seen,
            decay=decay,
            fan_out=fan_out,
            min_match=min_match,
        )
        seen = step.seen
        for row in step.found:
            key = row.name.casefold()
            totals[key] = totals.get(key, 0.0) + row.score
            rows.setdefault(key, row)  # the closest path to it is the one displayed
        frontier = step.following
        if not frontier:
            break

    ranked = sorted((key for key in rows if key != seed), key=totals.__getitem__, reverse=True)
    return [
        WalkedArtist(name=rows[key].name, score=totals[key], hops=rows[key].hops, url=rows[key].url)
        for key in ranked
    ]
