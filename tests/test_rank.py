import math

import pytest

from lfm.api.models import SearchArtist, SimilarArtist
from lfm.api.rank import blend, deep_cuts, rank, suggestions, walk_level


def a(name: str, match: float) -> SimilarArtist:
    return SimilarArtist(name=name, match=match)


def test_sorts_by_match_desc():
    assert [x.name for x in rank([a("Low", 0.2), a("High", 0.9), a("Mid", 0.5)])] == [
        "High",
        "Mid",
        "Low",
    ]


def test_min_match_drops_below_threshold():
    ranked = rank([a("High", 0.9), a("Mid", 0.5), a("Low", 0.2)], min_match=0.5)
    assert [x.name for x in ranked] == ["High", "Mid"]


def test_dedupe_is_case_insensitive_and_keeps_highest():
    ranked = rank([a("Kode9", 0.4), a("kode9", 0.9), a("Zomby", 0.8)])
    assert [x.name for x in ranked] == ["kode9", "Zomby"]


def test_limit_caps_after_sort_and_dedupe():
    ranked = rank([a("A", 0.9), a("a", 0.8), a("B", 0.7), a("C", 0.6)], limit=2)
    assert [x.name for x in ranked] == ["A", "B"]


def test_empty_in_empty_out():
    assert rank([], min_match=0.5, limit=10) == []


def test_blend_is_the_geometric_mean_of_the_seeds():
    results = blend({"A": [a("Shared", 0.9)], "B": [a("Shared", 0.7)]})
    assert [r.name for r in results] == ["Shared"]
    assert results[0].score == pytest.approx(math.sqrt(0.9 * 0.7))
    assert results[0].matches == {"A": 0.9, "B": 0.7}


def test_blend_penalises_a_seed_that_does_not_know_the_artist():
    results = blend({"A": [a("Both", 0.9), a("OnlyA", 1.0)], "B": [a("Both", 0.9)]})
    assert [r.name for r in results] == ["Both", "OnlyA"]
    assert results[1].score < results[0].score


def test_blend_excludes_the_seeds_themselves():
    results = blend({"Burial": [a("Burial", 1.0), a("Kode9", 0.9)]}, exclude={"Burial"})
    assert [r.name for r in results] == ["Kode9"]


def test_blend_merges_names_case_insensitively():
    results = blend({"A": [a("Kode9", 0.5)], "B": [a("kode9", 0.9)]})
    assert [r.name for r in results] == ["Kode9"]
    assert results[0].matches == {"A": 0.5, "B": 0.9}


def test_blend_respects_min_score_and_limit():
    seed_lists = {"A": [a("High", 0.9), a("Low", 0.1)], "B": [a("High", 0.9)]}
    assert [r.name for r in blend(seed_lists, min_score=0.2)] == ["High"]
    assert len(blend(seed_lists, limit=1)) == 1


def test_deep_cuts_keeps_the_obscure_ones_in_order():
    artists = [a("Kode9", 0.5), a("Zomby", 0.5), a("Darkstar", 0.5)]
    counts = {"kode9": 900_000, "zomby": 40_000, "darkstar": 12_000}
    found = deep_cuts(artists, counts, max_listeners=50_000)
    assert [a.name for a in found.artists] == ["Zomby", "Darkstar"]
    assert (found.threshold, found.unknown) == (50_000, 0)


def test_deep_cuts_can_work_from_the_seed_instead_of_a_number():
    artists = [a("Kode9", 0.5), a("Darkstar", 0.5)]
    found = deep_cuts(
        artists, {"kode9": 900_000, "darkstar": 12_000}, seed_listeners=2_000_000, ratio=10
    )
    assert [a.name for a in found.artists] == ["Darkstar"]  # 900k is not under 200k
    assert found.threshold == 200_000


def test_deep_cuts_counts_what_it_could_not_judge():
    artists = [a("Kode9", 0.5), a("Zomby", 0.5)]
    found = deep_cuts(artists, {"zomby": 40_000}, max_listeners=50_000)
    assert [a.name for a in found.artists] == ["Zomby"]
    assert found.unknown == 1  # left out rather than guessed at


def test_without_a_threshold_every_artist_we_know_a_count_for_stays():
    found = deep_cuts([a("Kode9", 0.5)], {"kode9": 900_000})
    assert found.threshold is None
    assert [a.name for a in found.artists] == ["Kode9"]


def test_deep_cuts_can_come_back_empty():
    found = deep_cuts([a("Kode9", 0.5)], {"kode9": 900_000}, max_listeners=100)
    assert found.artists == []


def test_the_relative_cut_never_rounds_down_to_zero():
    found = deep_cuts([a("Tiny", 0.5)], {"tiny": 5}, seed_listeners=5, ratio=10)
    assert found.threshold == 1


def hit(name: str, listeners: int) -> SearchArtist:
    return SearchArtist(name=name, listeners=listeners)


def test_suggestions_drop_the_echo_and_its_full_width_duplicates():
    """Recorded live: search('mich') is the query itself, twice more in other widths, then acts."""
    matches = [
        hit("Mich", 44_313),
        hit("ＭｉｃＨ", 2),
        hit("Ｍｉｃｈ", 1),
        hit("Mach-Hommy", 100_000),
        hit("Mich Gerber", 35_425),
    ]
    assert suggestions("mich", matches) == ["Mach-Hommy", "Mich Gerber"]


def test_suggestions_lead_with_the_act_you_meant():
    """Recorded live: search('beatls') -> Beatls, The Beatles, Beatles, R.L. Beats, Kenny Beats."""
    matches = [
        hit("Beatls", 3),
        hit("The Beatles", 6_685_905),
        hit("Beatles", 3_594),
        hit("Kenny Beats", 200_000),
    ]
    assert suggestions("beatls", matches) == ["The Beatles", "Kenny Beats", "Beatles"]


def test_suggestions_do_not_repeat_one_act_in_two_spellings():
    """The API lists the same act twice, padded and cased differently; offer it once."""
    matches = [hit("The Beatles", 6_685_905), hit("THE  BEATLES", 4)]
    assert suggestions("beatle", matches) == ["The Beatles"]


def test_suggestions_can_come_back_empty():
    assert suggestions("mich", [hit("Mich", 44_313)]) == []


def test_a_limit_of_zero_is_a_limit_of_zero():
    """Not "no limit": `0` is a legitimate value, and hypothesis found `rank`
    returning one artist for it because the cap was checked after the append."""
    assert rank([a("Burial", 0.9), a("Kode9", 0.5)], limit=0) == []


def test_a_walk_scores_the_path_that_reached_the_artist():
    step = walk_level(
        [("Burial", 1.0)],
        {"Burial": [a("Kode9", 1.0), a("Zomby", 0.5)]},
        hops=1,
        seen=frozenset({"burial"}),
    )
    assert [(row.name, row.score, row.hops) for row in step.found] == [
        ("Kode9", 0.5, 1),  # a perfect match, halved for being one hop out
        ("Zomby", 0.25, 1),
    ]
    assert step.following == [("Kode9", 0.5), ("Zomby", 0.25)]  # strongest first


def test_a_walk_scores_an_artist_every_path_reaches_it_by():
    step = walk_level(
        [("A", 1.0), ("B", 0.5)],
        {"A": [a("Shared", 0.8)], "B": [a("shared", 0.4)]},
        hops=1,
        seen=frozenset({"a", "b"}),
    )
    assert [row.score for row in step.found] == [0.4, 0.1]
    assert step.following == [("Shared", 0.4)]  # asked about once, from its better parent


def test_a_walk_never_asks_about_an_artist_it_has_already_reached():
    """These lists carry the artist itself and names already walked from: not new ground."""
    step = walk_level(
        [("Burial", 1.0)],
        {"Burial": [a("Burial", 0.9), a("Kode9", 0.8), a("kode9", 0.7)]},
        hops=1,
        seen=frozenset({"burial", "kode9"}),
    )
    assert [row.name for row in step.found] == ["Kode9"]  # itself is not an answer
    assert step.following == []  # and a name already walked is not asked about again


def test_a_walk_follows_only_the_strongest_neighbours_it_was_told_to():
    neighbours = [a(f"artist-{i}", 1.0 - i / 100) for i in range(30)]
    step = walk_level(
        [("Seed", 1.0)], {"Seed": neighbours}, hops=1, seen=frozenset({"seed"}), fan_out=5
    )
    assert len(step.found) == 5  # the rest are neither walked nor scored: they are the cap
    assert [name for name, _ in step.following] == [f"artist-{i}" for i in range(5)]


def test_a_walk_honours_min_match_on_the_way_out():
    step = walk_level(
        [("Seed", 1.0)],
        {"Seed": [a("Close", 0.9), a("Loose", 0.2)]},
        hops=1,
        seen=frozenset({"seed"}),
        min_match=0.5,
    )
    assert [row.name for row in step.found] == ["Close"]


def test_a_parent_with_nothing_to_say_costs_the_walk_nothing():
    step = walk_level([("Seed", 1.0)], {"Seed": []}, hops=2, seen=frozenset({"seed"}))
    assert (step.found, step.following) == ([], [])


def test_a_walk_records_how_far_out_it_found_an_artist_and_keeps_its_url():
    step = walk_level(
        [("Seed", 0.5)],
        {"Seed": [SimilarArtist(name="Kode9", match=0.5, url="https://www.last.fm/music/Kode9")]},
        hops=2,
        seen=frozenset({"seed"}),
    )
    assert [(row.hops, row.url) for row in step.found] == [(2, "https://www.last.fm/music/Kode9")]
