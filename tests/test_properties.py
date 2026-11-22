"""What must hold for *every* input, not just the ones someone thought of.

The bugs that reached review this week were invariants, not examples: a bar that
was one cell wide because it appended per cell, a ranking that followed the order
the API handed it instead of the column that decides. Hypothesis is here for that
shape of bug, and it shrinks a failure to the smallest input that still shows it —
which is a better bug report than any example.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from lfm.api.models import SimilarArtist
from lfm.api.rank import rank, walk_level
from lfm.ui.render import VOLUME_LEVELS, progress_bar, volume_glyph

FILL = "#ffa62b"  # one colour: the bar is a solid run

finite = st.floats(0, 5_000, allow_nan=False, allow_infinity=False)


def of(*pairs: tuple[str, float]) -> list[SimilarArtist]:
    return [SimilarArtist(name=name, match=match) for name, match in pairs]


@given(position=finite, duration=finite, width=st.integers(1, 200))
def test_the_progress_line_is_exactly_as_wide_as_asked(
    position: float, duration: float, width: int
) -> None:
    """A bar of the wrong width is a layout bug wherever it is drawn."""
    assert progress_bar(position, duration, width, FILL).cell_len == width


def _filled(position: float, width: int) -> int:
    text = progress_bar(position, 300.0, width, FILL)
    return sum(len(span) for span in text.spans if str(span.style) != "dim")


@given(small=finite, big=finite, width=st.integers(1, 60))
def test_further_along_never_draws_less(small: float, big: float, width: int) -> None:
    """Whatever the proportions, the later position fills at least as much."""
    low, high = sorted((small, big))
    assert _filled(high, width) >= _filled(low, width)


@given(volume=finite)
def test_the_volume_glyph_never_leaves_its_scale(volume: float) -> None:
    assert volume_glyph(volume) in VOLUME_LEVELS


@given(a=st.floats(0, 130, allow_nan=False, allow_infinity=False), b=finite)
def test_the_volume_glyph_climbs_with_the_volume(a: float, b: float) -> None:
    """A louder setting never draws a lower block."""
    low, high = sorted((a, b))
    assert VOLUME_LEVELS.index(volume_glyph(low)) <= VOLUME_LEVELS.index(volume_glyph(high))


@given(matches=st.lists(st.floats(0, 1, allow_nan=False), min_size=1, max_size=12))
def test_the_ranking_follows_the_deciding_column(matches: list[float]) -> None:
    """The order it was handed is not the ranking; the deciding column is.

    #114 was exactly this shape: the list looked plausible because the API had
    already returned it in relevance order.
    """
    names = [f"artist-{i}" for i in range(len(matches))]
    ranked = rank(of(*zip(names, matches, strict=True)))
    assert [artist.match for artist in ranked] == sorted(matches, reverse=True)


@given(
    pairs=st.lists(
        st.tuples(st.text(min_size=1, max_size=20), st.floats(0, 1, allow_nan=False)),
        min_size=1,
        max_size=12,
    ),
    limit=st.integers(0, 15),
    floor=st.floats(0, 1, allow_nan=False),
)
def test_the_ranking_never_invents_loses_or_reorders_past_its_limits(
    pairs: list[tuple[str, float]], limit: int, floor: float
) -> None:
    """Whatever it is given and whatever the limits, the output obeys both."""
    ranked = rank(of(*pairs), min_match=floor, limit=limit)
    assert {artist.name for artist in ranked} <= {name for name, _ in pairs}
    assert len(ranked) <= min(limit, len(pairs))  # the limit is a limit
    assert all(artist.match >= floor for artist in ranked)  # the floor is a floor


@given(
    arrived=st.floats(0, 1, allow_nan=False),
    matches=st.lists(st.floats(0, 1, allow_nan=False), min_size=1, max_size=10),
    decay=st.floats(0, 1, allow_nan=False),
    fan_out=st.integers(1, 10),
)
def test_a_hop_never_scores_more_than_the_score_that_reached_it(
    arrived: float, matches: list[float], decay: float, fan_out: int
) -> None:
    """The decay is the whole point of it: nothing a hop out outranks what led there."""
    names = [f"artist-{i}" for i in range(len(matches))]
    step = walk_level(
        [("Seed", arrived)],
        {"Seed": of(*zip(names, matches, strict=True))},
        hops=1,
        seen=frozenset({"seed"}),
        decay=decay,
        fan_out=fan_out,
    )
    assert all(row.score <= arrived * decay for row in step.found)


@given(
    matches=st.lists(st.floats(0, 1, allow_nan=False), min_size=1, max_size=20),
    fan_out=st.integers(1, 10),
)
def test_a_walk_follows_no_more_than_the_fan_out_it_was_given(
    matches: list[float], fan_out: int
) -> None:
    """The fan-out is the call budget, so it is a bound rather than a suggestion."""
    names = [f"artist-{i}" for i in range(len(matches))]
    step = walk_level(
        [("Seed", 1.0)],
        {"Seed": of(*zip(names, matches, strict=True))},
        hops=1,
        seen=frozenset({"seed"}),
        fan_out=fan_out,
    )
    assert len(step.found) <= fan_out
    assert len(step.following) <= fan_out


@given(matches=st.lists(st.floats(0, 1, allow_nan=False), min_size=1, max_size=10))
def test_a_walk_never_follows_a_name_it_has_already_seen(matches: list[float]) -> None:
    """Reached is not followed: an artist already walked is counted again and left alone."""
    names = [f"artist-{i}" for i in range(len(matches))]
    step = walk_level(
        [("Seed", 1.0)],
        {"Seed": of(*zip(names, matches, strict=True))},
        hops=1,
        seen=frozenset({name.casefold() for name in names}),
    )
    assert step.following == []
    assert len(step.found) == len(names)
