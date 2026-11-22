"""Contrast of the text lfm renders, measured from the render itself.

Textual composites `$text-muted` and the row highlight while drawing, so the only
honest source for "what colour is this on what" is a screenshot, and the SVG that
`save_screenshot` writes is plain text. These tests read it back and check the two
backgrounds a reader actually looks at: the page, and the row the cursor is on.

Letters only. A scrollbar glyph is not text you read (see the open note at the bottom).
"""

import os
import re
import tempfile
import xml.etree.ElementTree as ElementTree

import pytest
from textual.widgets import Input, ListView
from tuihelpers import FakePlayer, make_app

from lfm.ui.theme import (
    LFM_CREAM,
    LFM_MIDNIGHT,
    LFM_OBSIDIAN,
)

CELL, ROW = 12.2, 24.4
NS = "{http://www.w3.org/2000/svg}"


def luminance(colour: str) -> float:
    colour = colour.lstrip("#")
    channels = [int(colour[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(one: str, two: str) -> float:
    first, second = luminance(one), luminance(two)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def rendered(path: str) -> list[tuple[str, str, str]]:
    """Every rendered character as (fill, the fill of the cell behind it, the character)."""
    text = open(path).read()
    rules = dict(re.findall(r"\.([\w-]+)\s*\{\s*fill:\s*(#[0-9a-fA-F]{6})", text))
    backgrounds: dict[tuple[int, int], str] = {}
    cells: list[tuple[str, str, str]] = []

    def walk(node, inherited: str = "#000000", dx: float = 0.0, dy: float = 0.0) -> None:
        here = node.get("fill") or inherited
        for name in node.get("class", "").split():
            if name in rules:
                here = rules[name]
        found = re.match(r"translate\(([-\d.]+)[ ,]+([-\d.]+)\)", node.get("transform", ""))
        if found:
            dx, dy = dx + float(found.group(1)), dy + float(found.group(2))
        if node.tag == f"{NS}rect":
            y, height = float(node.get("y", 0)) + dy, float(node.get("height", ROW))
            x, width = float(node.get("x", 0)) + dx, float(node.get("width", 0))
            for row in range(max(0, round((y - 1.5) / ROW)), round((y + height - 1.5) / ROW)):
                for col in range(round(x / CELL), round((x + width) / CELL)):
                    backgrounds[(col, row)] = here
        elif node.tag == f"{NS}text":
            where = "".join(node.itertext()).replace("\xa0", " ")
            row = round((float(node.get("y", 0)) + dy - 19.8) / ROW)
            col = round((float(node.get("x", 0)) + dx) / CELL)
            for offset, char in enumerate(where):
                if char.strip():
                    cells.append((here, backgrounds.get((col + offset, row), "#000000"), char))
        for child in node:
            walk(child, here, dx, dy)

    walk(ElementTree.fromstring(text))
    return cells


def worst_letters(path: str, background: str) -> float:
    worst = 21.0
    for fill, behind, char in rendered(path):
        if behind == background and char.isalnum():
            worst = min(worst, ratio(fill, behind))
    return worst


async def shot(theme: str, focus: str = "#tracks") -> tuple[str, dict[str, str]]:
    """The artist page, rendered natively in `theme`, with `focus` focused.

    Returns the screenshot and the app's *resolved* CSS variables: the theme object's
    fields are not always what the app renders with (midnight inherits Textual's `error`
    field while overriding the variable), and `$text-muted` is translucent until the
    compositor flattens it.
    """
    app = make_app(FakePlayer())
    async with app.run_test(size=(100, 30)) as pilot:
        # The theme is settled before the page is built: a switch afterwards leaves
        # already-drawn widgets in the old theme's colours, which is Textual's
        # compositor rather than anything lfm does, and would measure the wrong screen.
        app.theme = theme
        await pilot.pause()
        app.query_one("#artist", Input).value = "Burial"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        detail = app.screen
        detail.query_one(focus, ListView).focus()
        await pilot.pause()
        path = os.path.join(tempfile.mkdtemp(), f"{theme}.svg")
        app.save_screenshot(path)
        variables = {name: str(value) for name, value in app.get_css_variables().items()}
    return path, variables


# The marker's shapes, per the play/pause marker's own change: they are marks, not text,
# so they answer to the 3:1 non-text floor rather than 4.5:1.
MARKER_SHAPES = "▶⏸●○"

# Every pair the interface relies on, one place. Parametrised over the themes rather than
# written per theme: a fourth theme then cannot arrive with holes in it, which is what the
# issue asked for and what the previous three near-identical tests could not promise.
THEMES = [theme for theme in (LFM_OBSIDIAN, LFM_CREAM, LFM_MIDNIGHT) if theme is not None]


@pytest.mark.parametrize("theme", THEMES, ids=lambda theme: theme.name)
async def test_the_active_tab_stays_legible_when_the_keyboard_leaves(theme):
    """The tab keeps a grey block when the tags window has the keyboard: measure it."""
    path, _ = await shot(theme.name, focus="#tags")
    backgrounds = {background for _, background, char in rendered(path) if char.isalnum()}
    assert backgrounds, "the render produced no letters at all"
    for background in backgrounds:
        assert worst_letters(path, background) >= 4.5, (theme.name, background)


@pytest.mark.parametrize("theme", THEMES, ids=lambda theme: theme.name)
async def test_a_theme_clears_every_pair_the_interface_relies_on(theme):
    path, variables = await shot(theme.name)

    # Letters: on every background the render actually used, not only the ones we can name.
    # Textual composites `$text-muted` and the row highlight while drawing, and midnight's
    # row is a composite of a highlight the built-in leaves unset, so the render is the only
    # honest source for any of it.
    backgrounds = {background for _, background, char in rendered(path) if char.isalnum()}
    assert backgrounds, "the render produced no letters at all"
    for background in backgrounds:
        assert worst_letters(path, background) >= 4.5, (theme.name, background)

    # The marker is a mark, not text: 3:1, on whatever it sits on.
    for fill, background, char in rendered(path):
        if char in MARKER_SHAPES:
            assert ratio(fill, background) >= 3.0, (theme.name, char, background)

    # The semantic colours, from the resolved variables, measured against the page. These
    # carry text when a notification shows them.
    page = variables.get("background", "")
    assert page.startswith("#"), (theme.name, page)
    for name in ("accent", "warning", "error", "success"):
        colour = variables.get(name, "")
        assert colour.startswith("#"), (theme.name, name, colour)
        assert ratio(colour, page) >= 4.5, (theme.name, name, colour)
