"""Ranking: plain functions over typed models, no I/O. Easy to test."""

import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .models import SearchArtist, SimilarArtist

# A seed that does not list a candidate contributes this, so an artist known to
# every seed beats one known to a single seed without a missing entry scoring 0.
BLEND_EPSILON = 0.05

# A hop is worth less than the one that reached it, whatever the matches on the way:
# an artist two steps from the seed is a weaker answer than one it named itself.
WALK_DECAY = 0.5
# How many of an artist's neighbours the walk keeps — and so how many of them it
# follows, since every follower is a request. A similar list can be a hundred long,
# and this is what makes a second hop cost twenty-one calls instead of a hundred.
WALK_FAN_OUT = 20


@dataclass(frozen=True)
class DeepCuts:
    """The candidates obscure enough to count, and what the filter could not judge."""

    artists: list[SimilarArtist]
    threshold: int | None = None
    unknown: int = 0


def deep_cuts(
    artists: list[SimilarArtist],
    listeners: dict[str, int],
    *,
    max_listeners: int | None = None,
    seed_listeners: int | None = None,
    ratio: int = 10,
) -> DeepCuts:
    """Keep the candidates obscure enough to be a deep cut, in the order given.

    A candidate qualifies at or below `max_listeners` or — when that is not given —
    below the seed's own count divided by `ratio`, so "much less known than what I
    am listening to" needs no magic number. `listeners` maps a case-folded name to
    its count: collecting that is the caller's job, and a candidate missing from it
    is counted as `unknown` and left out, because guessing either way would be a lie.
    """
    if max_listeners is not None:
        threshold: int | None = max_listeners
    elif seed_listeners:
        threshold = max(1, seed_listeners // ratio)
    else:
        threshold = None

    cuts: list[SimilarArtist] = []
    unknown = 0
    for artist in artists:
        count = listeners.get(artist.name.casefold())
        if count is None:
            unknown += 1
        elif threshold is None or count <= threshold:
            cuts.append(artist)
    return DeepCuts(artists=cuts, threshold=threshold, unknown=unknown)


@dataclass(frozen=True)
class BlendArtist:
    """A blend candidate: its score plus how each seed matched it."""

    name: str
    score: float
    url: str | None = None
    matches: dict[str, float] = field(default_factory=dict)


def rank(
    artists: list[SimilarArtist], *, min_match: float = 0.0, limit: int | None = None
) -> list[SimilarArtist]:
    """Sort by match (desc), drop below min_match, dedupe names case-insensitively, cap at limit."""
    seen: set[str] = set()
    out: list[SimilarArtist] = []
    for artist in sorted(artists, key=lambda a: a.match, reverse=True):
        # Before the append, not after: a cap of zero means zero, and `0` is a
        # legitimate value of an `int | None`. Hypothesis found this one.
        if limit is not None and len(out) >= limit:
            break
        if artist.match < min_match:
            continue
        key = artist.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(artist)
    return out


def blend(
    seed_lists: dict[str, list[SimilarArtist]],
    *,
    epsilon: float = BLEND_EPSILON,
    limit: int | None = None,
    min_score: float = 0.0,
    exclude: set[str] | None = None,
) -> list[BlendArtist]:
    """Score candidates by the geometric mean of their match across the seeds.

    The geometric mean favours artists that are close to *all* the seeds: one
    that scores 0.9 with both beats one that scores 1.0 with one and is absent
    from the other (which contributes `epsilon`). The seeds themselves are left
    out — they are what was asked for, not an answer.
    """
    skipped = {name.casefold() for name in (exclude or set())}
    matches: dict[str, dict[str, float]] = {}
    names: dict[str, str] = {}
    urls: dict[str, str | None] = {}
    for seed, artists in seed_lists.items():
        for artist in artists:
            key = artist.name.casefold()
            if key in skipped:
                continue
            names.setdefault(key, artist.name)
            urls.setdefault(key, artist.url)
            matches.setdefault(key, {})[seed] = artist.match

    scored: list[BlendArtist] = []
    for key, per_seed in matches.items():
        values = [max(per_seed.get(seed, epsilon), epsilon) for seed in seed_lists]
        score = math.exp(sum(math.log(value) for value in values) / len(values))
        if score < min_score:
            continue
        scored.append(BlendArtist(name=names[key], score=score, url=urls[key], matches=per_seed))
    scored.sort(key=lambda a: a.score, reverse=True)
    return scored[:limit] if limit is not None else scored


def shared_tags(seed: list[str], other: list[str], *, limit: int = 3) -> list[str]:
    """Tags both artists carry, in the seed's order, compared case-insensitively."""
    wanted = {tag.casefold() for tag in other}
    return [tag for tag in seed if tag.casefold() in wanted][:limit]


def _fold(name: str) -> str:
    """Compare names the way the API duplicates them: width, case and spacing all vary.

    NFKC collapses the full-width spellings Last.fm is full of ("Ｍｉｃｈ" is "Mich"), and
    collapsing the whitespace catches the padded variants of the same act.
    """
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def suggestions(query: str, matches: list[SearchArtist], limit: int = 5) -> list[str]:
    """Names worth offering for a query Last.fm could not resolve, best first.

    `artist.search` answers a relevance search, not a name lookup: it echoes the query back as
    an artist in its own right, repeats that echo in other widths and spellings, and ranks all
    of it above the acts you probably meant. So drop every name that folds to the query, drop
    names that fold together, and order what is left by listeners — the only column that tells
    a real act from the noise.
    """
    taken = {_fold(query)}
    found: list[SearchArtist] = []
    for match in matches:
        key = _fold(match.name)
        if not key or key in taken:
            continue
        taken.add(key)
        found.append(match)
    found.sort(key=lambda match: match.listeners, reverse=True)
    return [match.name for match in found[:limit]]


@dataclass(frozen=True)
class WalkedArtist:
    """An artist a walk reached: its score, and how many hops out it was found."""

    name: str
    score: float
    hops: int
    url: str | None = None


@dataclass(frozen=True)
class WalkStep:
    """One hop of a walk: what it reached, and who the next hop would ask about."""

    found: list[WalkedArtist]
    following: list[tuple[str, float]]
    seen: frozenset[str]


def walk_level(
    frontier: Sequence[tuple[str, float]],
    edges: Mapping[str, Sequence[SimilarArtist]],
    *,
    hops: int,
    seen: frozenset[str],
    decay: float = WALK_DECAY,
    fan_out: int = WALK_FAN_OUT,
    min_match: float = 0.0,
) -> WalkStep:
    """Follow one hop out from `frontier`, scoring every artist it reaches.

    `frontier` is (name, the score of the path that arrived at it) and `edges` is each of
    those names' own similar list, as far as the caller has gathered it. A candidate
    scores what reached its parent times its own match, decayed once for the hop — so a
    strong parent is worth more than a weak one, and an artist reached by two paths is
    worth both.

    Each parent contributes its strongest `fan_out` neighbours and no more: that cap is
    the price of the next hop, because every one of them is a request. `seen` holds the
    folded names already reached, the seed among them; one of those is scored again but
    never followed, since these lists carry the artist itself and each other, and a walk
    that follows those forever is a flood rather than a search.
    """
    found: list[WalkedArtist] = []
    following: list[tuple[str, float]] = []
    reached = set(seen)
    for name, arrived in frontier:
        parent = name.casefold()
        for candidate in rank(list(edges.get(name, [])), min_match=min_match, limit=fan_out):
            key = candidate.name.casefold()
            if key == parent:
                continue  # Last.fm lists an artist among its own similar; that is not an answer
            score = arrived * candidate.match * decay
            found.append(
                WalkedArtist(name=candidate.name, score=score, hops=hops, url=candidate.url)
            )
            if key in reached:
                continue  # already walked from somewhere else: it counts, it is not asked again
            reached.add(key)
            following.append((candidate.name, score))
    # Strongest first, so a name two parents both reached is followed from the better one.
    following.sort(key=lambda item: item[1], reverse=True)
    return WalkStep(found=found, following=following, seen=frozenset(reached))
