"""The key sheet, and the bindings it is generated from."""

from rich.console import Console
from textual.widgets import (
    Static,
)
from tuihelpers import (
    FakeClient,
    FakePlayer,
    make_app,
    opened_detail,
    text_of,
)

from lfm.ui.app import (
    LfmApp,
    ShortcutsScreen,
)
from lfm.ui.artist import (
    ArtistDetail,
)
from lfm.ui.screens import (
    SimilarScreen,
    SourceScreen,
    TagScreen,
)
from lfm.ui.shortcuts import (
    KEY_LABELS,
    binding_parts,
    shortcut_groups,
)


def renderable_text(renderable) -> str:
    """A Rich renderable flattened to the text it draws, for reading a sheet back."""
    console = Console(width=200, no_color=True)
    with console.capture() as captured:
        console.print(renderable)
    return captured.get()


async def test_F1_opens_the_shortcuts_and_F1_closes_them_again():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        detail = await opened_detail(app, pilot)
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, ShortcutsScreen)

        await pilot.press("f1")
        await pilot.pause()
        assert app.screen is detail  # straight back, nothing else stirred


async def test_the_sheet_lists_the_keys_that_never_fitted_a_bottom_row():
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(100, 30)) as pilot:
        await opened_detail(app, pilot)
        await pilot.press("f1")
        await pilot.pause()
        # the columns are renderables now, so the sheet is read back out of them
        columns = [
            column for column in app.screen.query(".shortcut-column") if isinstance(column, Static)
        ]
        sheet = " ".join(renderable_text(column.content) for column in columns)

        # the point of the sheet: the hidden ones, and enough to read it
        for fragment in ("Artist", "Deep cuts", "Repeat", "Vol+", "+15s", "Exit shortcuts"):
            assert fragment in sheet


async def test_the_home_hint_points_at_the_shortcuts():
    app = LfmApp(FakeClient(), FakePlayer())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "F1 for shortcuts" in text_of(app.query_one("#hint", Static))


def test_every_named_key_has_a_label_a_person_would_write():
    """`plus`, `minus` and `shift+tab` used to reach the sheet as Textual spells them."""
    screens = (LfmApp, ArtistDetail, SimilarScreen, TagScreen, SourceScreen, ShortcutsScreen)
    unlabelled = sorted(
        {
            binding_parts(binding)[0]
            for screen in screens
            for binding in screen.BINDINGS
            if len(binding_parts(binding)[0]) > 1 and binding_parts(binding)[0] not in KEY_LABELS
        }
    )
    assert unlabelled == []


async def test_the_shortcuts_sheet_uses_one_column_on_a_narrow_terminal():
    """Two columns at 60 columns wrapped every description over three rows."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(60, 20)) as pilot:
        app.action_shortcuts()  # the action, not the key: the prompt would eat an `h`
        await pilot.pause()
        assert len(app.screen.query(".shortcut-column")) == 1
        assert app.screen.query_one("#shortcut-columns").region.width <= 60

    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        app.action_shortcuts()
        await pilot.pause()
        assert len(app.screen.query(".shortcut-column")) == 2


async def test_F1_opens_the_sheet_with_the_prompt_focused(isolated_env):
    """It works from the landing page too, where the prompt holds the keyboard."""
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        assert app.query_one("#artist").has_focus  # which is why the hint says F1
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, ShortcutsScreen)
        await pilot.press("f1")
        await pilot.pause()
        assert not isinstance(app.screen, ShortcutsScreen)


def test_the_sheet_says_each_thing_once():
    """It listed `d Deep cuts` under three headings, and the volume four times."""
    descriptions = [description for _, entries in shortcut_groups() for _, description in entries]
    assert len(descriptions) == len(set(descriptions))


def test_the_sheet_stays_small():
    """39 keys under 10 headings was 49 lines, most of it repetition.

    33 now: the two Shift seek rows are the only additions the coarse keys need,
    and the Shift volume keys share their rows with the bracket keys.
    """
    groups = shortcut_groups()
    assert sum(len(entries) for _, entries in groups) + len(groups) <= 33


def test_keys_for_one_action_share_a_row():
    rows = {key for _, entries in shortcut_groups() for key, _ in entries}
    # `h / F1` was one of these until `h` was unbound and only F1 was left
    assert "Tab / Shift+Tab" in rows
    assert "Shift+↓ / [ / -" in rows and "Shift+↑ / ] / +" in rows
