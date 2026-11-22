"""The player bar at the sizes a terminal actually comes in.

Every screen is snapshotted at exactly one size, so nothing tests either side of
the bar's thresholds: `NARROW_BAR_WIDTH` columns, where the seek buttons go, and
the width at which a long title would push the readout off the row. The
`NARROW_BAR_WIDTH` comment records the bugs that motivated it — the buttons were
eating the room the time and the progress line needed, and at 56 they left the
progress two cells wide — and these are the assertions that would have caught
that.
"""

from __future__ import annotations

import pytest
from textual.widgets import Button, Static
from tuihelpers import FakePlayer, make_app, opened_detail, text_of

from lfm.ui.widgets import NARROW_BAR_WIDTH

# Either side of the threshold, plus the old 56 and a couple of roomy sizes.
SIZES = [
    (NARROW_BAR_WIDTH - 13, 24),
    (NARROW_BAR_WIDTH - 1, 24),
    (NARROW_BAR_WIDTH, 24),
    (56, 24),
    (80, 24),
    (100, 30),
    (200, 50),
]

BAR_CONTROLS = {"seek-back", "seek-forward", "stop"}


async def played_first_track(app, pilot) -> None:
    """Put a track on, so the bar is populated rather than empty."""
    await opened_detail(app, pilot)
    await pilot.press("enter")
    await app.workers.wait_for_complete()


@pytest.mark.parametrize(("columns", "rows"), SIZES)
async def test_the_seek_buttons_appear_exactly_where_there_is_room(columns: int, rows: int) -> None:
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(columns, rows)) as pilot:
        await played_first_track(app, pilot)
        shown = columns >= NARROW_BAR_WIDTH
        for button in app.screen.query(Button):
            if button.id in BAR_CONTROLS:
                assert button.display is shown, f"{button.id} at {columns} columns"


@pytest.mark.parametrize(("columns", "rows"), SIZES)
async def test_the_readout_and_progress_line_always_keep_their_room(
    columns: int, rows: int
) -> None:
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(columns, rows)) as pilot:
        await played_first_track(app, pilot)
        title = app.screen.query_one("#now-playing", Static)
        info = app.screen.query_one("#bar-info", Static)
        progress = app.screen.query_one("#progress", Static)
        assert text_of(info).endswith("vol ▇ 100%")  # the readout, whole
        assert progress.size.width > 0  # and the line did not lose its room
        assert title.size.width + info.size.width <= columns  # the row fits


async def test_a_long_title_shortens_rather_than_pushing_the_readout_off() -> None:
    """The title is the one widget that may give ground; the readout may not."""
    app = make_app(FakePlayer(), artist="Burial")
    async with app.run_test(size=(60, 24)) as pilot:
        await played_first_track(app, pilot)
        title = app.screen.query_one("#now-playing", Static)
        before = title.size.width
        title.update("A" * 400)
        await pilot.pause()
        info = app.screen.query_one("#bar-info", Static)
        assert text_of(info).endswith("vol ▇ 100%")  # still whole, still last
        assert title.size.width <= before  # and the title is what shrank
