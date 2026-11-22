from rich.cells import cell_len

from lfm.api.models import ArtistInfo
from lfm.api.rank import shared_tags
from lfm.ui.render import (
    blend,
    compact_count,
    ease_in_out_cubic,
    info_table,
    one_line,
    progress_bar,
    stats_line,
    truncated,
    volume_glyph,
)


def test_ease_in_out_cubic_matches_the_tabs_transition_curve():
    """The same curve Textual's `in_out_cubic` uses: the fade must feel like a tab."""
    assert ease_in_out_cubic(0.0) == 0.0
    assert ease_in_out_cubic(0.5) == 0.5  # its fixed point, like the tabs'
    assert ease_in_out_cubic(1.0) == 1.0
    assert ease_in_out_cubic(-3.0) == 0.0  # clamped, so a slow frame cannot undershoot
    assert ease_in_out_cubic(4.0) == 1.0
    points = [ease_in_out_cubic(i / 20) for i in range(21)]
    assert points == sorted(points)  # never backwards


def test_shared_tags_keeps_the_seed_order_case_insensitively():
    seed = ["Dubstep", "Ambient", "Bass"]
    other = ["ambient", "BASS"]
    assert shared_tags(seed, other) == ["Ambient", "Bass"]


def test_shared_tags_caps_the_list():
    assert shared_tags(["a", "b", "c", "d"], ["a", "b", "c", "d"], limit=2) == ["a", "b"]


def test_shared_tags_when_nothing_is_shared():
    assert shared_tags(["dubstep"], ["polka"]) == []
    assert shared_tags([], ["polka"]) == []
    assert shared_tags(["dubstep"], []) == []


def test_a_corrected_name_is_not_parsed_as_markup(capsys):
    from lfm.ui.render import table

    table("[bold red]evil[/bold red]", [])
    assert "[bold red]evil[/bold red]" in capsys.readouterr().out


def test_blend_seeds_are_not_parsed_as_markup(capsys):
    from lfm.ui.render import blend_table

    blend_table(["[red]A[/red]", "B"], [])
    assert "[red]A[/red]" in capsys.readouterr().out


def test_tags_are_not_parsed_as_markup(capsys):
    from lfm.ui.render import info_table

    info = ArtistInfo.model_validate({"name": "Burial", "stats": {}})
    info_table("Burial", info, ["[red]tag[/red]"], [])
    assert "[red]tag[/red]" in capsys.readouterr().out


def test_info_table_shows_bracketed_bio_text_as_written(capsys):
    info = ArtistInfo.model_validate(
        {
            "name": "Burial",
            "bio": {"summary": "Raised on [b]bass[/b] and [citation needed] notes."},
            "stats": {},
        }
    )
    info_table("Burial", info, [], [])
    out = capsys.readouterr().out
    assert "[b]bass[/b]" in out
    assert "[citation needed]" in out


def test_info_table_survives_a_bio_with_a_stray_closing_tag(capsys):
    # rich used to raise MarkupError here, taking the whole command down
    info = ArtistInfo.model_validate({"name": "Burial", "bio": {"summary": "a [/] b"}, "stats": {}})
    info_table("Burial", info, [], [])
    assert "[/]" in capsys.readouterr().out


def test_compact_count_is_exact_below_a_million():
    assert compact_count(0) == "0"
    assert compact_count(573_048) == "573,048"
    assert compact_count(999_999) == "999,999"


def test_compact_count_shortens_from_a_million_up():
    assert compact_count(1_000_000) == "1M"
    assert compact_count(13_850_808) == "13.9M"
    assert compact_count(1_140_897_124) == "1.1B"
    assert compact_count(2_000_000_000) == "2B"
    # a value that rounds up to a thousand of its unit rolls into the next one
    assert compact_count(999_999_999) == "1B"
    assert compact_count(999_999_999_999) == "1T"


def test_one_line_flattens_control_characters_and_keeps_names():
    assert one_line("a\nb\tc") == "a b c"
    assert one_line("  Burial  ") == "Burial"
    assert one_line("坂本龍一") == "坂本龍一"
    assert "\x1b" not in one_line("safe\x1b]0;x")
    assert one_line("a\u2028b") == "a b"


def test_truncated_keeps_the_cell_budget_and_marks_the_loss():
    assert truncated("Burial", 20) == "Burial"
    cut = truncated("A Very Long Album Name That Will Not Fit In The Tabs Bar", 24)
    assert cut.endswith("…")
    assert cell_len(cut) <= 24
    assert truncated("坂本龍一のとても長い名前", 10).endswith("…")


def test_the_cli_stats_line_stays_exact():
    info = ArtistInfo.model_validate(
        {"name": "x", "stats": {"listeners": "1642253", "playcount": "7000000000"}}
    )
    assert stats_line(info) == "1,642,253 listeners · 7,000,000,000 plays"
    assert stats_line(info, compact_count) == "1.6M listeners · 7B plays"


def test_blend_mixes_two_colours():
    assert blend("#000000", "#ffffff", 0.0) == "#000000"
    assert blend("#000000", "#ffffff", 1.0) == "#ffffff"
    assert blend("#000000", "#ffffff", 0.5) == "#808080"
    assert blend("#000000", "#ffffff", 9.0) == "#ffffff"  # clamped
    assert blend("#ffffff", "#000000", -1.0) == "#ffffff"


def test_progress_bar_fills_by_position_and_leaves_the_rest_dim():
    text = progress_bar(25, 100, 8, "#336699")
    assert text.plain == "━" * 8
    # Three regions: the run behind the playhead, one brighter cell at the boundary, and
    # the dim rest. One solid colour, not a gradient shading across its own length.
    head = blend("#336699", "#ffffff", 0.35)
    assert [(str(span.style), span.start, span.end) for span in text.spans] == [
        ("#336699", 0, 1),  # a quarter of eight cells, minus the playhead
        (head, 1, 2),  # the point you are at
        ("dim", 2, 8),
    ]


def test_progress_bar_without_a_duration_is_all_rest():
    text = progress_bar(0.0, 0.0, 4, "#ffffff")
    assert text.plain == "━" * 4
    assert [str(span.style) for span in text.spans] == ["dim"]


def test_volume_glyph_rises_with_the_level():
    assert volume_glyph(0) == "▁"
    assert volume_glyph(65) == "▅"
    assert volume_glyph(130) == "█"
    assert volume_glyph(200) == "█"  # past the ceiling stays at the top
