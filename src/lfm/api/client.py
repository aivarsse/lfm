"""Async Last.fm client. The only module that talks to the network.

The reads below are anonymous, cached and retried freely, because the worst a
repeat can do is fetch the same page twice. The writes at the bottom of the file
are the opposite in every respect: signed with the account's secret, carrying a
session key, uncached, and retried only when Last.fm itself says the failure was
temporary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
import stamina
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:  # the write path's unit of work; see scrobble.py
    from ..playback.scrobble import Play

from ..store.cache import Cache, cache_key
from .models import (
    Album,
    AlbumInfo,
    AlbumInfoResponse,
    ArtistInfo,
    InfoResponse,
    SearchArtist,
    SearchResponse,
    SimilarResponse,
    SimilarTrack,
    SimilarTracksResponse,
    TagArtist,
    TagArtistsResponse,
    TopAlbumsResponse,
    TopTagsResponse,
    TopTracksResponse,
    Track,
)

BASE_URL = "https://ws.audioscrobbler.com/2.0/"

# Methods whose answers are about what you are *typing*, not about the catalogue, and so
# are not written to the cache at all. A transient empty `artist.search` sat there for a
# week, and the prompt's suggestions were off with nothing on screen to say why. The
# suggester keeps its own cache for the length of a session.
UNCACHED = {"artist.search"}

DEFAULT_TTL = 7 * 86400
# Cache TTLs per method, in seconds. 30 days for the browsing-style calls,
# 7 days (the default) for the slower-moving info.
TTL = {
    "artist.getsimilar": 30 * 86400,
    "track.getsimilar": 30 * 86400,
    "artist.gettopalbums": 30 * 86400,
    "album.getinfo": 30 * 86400,
    "artist.gettoptags": 30 * 86400,
    "artist.gettoptracks": 30 * 86400,
    "tag.gettopartists": 30 * 86400,
}


class LastfmError(Exception):
    """Last.fm answered with an error."""


class ArtistNotFound(LastfmError):
    """Last.fm error 6: the artist could not be found or autocorrected."""


class RateLimited(LastfmError):
    """Last.fm error 29: too many requests. Worth retrying."""


class TemporaryError(LastfmError):
    """Last.fm error 11 or 16: "temporarily offline" / "temporary error. Please try again"."""


class ServerError(LastfmError):
    """Last.fm answered 5xx. Worth retrying."""


class NeedsAuth(LastfmError):
    """Last.fm error 9: the session key is not valid, or no longer is.

    Only a person can clear this one, by authorising lfm again, so it is kept
    apart from the failures a retry might fix.
    """


class BadSignature(LastfmError):
    """Last.fm error 13: the signature does not match the secret.

    In practice this is a secret and an api_key from two different Last.fm API
    accounts, so the message says so rather than leaving "invalid signature" to
    be worked out from scratch.
    """


# Transport hiccups plus the two failures a retry can actually fix.
RETRYABLE = (httpx.TransportError, RateLimited, TemporaryError, ServerError)
# Last.fm rate limits (error 29) at about five requests a second, and `--why`
# fans out over every result row at once. A small cap keeps a burst under the
# limit without serialising the command; the retry above covers what slips past.
MAX_CONCURRENT_REQUESTS = 4
# How many listener lookups one deep-cut run may spend. Those calls are the
# expensive part of the feature, so they are only made for the rows about to be
# shown and never more than this many.
MAX_LISTENER_LOOKUPS = 30
RETRY_ATTEMPTS = 3


def _parse(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 500:
        raise ServerError(f"Last.fm is unavailable (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except ValueError:
        raise LastfmError(f"Last.fm returned non-JSON (HTTP {resp.status_code})") from None
    if not isinstance(data, dict):
        raise LastfmError("Last.fm returned an unexpected response")
    if "error" in data:
        code = data["error"]
        message = data.get("message", "unknown error")
        if code == 6:
            # The docs call 6 "Invalid parameters", but real responses use it for
            # an unknown artist with that wording; anything else is a real error.
            if "could not be found" in str(message).lower():
                raise ArtistNotFound(str(message))
            raise LastfmError(f"Last.fm error 6: {message}")
        if code in (11, 16):
            # 11 "temporarily offline. Try again later.", 16 "temporary error
            # processing your request. Please try again" - both mean retry.
            raise TemporaryError(f"Last.fm error {code}: {message}")
        if code == 29:
            raise RateLimited(str(message))
        if code == 9:
            # The session key is stale or was never authorised: only the owner
            # can fix it, by going through the browser flow again.
            raise NeedsAuth(f"Last.fm error 9: {message} — run `lfm auth` again")
        if code == 13:
            raise BadSignature(
                f"Last.fm error 13: {message} — the secret and the api_key have to come from "
                "the same Last.fm API account"
            )
        raise LastfmError(f"Last.fm error {code}: {message}")
    return data


_Model = TypeVar("_Model", bound=BaseModel)


def _model(model: type[_Model], data: dict[str, Any]) -> _Model:
    """Parse a response; an unexpected shape becomes a Last.fm error, not a traceback."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise LastfmError(f"unexpected Last.fm response ({exc.error_count()} problems)") from None


class LastfmClient:
    def __init__(
        self,
        api_key: str,
        cache: Cache | None = None,
        *,
        refresh: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.cache = cache
        self.refresh = refresh
        self.timeout = timeout
        self._gate = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _get(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        key = cache_key(method, params)
        if self.cache is not None and not self.refresh and method not in UNCACHED:
            cached = self.cache.get(key)
            if cached is not None:
                try:
                    return json.loads(cached)
                except ValueError:
                    pass  # a corrupt row is just a miss; the fetch below overwrites it

        query = {"method": method, "api_key": self.api_key, "format": "json", **params}
        data = await self._fetch(query)
        if self.cache is not None and method not in UNCACHED:
            self.cache.set(key, json.dumps(data), TTL.get(method, DEFAULT_TTL))
        return data

    @stamina.retry(on=RETRYABLE, attempts=RETRY_ATTEMPTS, wait_initial=0.5, wait_max=8.0)
    async def _fetch(self, query: dict[str, str]) -> dict[str, Any]:
        async with self._gate:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.get(BASE_URL, params=query)
        return _parse(resp)

    async def similar(self, artist: str, limit: int = 100) -> SimilarResponse:
        data = await self._get(
            "artist.getsimilar", {"artist": artist, "autocorrect": "1", "limit": str(limit)}
        )
        return _model(SimilarResponse, data)

    async def similar_tracks(self, artist: str, track: str, limit: int = 30) -> list[SimilarTrack]:
        """Tracks close to this one — the material a song radio is built from."""
        data = await self._get(
            "track.getsimilar",
            {"artist": artist, "track": track, "autocorrect": "1", "limit": str(limit)},
        )
        return _model(SimilarTracksResponse, data).similartracks.track[:limit]

    async def top_albums(self, artist: str, limit: int = 30) -> list[Album]:
        """The artist's albums by popularity."""
        data = await self._get(
            "artist.gettopalbums",
            {"artist": artist, "autocorrect": "1", "limit": str(limit)},
        )
        return _model(TopAlbumsResponse, data).topalbums.album[:limit]

    async def album_info(self, artist: str, album: str) -> AlbumInfo:
        """One album: dates, stats and its tracklist, durations included."""
        data = await self._get(
            "album.getinfo", {"artist": artist, "album": album, "autocorrect": "1"}
        )
        return _model(AlbumInfoResponse, data).album

    async def search_matches(self, artist: str, limit: int = 5) -> list[SearchArtist]:
        """The raw ranked hits, listeners included.

        `artist.search` is a relevance search, not a prefix index: it happily returns artists
        whose name merely *contains* the query, and it echoes the query back as an artist of its
        own for most prefixes. Listeners are what tell those apart from the real act, so the
        prompt's completion needs them.
        """
        data = await self._get("artist.search", {"artist": artist, "limit": str(limit)})
        return _model(SearchResponse, data).results.artistmatches.artist

    async def search(self, artist: str, limit: int = 5) -> list[str]:
        return [a.name for a in await self.search_matches(artist, limit)]

    async def info(self, artist: str) -> ArtistInfo:
        data = await self._get("artist.getinfo", {"artist": artist, "autocorrect": "1"})
        return _model(InfoResponse, data).artist

    async def top_tags(self, artist: str, limit: int = 10) -> list[str]:
        data = await self._get("artist.gettoptags", {"artist": artist, "autocorrect": "1"})
        return [t.name for t in _model(TopTagsResponse, data).toptags.tag][:limit]

    async def top_tracks(self, artist: str, limit: int = 20) -> list[Track]:
        data = await self._get(
            "artist.gettoptracks",
            {"artist": artist, "autocorrect": "1", "limit": str(limit)},
        )
        return _model(TopTracksResponse, data).toptracks.track[:limit]

    async def tag_top_artists(self, tag: str, limit: int = 30) -> list[TagArtist]:
        data = await self._get("tag.gettopartists", {"tag": tag, "limit": str(limit)})
        return _model(TagArtistsResponse, data).topartists.artist[:limit]


# ---------------------------------------------------------------------------
# Writes: the scrobbling half. Nothing above this line changes anything on
# Last.fm; everything below it does.
# ---------------------------------------------------------------------------

# The spec's batch limit, and why there are batches at all: fifty plays in one
# signed request instead of fifty requests.
MAX_SCROBBLE_BATCH = 50

# The two parameters the auth spec says are never signed.
_UNSIGNED = {"format", "callback"}

# What is worth retrying for a write. httpx's transport errors are deliberately
# absent: a request that died on the way out may still have been accepted, and
# repeating it would put the same play on the profile twice. Ambiguous failures
# stay in the outbox instead, for the next drain — and a person — to see.
RETRYABLE_WRITE = (RateLimited, TemporaryError, ServerError)

# The ignoredMessage code used when Last.fm answers with the batch counts but no
# per-scrobble detail: refused, reason unstated.
UNKNOWN_IGNORE = -1


def signature(params: Mapping[str, str], secret: str) -> str:
    """The api_sig for one call: md5 over the parameters, the secret appended.

    The auth spec's scheme — every parameter as `<name><value>`, sorted by name,
    then the secret, then hashed. `format` and `callback` are never included.

    The sort is byte-wise on the name, which is precisely what puts `artist[10]`
    before `artist[1]` once a batch reaches two-digit indexes; the scrobble
    documentation names that as a trap, so it is done in one place, here.
    """
    ordered = sorted((name, value) for name, value in params.items() if name not in _UNSIGNED)
    joined = "".join(f"{name}{value}" for name, value in ordered)
    return hashlib.md5((joined + secret).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ScrobbleOutcome:
    """What Last.fm made of one scrobble in a batch."""

    artist: str = ""
    track: str = ""
    album: str = ""
    ignored: int = 0
    corrected: bool = False

    @property
    def accepted(self) -> bool:
        """Was it taken? Every ignoredMessage code but 0 means no."""
        return self.ignored == 0


def _text(entry: Mapping[str, Any], name: str) -> tuple[str, bool]:
    """One correctable field of a scrobble answer: (value, was it corrected)."""
    value = entry.get(name)
    if isinstance(value, Mapping):
        return str(value.get("#text", "")), str(value.get("corrected", "0")) == "1"
    return ("" if value is None else str(value)), False


def _counts(data: Mapping[str, Any]) -> tuple[int, int]:
    """The batch's own accepted/ignored totals, or (-1, -1) if it did not say."""
    scrobbles = data.get("scrobbles")
    attr = scrobbles.get("@attr") if isinstance(scrobbles, Mapping) else None
    if not isinstance(attr, Mapping):
        return -1, -1
    try:
        accepted = int(str(attr.get("accepted", "")).strip() or -1)
        ignored = int(str(attr.get("ignored", "")).strip() or -1)
    except ValueError:
        return -1, -1
    return accepted, ignored


def _scrobble_outcomes(data: Mapping[str, Any], plays: Sequence[Play]) -> list[ScrobbleOutcome]:
    """One outcome per play sent, from the per-scrobble entries where possible.

    The documented answer carries one entry per scrobble, with the corrected
    names and an ignoredMessage code. If those entries are missing, or there are
    not as many of them as there were plays — this shape has changed before —
    the batch's own counts decide the fate of each play in order, so that a good
    scrobble is not thrown away (and posted a second time later) over a parsing
    change. Only when neither is usable does this return nothing, which the
    caller treats as "unknown, keep it queued".
    """
    scrobbles = data.get("scrobbles")
    raw = scrobbles.get("scrobble") if isinstance(scrobbles, Mapping) else None
    entries = raw if isinstance(raw, list) else ([raw] if isinstance(raw, Mapping) else [])

    outcomes = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        artist, artist_fixed = _text(entry, "artist")
        track, track_fixed = _text(entry, "track")
        album, album_fixed = _text(entry, "album")
        ignored = entry.get("ignoredMessage")
        code = 0
        if isinstance(ignored, Mapping):
            try:
                code = int(str(ignored.get("code", 0)).strip() or 0)
            except ValueError:
                code = 0
        outcomes.append(
            ScrobbleOutcome(
                artist=artist,
                track=track,
                album=album,
                ignored=code,
                corrected=artist_fixed or track_fixed or album_fixed,
            )
        )
    if len(outcomes) == len(plays):
        return outcomes

    accepted, ignored = _counts(data)
    if accepted < 0 or accepted + ignored != len(plays):
        return []
    return [
        ScrobbleOutcome(
            artist=play.artist,
            track=play.track,
            album=play.album,
            ignored=0 if index < accepted else UNKNOWN_IGNORE,
        )
        for index, play in enumerate(plays)
    ]


class Scrobbler:
    """The write half of the client: signed POSTs to the scrobbling methods.

    Reads are anonymous and cached; everything here carries the `api_sig` the
    secret produces and the `sk` that `lfm auth` stored, and nothing is cached,
    because the whole point of a scrobble is the moment it happened.
    """

    def __init__(
        self,
        api_key: str,
        secret: str,
        session_key: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.secret = secret
        self.session_key = session_key
        self.timeout = timeout
        self._gate = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def _sign(self, params: Mapping[str, str]) -> dict[str, str]:
        """The parameters plus api_key, the session key and the signature over all of them."""
        signed = {**params, "api_key": self.api_key}
        if self.session_key:
            # Only when there is one: an empty `sk=` is a parameter like any
            # other and would go into the signature string, and the two auth
            # calls are made before there is a session to carry at all.
            signed["sk"] = self.session_key
        signed["api_sig"] = signature(signed, self.secret)
        return signed

    @stamina.retry(on=RETRYABLE_WRITE, attempts=RETRY_ATTEMPTS, wait_initial=0.5, wait_max=8.0)
    async def _post(self, params: Mapping[str, str]) -> dict[str, Any]:
        async with self._gate:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.post(BASE_URL, data=self._sign(params))
        return _parse(resp)

    async def auth_token(self) -> str:
        """auth.getToken: the token that goes in front of the owner in a browser.

        Signed with the secret but carrying no session key — there is nothing to
        authorise yet, which is the whole point of it.
        """
        data = await self._post({"method": "auth.getToken", "format": "json"})
        token = data.get("token")
        if not token:
            raise LastfmError("Last.fm returned no token to authorise with")
        return str(token)

    async def auth_session(self, token: str) -> tuple[str, str]:
        """auth.getSession: the session key, and the name it belongs to."""
        data = await self._post({"method": "auth.getSession", "token": token, "format": "json"})
        session = data.get("session")
        if not isinstance(session, Mapping) or not session.get("key"):
            raise LastfmError("Last.fm returned no session key")
        return str(session["key"]), str(session.get("name", ""))

    async def now_playing(
        self, artist: str, track: str, album: str = "", duration: int = 0
    ) -> None:
        """Say what is playing: best effort, never retried, per the spec."""
        params = {
            "method": "track.updateNowPlaying",
            "artist": artist,
            "track": track,
            "format": "json",
        }
        if album:
            params["album"] = album
        if duration:
            params["duration"] = str(duration)
        await self._post(params)

    async def scrobble(self, plays: Sequence[Play]) -> list[ScrobbleOutcome]:
        """Send up to MAX_SCROBBLE_BATCH plays in one signed request."""
        if not plays:
            return []
        if len(plays) > MAX_SCROBBLE_BATCH:
            raise ValueError(
                f"at most {MAX_SCROBBLE_BATCH} scrobbles per request, got {len(plays)}"
            )
        params = {"method": "track.scrobble", "format": "json"}
        for index, play in enumerate(plays):
            params[f"artist[{index}]"] = play.artist
            params[f"track[{index}]"] = play.track
            params[f"timestamp[{index}]"] = str(play.started_at)
            if play.album:
                params[f"album[{index}]"] = play.album
            if play.duration:
                params[f"duration[{index}]"] = str(play.duration)
        return _scrobble_outcomes(await self._post(params), plays)
