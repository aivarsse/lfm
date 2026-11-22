"""A real terminal, not Rich's model of one.

The snapshots record what Rich believes the screen to be. This runs the app in an
actual terminal and reads the cells the terminal actually has, which is the only
way to catch the two disagreeing. Wide characters are where they can: a CJK title
is two cells per glyph, and the bar is laid out in cells.

Skipped where tmux is absent rather than failed: an unrunnable check has not run,
and pretending otherwise is what a green suite must never do.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOCKET = "lfm-terminal-test"
# The title from a real screenshot of a real session.
WIDE_TITLE = "Baby Don't Cry - 安室奈美恵（フル）"
COLUMNS, ROWS = 100, 30


def cells(text: str) -> int:
    """What a terminal charges for this text: wide characters take two cells."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def tmux(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["tmux", "-L", SOCKET, "-f", "/dev/null", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    ).stdout


def bar_row(rows: list[str]) -> str:
    return next(row for row in rows if "vol" in row)


@pytest.fixture(scope="module")
def pane() -> Iterator[list[str]]:
    """The app's screen, as a real terminal has it.

    Own socket and no config file, so nothing here reads or writes the owner's
    tmux or the app's real caches.
    """
    tmux("kill-server", check=False)
    with tempfile.TemporaryDirectory() as home:
        env = (
            f"XDG_CONFIG_HOME={home}/config XDG_CACHE_HOME={home}/cache "
            f"XDG_DATA_HOME={home}/data PYTHONPATH={ROOT}/tests TERM=xterm-256color"
        )
        tmux(
            "new-session",
            "-d",
            "-s",
            "lfm",
            "-x",
            str(COLUMNS),
            "-y",
            str(ROWS),
            f"cd {ROOT} && env {env} .venv/bin/python tests/run_in_terminal.py",
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                rows = tmux("capture-pane", "-p", "-t", "lfm").splitlines()
                if any("vol" in row for row in rows):
                    yield rows
                    return
                time.sleep(0.5)
            pytest.fail("the app never drew its player bar in the terminal")
        finally:
            tmux("kill-server", check=False)


def test_the_readout_keeps_the_space_before_the_percentage(pane: list[str]) -> None:
    """`vol ▇ 100%`, not `vol▇100%`.

    This is the one thing no snapshot can see: Rich's SVG holds the space in a
    text run, but a renderer that paints the glyph's background across the next
    cell hides it — which is exactly what happened when this bar was rendered to
    PNG, and it took a real screenshot to notice.
    """
    assert "vol ▇ 100%" in bar_row(pane)


def test_a_wide_title_stays_inside_the_row(pane: list[str]) -> None:
    """Two cells per CJK glyph, so the title is where a row overflows first."""
    row = bar_row(pane)
    assert WIDE_TITLE in row  # intact, not mangles or truncated mid-glyph
    assert row.rstrip().endswith("vol ▇ 100%")  # the readout still ends the row
    assert cells(row) <= COLUMNS  # and the row never ran off the screen


def test_the_progress_line_ends_the_row_below(pane: list[str]) -> None:
    """The seek row is drawn to the same width, in the same terminal."""
    seek = next(row for row in pane if "-15s" in row)
    assert cells(seek) <= COLUMNS
    assert "0:00 / 1:40" in seek
