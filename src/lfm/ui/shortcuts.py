"""The key sheet: every row generated from BINDINGS, so it cannot drift."""

from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.binding import Binding

if TYPE_CHECKING:
    from .app import LfmApp

# Textual keeps key names machine-ish; these are the ones worth spelling out.
KEY_LABELS = {
    "space": "Space",
    "delete": "Del",
    "escape": "Esc",
    "enter": "Enter",
    "tab": "Tab",
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "plus": "+",
    "minus": "-",
    "shift+tab": "Shift+Tab",
    "shift+left": "Shift+←",
    "shift+right": "Shift+→",
    "shift+up": "Shift+↑",
    "shift+down": "Shift+↓",
    "f1": "F1",
}

# What the keys do not say for themselves, in one group rather than five. Terse on
# purpose: a cheat sheet with sentences in it is a chapter.
SHORTCUT_NOTES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Prompt, lists and seeking",
        [
            ("Enter", "search, open or play"),
            ("↑ ↓", "move"),
            ("Tab", "the suggestion, else the recent list"),
            ("Del", "Forget"),
            ("b / Esc", "back"),
            ("0–9", "jump through the track"),
        ],
    ),
]


def shortcut_colours(app: "LfmApp") -> dict[str, str]:
    """The sheet's three colours, as hex. Rich cannot read Textual's CSS variables."""
    variables = app.get_css_variables()
    wanted = {
        "heading": ("accent", "#ffa62b"),
        "key": ("text", "#e8e3da"),
        "description": ("text-muted", "#a7a7ad"),
    }
    colours = {}
    for name, (variable, fallback) in wanted.items():
        value = str(variables.get(variable, ""))
        colours[name] = value if value.startswith("#") else fallback
    return colours


def key_label(key: str) -> str:
    """A binding's key as a person would write it."""
    return KEY_LABELS.get(key, key)


def binding_parts(binding) -> tuple[str, str]:
    """(key, action) of a binding, accepting Textual's Binding or tuple spelling."""
    if isinstance(binding, Binding):
        return binding.key, binding.action
    return binding[0], binding[1]  # ("key", "action") or ("key", "action", "description")


def binding_entry(binding) -> tuple[str, str] | None:
    """(key, description) of a binding, or None when it has nothing to say."""
    key, _ = binding_parts(binding)
    description = (
        binding.description
        if isinstance(binding, Binding)
        else (binding[2] if len(binding) > 2 else "")
    )
    return (key_label(key), description) if description else None


KEY_COLUMN_WIDTH = 16  # the old `.shortcut-key { width: 16 }`, now a table column


def shortcut_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    """(heading, its (key, description) rows) for the cheat sheet.

    The rows come from the bindings themselves, so the sheet cannot drift from
    what the keys actually do: add a binding anywhere below and it shows up here,
    hidden ones included (which is rather the point of a cheat sheet). One row per
    key keeps a pair from being split across a wrapped line, which is what a
    paragraph of them did.
    """
    # imported here because every screen imports this module for its key sheet
    from .app import LfmApp
    from .artist import ArtistDetail
    from .screens import SimilarScreen, SourceScreen, TagScreen

    screens = (
        ("Everywhere", LfmApp),
        ("Artist", ArtistDetail),
        ("Similar artists", SimilarScreen),
        ("Tag artists", TagScreen),
        ("Sources", SourceScreen),
    )
    written = list(SHORTCUT_NOTES)
    for heading, screen in screens:
        # "back" is every screen's own key, so it is said once in the notes instead
        own = [b for b in screen.BINDINGS if binding_parts(b)[1] != "app.pop_screen"]
        entries = [entry for entry in map(binding_entry, own) if entry]
        if entries:
            written.append((heading, entries))

    # Say each thing once. It used to say "d Deep cuts" under three headings, list the
    # volume four times, and split `h`/`F1` and `Tab`/`Shift+Tab` over two rows each.
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    said: set[tuple[str, str]] = set()
    described: set[str] = set()
    for heading, entries in written:
        rows: list[tuple[str, str]] = []
        where: dict[str, int] = {}
        for key, description in entries:
            if (key, description) in said:
                continue
            said.add((key, description))
            if description in described:  # the same thing, a second key for it
                rows[where[description]] = (f"{rows[where[description]][0]} / {key}", description)
                continue
            described.add(description)
            where[description] = len(rows)
            rows.append((key, description))
        if rows:
            groups.append((heading, rows))
    return groups


def shortcut_column(
    column: list[tuple[str, list[tuple[str, str]]]],
    *,
    heading: str,
    key: str,
    description: str,
) -> Group:
    """One column of the sheet as a single renderable: each group's heading, then its keys.

    It used to be a widget per key — ninety-odd Statics for the whole sheet — and Textual
    spends a couple of milliseconds resolving styles for each, so opening the help took
    about a quarter of a second. This is a heading `Text` and a `Table` per group, in one
    `Group`: same look, a long description still wraps inside its own column, one frame to
    draw. The heading is its own line because a table row cannot span both columns.
    """
    parts: list[RenderableType] = []
    for index, (group, entries) in enumerate(column):
        if index:
            parts.append(Text(""))  # air between groups: a wall of rows is not a sheet
        parts.append(Text(group, style=f"bold {heading}"))
        table = Table.grid(expand=True)
        table.add_column(width=KEY_COLUMN_WIDTH)
        table.add_column()  # the rest, word-wrapped
        for name, what in entries:
            table.add_row(Text(name, style=f"bold {key}"), Text(what, style=description))
        parts.append(table)
    return Group(*parts)


TWO_COLUMN_WIDTH = 84


def shortcut_columns(width: int = 200) -> list[list[tuple[str, list[tuple[str, str]]]]]:
    """The sheet's groups laid out in two columns, balanced by how tall they are.

    One long list is a scroll: the artist's thirteen keys put the rest of the sheet
    below the fold. Two columns fit it and keep each pair side by side where it is
    read — but only when there is room for both. At 60 columns the pair squeezed
    every description into a three-line strip, so a narrow terminal gets one column
    and scrolls.
    """
    if width < TWO_COLUMN_WIDTH:
        return [shortcut_groups()]
    columns: list[list[tuple[str, list[tuple[str, str]]]]] = [[], []]
    heights = [0, 0]
    for group in shortcut_groups():
        column = 0 if heights[0] <= heights[1] else 1
        columns[column].append(group)
        heights[column] += len(group[1]) + 2  # its rows, its heading, and the air
    return columns
