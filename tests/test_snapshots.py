"""Layout snapshots: the whole screen as text and colour, caught when it shifts.

These run offline against the suite's fakes, so a snapshot is a pure function of the
code and the colours it asks for. Accept a deliberate change with:

    uv run pytest tests/test_snapshots.py --snapshot-update

and read the diff in the committed SVG (they are plain text) before accepting it.
"""

from textual.pilot import Pilot
from tuihelpers import FakePlayer, make_app

from lfm.ui.app import (
    LfmApp,
)
from lfm.ui.theme import (
    THEMES,
)


async def settled(pilot: Pilot[LfmApp]) -> None:
    """Let the page's workers land before the shutter goes."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def test_landing_page_snapshot(snap_compare, isolated_env) -> None:
    assert snap_compare(make_app(FakePlayer()), terminal_size=(100, 30))


# The artist page is deliberately not snapshotted yet, and here is exactly why, so the
# next attempt does not start from scratch. With the visualiser off (no spectrum factory
# is passed, so `_start_spectrum` returns early and the strip stays blank) the screen is
# deterministic: rendered twice by hand, its SVG is byte-identical (38,027 bytes, no
# spectrum glyphs — the only block characters are the bar's `vol 100%`). But as a snapshot
# in the suite it fails, while passing when it is the only test running, and giving these
# tests `isolated_env` did not change that, so it is not config or cache state.
# Reproduce it with:  uv run pytest tests/test_snapshots.py --snapshot-update
# then:               make check
# Next step is to find what the rest of the suite leaves behind that this screen reads.


def test_shortcuts_sheet_snapshot(snap_compare, isolated_env) -> None:
    """`f1`, not `h`: on the landing page the prompt holds focus, so an `h` was typed
    into it and this snapshot showed the landing page for its whole life."""
    app = make_app(FakePlayer())
    assert snap_compare(app, press=["f1"], terminal_size=(100, 30))


async def to_black(pilot: Pilot[LfmApp]) -> None:
    """Switch to the black theme the way `T` does: two steps from dark.

    Not with key presses: on a fresh landing page the prompt holds focus, so typeable
    keys go into it as text and the theme never changes. The action is the same code
    the key runs, and it redraws the wordmark after the switch.
    """
    app = pilot.app
    assert isinstance(app, LfmApp)  # `Pilot.app` is typed as the base App
    while app.theme != THEMES["midnight"]:
        app.action_toggle_theme()
        await pilot.pause()


def test_the_black_theme_snapshot(snap_compare, isolated_env) -> None:
    """The black theme is a copy of Textual's own, with a white wordmark on it."""
    app = make_app(FakePlayer())
    assert snap_compare(app, terminal_size=(100, 30), run_before=to_black)
