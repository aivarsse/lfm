"""The playback seam the screens are written against (lets tests inject a fake)."""

from typing import Protocol

from ..playback.player import (
    PlayerStatus,
    PlayResult,
    YtResult,
)


class Player(Protocol):
    """The playback surface the TUI needs (lets tests inject a fake)."""

    async def play(self, artist: str, track: str) -> PlayResult: ...

    async def play_url(self, title: str, url: str) -> PlayResult: ...

    async def find_sources(self, artist: str, track: str) -> list[YtResult]: ...

    def prefetch_track(self, artist: str, track: str) -> None: ...

    async def prefetch(self, url: str) -> str | None: ...

    async def toggle_pause(self) -> bool | None: ...

    async def seek(self, seconds: int) -> None: ...

    async def change_volume(self, delta: int) -> int: ...

    volume: float  # read for mute, written by `m`

    async def stop(self) -> None: ...

    async def status(self) -> PlayerStatus: ...
