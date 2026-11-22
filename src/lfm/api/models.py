"""Pydantic models for the slice of the Last.fm API that lfm uses.

Last.fm quirks absorbed here, so nothing downstream has to care:
- numbers arrive as strings ("match": "0.872", "playcount": "58204291");
- a single result arrives as an object, not a one-element list;
- any field can be missing or null.
"""

from pydantic import BaseModel, Field, field_validator, model_validator


def _wrap_list(value: object) -> object:
    """Normalise None -> [] and a single object -> [object]."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_int(value: object) -> object:
    if value in (None, ""):
        return 0
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return int(float(text))  # Last.fm sends whole numbers; this is for "1234.5"


class SimilarArtist(BaseModel):
    """One row of an artist.getSimilar response."""

    name: str
    match: float = 0.0
    url: str | None = None

    @field_validator("match", mode="before")
    @classmethod
    def _coerce_match(cls, value: object) -> object:
        if value in (None, ""):
            return 0.0
        return float(str(value))


class _SimilarAttr(BaseModel):
    artist: str | None = None


class SimilarArtists(BaseModel):
    """The `similarartists` envelope of artist.getSimilar."""

    artist: list[SimilarArtist] = Field(default_factory=list)
    attr: _SimilarAttr | None = Field(None, alias="@attr")

    @field_validator("artist", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class SimilarResponse(BaseModel):
    similarartists: SimilarArtists


class SearchArtist(BaseModel):
    name: str
    listeners: int = 0

    @field_validator("listeners", mode="before")
    @classmethod
    def _coerce_listeners(cls, value: object) -> object:
        return _as_int(value)


class ArtistMatches(BaseModel):
    artist: list[SearchArtist] = Field(default_factory=list)

    @field_validator("artist", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class SearchResults(BaseModel):
    artistmatches: ArtistMatches


class SearchResponse(BaseModel):
    results: SearchResults


class SimilarTrack(BaseModel):
    """One row of a track.getSimilar response: the track and whose it is."""

    name: str
    artist: str
    match: float = 0.0
    url: str | None = None

    @field_validator("artist", mode="before")
    @classmethod
    def _unwrap_artist(cls, value: object) -> object:
        return value.get("name", "") if isinstance(value, dict) else str(value or "")

    @field_validator("match", mode="before")
    @classmethod
    def _coerce_match(cls, value: object) -> object:
        if value in (None, ""):
            return 0.0
        return float(str(value))


class SimilarTracks(BaseModel):
    """The `similartracks` envelope of track.getSimilar."""

    track: list[SimilarTrack] = Field(default_factory=list)

    @field_validator("track", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class SimilarTracksResponse(BaseModel):
    similartracks: SimilarTracks


class Album(BaseModel):
    """One row of an artist.getTopAlbums response."""

    name: str
    playcount: int = 0

    @field_validator("playcount", mode="before")
    @classmethod
    def _coerce_playcount(cls, value: object) -> object:
        return _as_int(value)


class TopAlbums(BaseModel):
    album: list[Album] = Field(default_factory=list)

    @field_validator("album", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class TopAlbumsResponse(BaseModel):
    topalbums: TopAlbums


class AlbumTrack(BaseModel):
    """One track of an album.getInfo tracklist: rank order, duration in seconds."""

    name: str
    rank: int = 0
    duration: int = 0  # 0 = Last.fm does not know it

    @field_validator("rank", "duration", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> object:
        return _as_int(value)

    @model_validator(mode="before")
    @classmethod
    def _rank_from_attr(cls, data: object) -> object:
        """The rank arrives as track["@attr"]["rank"], and not always."""
        if isinstance(data, dict) and "rank" not in data:
            attr = data.get("@attr") or {}
            data = {**data, "rank": attr.get("rank", 0)}
        return data


class AlbumInfo(BaseModel):
    """One album.getInfo response: the album, its stats and its tracklist."""

    name: str
    artist: str = ""
    listeners: int = 0
    playcount: int = 0
    url: str | None = None
    tracks: list[AlbumTrack] = Field(default_factory=list)

    @field_validator("listeners", "playcount", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> object:
        return _as_int(value)

    @field_validator("tracks", mode="before")
    @classmethod
    def _flatten(cls, value: object) -> object:
        if isinstance(value, dict):
            return _wrap_list(value.get("track"))
        return _wrap_list(value)


class AlbumInfoResponse(BaseModel):
    album: AlbumInfo


class Tag(BaseModel):
    name: str


class TopTags(BaseModel):
    tag: list[Tag] = Field(default_factory=list)

    @field_validator("tag", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class TopTagsResponse(BaseModel):
    toptags: TopTags


class Track(BaseModel):
    """One row of an artist.getTopTracks response."""

    name: str
    playcount: int = 0

    @field_validator("playcount", mode="before")
    @classmethod
    def _coerce_playcount(cls, value: object) -> object:
        return _as_int(value)


class TopTracks(BaseModel):
    track: list[Track] = Field(default_factory=list)

    @field_validator("track", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class TopTracksResponse(BaseModel):
    toptracks: TopTracks


class TagArtist(BaseModel):
    """One row of a tag.getTopArtists response.

    There is no count on these rows: the API used to send one per tag and stopped, so the only
    ordering is the order the API returns them in (which is its ranking for the tag).
    """

    name: str


class TagArtists(BaseModel):
    artist: list[TagArtist] = Field(default_factory=list)

    @field_validator("artist", mode="before")
    @classmethod
    def _wrap(cls, value: object) -> object:
        return _wrap_list(value)


class TagArtistsResponse(BaseModel):
    topartists: TagArtists


class _Stats(BaseModel):
    listeners: int = 0
    playcount: int = 0

    @field_validator("listeners", "playcount", mode="before")
    @classmethod
    def _coerce_int(cls, value: object) -> object:
        return _as_int(value)


class _Bio(BaseModel):
    summary: str = ""


class ArtistInfo(BaseModel):
    """The `artist` object of an artist.getInfo response (the fields lfm shows)."""

    name: str
    url: str | None = None
    stats: _Stats = Field(default_factory=_Stats)
    bio: _Bio = Field(default_factory=_Bio)


class InfoResponse(BaseModel):
    artist: ArtistInfo
