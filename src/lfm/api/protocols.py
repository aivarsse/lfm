"""What the read half of the client looks like, for callers that only read.

`Reader` is the part the flows use — the four calls a lookup makes — and
`DetailClient` is everything a screen needs. Both exist so the screens, the flows
and the tests can depend on the shape they use rather than on `LastfmClient`.
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    Album,
    AlbumInfo,
    ArtistInfo,
    SearchArtist,
    SimilarResponse,
    SimilarTrack,
    TagArtist,
    Track,
)


class Reader(Protocol):
    """The reads the flows in `flows.py` make, and nothing more."""

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse: ...

    async def info(self, artist: str) -> ArtistInfo: ...

    async def top_tags(self, artist: str) -> list[str]: ...

    async def top_tracks(self, artist: str) -> list[Track]: ...


class DetailClient(Reader, Protocol):
    """The whole read surface the screens need (lets tests stub it)."""

    async def top_albums(self, artist: str, limit: int = 30) -> list[Album]: ...

    async def album_info(self, artist: str, album: str) -> AlbumInfo: ...

    async def similar_tracks(
        self, artist: str, track: str, limit: int = 30
    ) -> list[SimilarTrack]: ...

    async def tag_top_artists(self, tag: str, limit: int = 30) -> list[TagArtist]: ...

    async def search(self, artist: str, limit: int = 5) -> list[str]: ...

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]: ...
