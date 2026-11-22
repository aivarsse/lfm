"""Run the app against the fakes, for the real-terminal test.

Not a test itself (no `test_` prefix, so pytest ignores it): it is the program
tmux starts. It plays a track whose title is the one from a real screenshot —
CJK, full-width brackets, an apostrophe — because wide characters are where a
terminal and Rich's model of it can disagree, and this app's real data is full
of them.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuihelpers import FakePlayer, make_app  # noqa: E402

WIDE_TITLE = "Baby Don't Cry - 安室奈美恵（フル）"


async def main() -> None:
    player = FakePlayer()
    await player.play_url(WIDE_TITLE, "https://example.invalid/watch")
    app = make_app(player, artist="Burial")
    await app.run_async()


if __name__ == "__main__":
    asyncio.run(main())
