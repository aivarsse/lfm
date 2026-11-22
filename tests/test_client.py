import asyncio
from urllib.parse import parse_qs

import pytest
import respx
from httpx import Response

from lfm.api.client import (
    BASE_URL,
    MAX_CONCURRENT_REQUESTS,
    MAX_SCROBBLE_BATCH,
    ArtistNotFound,
    BadSignature,
    LastfmClient,
    LastfmError,
    NeedsAuth,
    Scrobbler,
    TemporaryError,
    signature,
)
from lfm.api.models import Track
from lfm.playback.scrobble import Play
from lfm.store.cache import Cache, cache_key


def client(cache: Cache | None = None, refresh: bool = False) -> LastfmClient:
    return LastfmClient("test-key", cache, refresh=refresh)


@respx.mock
async def test_similar_parses_match_strings_and_correction(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )
    resp = await client().similar("burial")
    assert [a.name for a in resp.similarartists.artist] == ["Kode9", "Zomby", "Darkstar"]
    assert [a.match for a in resp.similarartists.artist] == [1.0, 0.872, 0.738]
    assert resp.similarartists.attr is not None
    assert resp.similarartists.attr.artist == "Burial"


@respx.mock
async def test_similar_wraps_single_object_response(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_single.json"))
    )
    resp = await client().similar("Burial")
    artists = resp.similarartists.artist
    assert isinstance(artists, list)
    assert len(artists) == 1
    assert artists[0].name == "Kode9"
    assert artists[0].match == 0.91


@respx.mock
async def test_similar_without_results_gives_empty_list():
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json={"similarartists": {"artist": None, "@attr": {}}})
    )
    resp = await client().similar("nobody")
    assert resp.similarartists.artist == []


@respx.mock
async def test_error_6_raises_artist_not_found(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(404, json=fixture_json("not_found.json"))
    )
    with pytest.raises(ArtistNotFound, match="could not be found"):
        await client().similar("burl")


@respx.mock
async def test_server_error_raises_lastfm_error():
    respx.get(BASE_URL).mock(return_value=Response(503, text="busy"))
    with pytest.raises(LastfmError, match="HTTP 503"):
        await client().similar("Burial")


@respx.mock
async def test_retries_a_server_error_then_succeeds(fixture_json):
    route = respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        side_effect=[
            Response(503, text="busy"),
            Response(200, json=fixture_json("similar_burial.json")),
        ]
    )
    resp = await client().similar("burial")
    assert route.call_count == 2
    assert [a.name for a in resp.similarartists.artist] == ["Kode9", "Zomby", "Darkstar"]


@respx.mock
async def test_retries_rate_limiting_then_succeeds(fixture_json):
    route = respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        side_effect=[
            Response(429, json={"error": 29, "message": "Rate limit exceeded"}),
            Response(200, json=fixture_json("similar_burial.json")),
        ]
    )
    await client().similar("burial")
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_three_attempts():
    route = respx.get(BASE_URL).mock(return_value=Response(503, text="busy"))
    with pytest.raises(LastfmError):
        await client().similar("Burial")
    assert route.call_count == 3


@respx.mock
async def test_missing_artist_is_not_retried(fixture_json):
    route = respx.get(BASE_URL).mock(
        return_value=Response(404, json=fixture_json("not_found.json"))
    )
    with pytest.raises(ArtistNotFound):
        await client().similar("burl")
    assert route.call_count == 1


@respx.mock
async def test_second_call_is_served_from_cache(fixture_json, tmp_path):
    route = respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )
    with Cache(tmp_path / "cache.db") as cache:
        c = client(cache)
        first = await c.similar("burial")
        second = await c.similar("burial")
    assert route.call_count == 1
    assert first == second


@respx.mock
async def test_refresh_bypasses_cache(fixture_json, tmp_path):
    route = respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_burial.json"))
    )
    with Cache(tmp_path / "cache.db") as cache:
        await client(cache).similar("burial")  # warm the cache
        await client(cache, refresh=True).similar("burial")  # bypass it
    assert route.call_count == 2


@respx.mock
async def test_search_returns_names(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.search"}).mock(
        return_value=Response(200, json=fixture_json("search_burl.json"))
    )
    names = await client().search("burl")
    assert names == ["Burial", "Burl Ives"]


@respx.mock
async def test_info_parses_stats_and_bio(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.getinfo"}).mock(
        return_value=Response(200, json=fixture_json("info_burial.json"))
    )
    info = await client().info("Burial")
    assert info.name == "Burial"
    assert info.stats.listeners == 1642253
    assert info.stats.playcount == 58204291
    assert "William Bevan" in info.bio.summary


@respx.mock
async def test_top_tags_returns_names(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.gettoptags"}).mock(
        return_value=Response(200, json=fixture_json("toptags_burial.json"))
    )
    assert await client().top_tags("Burial") == ["dubstep", "uk garage", "electronic"]


@respx.mock
async def test_top_tags_wraps_single_object():
    respx.get(BASE_URL, params__contains={"method": "artist.gettoptags"}).mock(
        return_value=Response(200, json={"toptags": {"tag": {"name": "dubstep", "count": 100}}})
    )
    assert await client().top_tags("Burial") == ["dubstep"]


@respx.mock
async def test_top_tracks_returns_name_and_playcount(fixture_json):
    route = respx.get(BASE_URL, params__contains={"method": "artist.gettoptracks"}).mock(
        return_value=Response(200, json=fixture_json("toptracks_burial.json"))
    )
    tracks = await client().top_tracks("Burial")
    assert route.calls.last.request.url.params["limit"] == "20"
    assert [t.name for t in tracks] == ["Archangel", "Etched Headplate", "Near Dark"]
    assert tracks[0].playcount == 1812345


@respx.mock
async def test_tag_top_artists_returns_names(fixture_json):
    """Recorded live: these rows carry no count any more, only the API's own order."""
    respx.get(BASE_URL, params__contains={"method": "tag.gettopartists"}).mock(
        return_value=Response(200, json=fixture_json("tag_top_artists_dubstep.json"))
    )
    artists = await client().tag_top_artists("dubstep")
    assert [a.name for a in artists] == ["Skrillex", "The Weeknd"]


@respx.mock
async def test_a_corrupt_cache_row_is_treated_as_a_miss(tmp_path):
    route = respx.get(BASE_URL, params__contains={"method": "artist.getsimilar"}).mock(
        return_value=Response(200, json={"similarartists": {"artist": []}})
    )
    params = {"artist": "Burial", "autocorrect": "1", "limit": "100"}
    with Cache(tmp_path / "cache.db") as cache:
        cache.set(cache_key("artist.getsimilar", params), "{not json at all", ttl=60)
        await client(cache).similar("Burial", limit=100)
    assert route.call_count == 1  # it fetched instead of trusting the row


@respx.mock
async def test_a_null_body_is_reported_not_traced(tmp_path):
    respx.get(BASE_URL).mock(return_value=Response(200, content=b"null"))
    with Cache(tmp_path / "cache.db") as cache:
        with pytest.raises(LastfmError, match="unexpected response"):
            await client(cache).similar("Burial")


@respx.mock
async def test_a_changed_shape_is_reported_not_traced(tmp_path):
    respx.get(BASE_URL).mock(return_value=Response(200, json={"similarartists": "oops"}))
    with Cache(tmp_path / "cache.db") as cache:
        with pytest.raises(LastfmError, match="unexpected Last.fm response"):
            await client(cache).similar("Burial")


@respx.mock
async def test_error_6_is_only_not_found_when_it_says_so(tmp_path):
    """Last.fm documents 6 as "Invalid parameters"; real not-found responses reuse it."""
    respx.get(BASE_URL).mock(
        return_value=Response(200, json={"error": 6, "message": "Invalid artist parameter"})
    )
    with Cache(tmp_path / "cache.db") as cache:
        with pytest.raises(LastfmError) as caught:
            await client(cache).similar("Burial")
    assert not isinstance(caught.value, ArtistNotFound)
    assert "Last.fm error 6" in str(caught.value)


@respx.mock
async def test_a_temporary_error_is_retried(tmp_path):
    route = respx.get(BASE_URL).mock(
        return_value=Response(200, json={"error": 16, "message": "There was a temporary error"})
    )
    with Cache(tmp_path / "cache.db") as cache:
        with pytest.raises(TemporaryError):
            await client(cache).similar("Burial")
    assert route.call_count == 3  # the documented "please try again" is honoured


@respx.mock
async def test_a_burst_of_requests_is_capped():
    """--why fans out over every row; the cap keeps the burst under the rate limit."""
    in_flight = 0
    peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return Response(200, json={"toptags": {"tag": []}})

    respx.get(BASE_URL).mock(side_effect=handler)
    client = LastfmClient("test-key")
    await asyncio.gather(*(client.top_tags(f"Artist {i}") for i in range(8)))
    assert peak <= MAX_CONCURRENT_REQUESTS


def test_a_float_where_an_int_belongs_is_accepted():
    assert Track.model_validate({"name": "x", "playcount": "1234.5"}).playcount == 1234
    assert Track.model_validate({"name": "x", "playcount": "1234"}).playcount == 1234


@respx.mock
async def test_similar_tracks_unwrap_the_artist_and_coerce_the_match(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "track.getsimilar"}).mock(
        return_value=Response(200, json=fixture_json("similar_tracks_burial.json"))
    )
    results = await client().similar_tracks("Burial", "Archangel")
    assert [(r.artist, r.name, r.match) for r in results][:2] == [
        ("Burial", "Archangel", 1.0),
        ("Burial", "Etched Headplate", 0.87),
    ]
    assert results[2].artist == "Jamie Woon"


@respx.mock
async def test_top_albums_parses_the_list(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "artist.gettopalbums"}).mock(
        return_value=Response(200, json=fixture_json("topalbums_burial.json"))
    )
    albums = await client().top_albums("Burial")
    assert [(a.name, a.playcount) for a in albums] == [
        ("Untrue", 95000),
        ("Kindred", 40000),
    ]


@respx.mock
async def test_album_info_brings_dates_and_durations(fixture_json):
    respx.get(BASE_URL, params__contains={"method": "album.getinfo"}).mock(
        return_value=Response(200, json=fixture_json("album_untrue.json"))
    )
    info = await client().album_info("Burial", "Untrue")
    assert (info.name, info.listeners) == ("Untrue", 1200000)
    assert [(t.rank, t.name, t.duration) for t in info.tracks] == [
        (1, "Untitled", 75),
        (2, "Archangel", 239),
        (0, "Near Dark", 0),  # no @attr and no duration: honest zeros, no crash
    ]


@respx.mock
async def test_an_artist_search_is_never_answered_from_the_cache(tmp_path):
    """A transient empty answer used to sit in the cache for a week, and with an empty
    answer the prompt suggests nothing at all — which is what "it does not work" looks
    like when there is no error to show."""
    empty = {"results": {"artistmatches": {"artist": []}}}
    full = {"results": {"artistmatches": {"artist": [{"name": "Burial"}]}}}
    route = respx.get(BASE_URL, params__contains={"method": "artist.search"}).mock(
        side_effect=[Response(200, json=empty), Response(200, json=full)]
    )
    with Cache(tmp_path / "responses.db") as cache:  # closed, or sqlite warns at exit
        searcher = LastfmClient("test-key", cache)
        assert await searcher.search("bur") == []
        assert await searcher.search("bur") == ["Burial"]
    assert route.call_count == 2  # the first answer was not kept


# --- the write half: signing, sending, and the answers ----------------------


def scrobbler() -> Scrobbler:
    return Scrobbler("test-key", "test-secret", "test-session")


def an_entry(track: str = "Archangel", code: str = "0") -> dict:
    """One scrobble in the shape the docs describe, JSON-ified as Last.fm does."""
    return {
        "artist": {"corrected": "0", "#text": "Burial"},
        "track": {"corrected": "0", "#text": track},
        "album": {"corrected": "0", "#text": "Untrue"},
        "albumArtist": {"corrected": "0", "#text": ""},
        "timestamp": {"#text": "1700000000"},
        "ignoredMessage": {"code": code, "#text": ""},
    }


def scrobble_response(*entries: object, accepted: int | None = None, ignored: int = 0) -> dict:
    """A track.scrobble answer. Entries are taken as given, so a test can put
    something that is not a scrobble in the list and check it is skipped."""
    payload: dict = {
        "@attr": {
            "accepted": str(len(entries) if accepted is None else accepted),
            "ignored": str(ignored),
        }
    }
    if entries:
        payload["scrobble"] = list(entries) if len(entries) > 1 else entries[0]
    return {"scrobbles": payload}


def test_the_signature_matches_the_auth_specs_own_example():
    """The spec's worked example: three parameters and the secret "ilovecher".

    The expected hash was computed from the spec's concatenated string
    (`api_keyxxxxxxxxxxmethodauth.getSessiontokenyyyyyy` + `ilovecher`) outside
    this code, so it catches a sort, separator or encoding mistake here.
    """
    params = {"api_key": "xxxxxxxxxx", "method": "auth.getSession", "token": "yyyyyy"}
    assert signature(params, "ilovecher") == "b87d61da3cda91a8b6746c4aef55d6f8"


def test_format_and_callback_are_never_signed():
    params = {"api_key": "k", "method": "track.scrobble"}
    assert signature({**params, "format": "json"}, "s") == signature(params, "s")
    assert signature({**params, "callback": "https://x"}, "s") == signature(params, "s")


def test_the_signature_sorts_names_byte_wise_so_artist_10_comes_first():
    """The scrobble doc warns about this by name, so it is pinned by name.

    The order is: api_key, artist[10], artist[1], artist[2], method — because
    `[10]` beats `[1]` on the byte '0' against ']'. Written out as a string so
    the order is visible rather than something to derive from the code.
    """
    params = {
        "method": "track.scrobble",
        "api_key": "k",
        "artist[1]": "one",
        "artist[2]": "two",
        "artist[10]": "ten",
    }
    assert signature(params, "s") == "52b582b30d3d8424bc8e523c15172011"


@respx.mock
async def test_scrobbling_posts_signed_form_parameters():
    route = respx.post(BASE_URL).mock(
        return_value=Response(200, json=scrobble_response(an_entry()))
    )
    outcomes = await scrobbler().scrobble(
        [Play("Burial", "Archangel", 1_700_000_000, "Untrue", 239)]
    )

    sent = route.calls[0].request
    assert sent.method == "POST"
    body = {name: values[0] for name, values in parse_qs(sent.content.decode()).items()}
    assert body["method"] == "track.scrobble"
    assert body["api_key"] == "test-key"
    assert body["sk"] == "test-session"
    assert (body["artist[0]"], body["track[0]"]) == ("Burial", "Archangel")
    assert body["timestamp[0]"] == "1700000000"  # when it started, not when it is sent
    assert body["album[0]"] == "Untrue"
    assert body["duration[0]"] == "239"
    # The signature has to cover exactly what went on the wire: a parameter that
    # is sent but not signed (or the other way round) is error 13 at Last.fm.
    covered = {name: value for name, value in body.items() if name != "api_sig"}
    assert body["api_sig"] == signature(covered, "test-secret")
    assert outcomes[0].accepted


@respx.mock
async def test_a_play_without_an_album_sends_no_album_parameter():
    route = respx.post(BASE_URL).mock(
        return_value=Response(200, json=scrobble_response(an_entry()))
    )
    await scrobbler().scrobble([Play("Burial", "Archangel", 1_700_000_000)])
    body = parse_qs(route.calls[0].request.content.decode())
    assert "album[0]" not in body
    assert "artist[0]" in body


@respx.mock
async def test_a_batch_keeps_the_indexes_with_their_own_play():
    route = respx.post(BASE_URL).mock(
        return_value=Response(
            200, json=scrobble_response(an_entry("Archangel"), an_entry("Etched Headplate"))
        )
    )
    plays = [Play("Burial", "Archangel", 1), Play("Burial", "Etched Headplate", 2)]
    outcomes = await scrobbler().scrobble(plays)
    body = {
        name: values[0]
        for name, values in parse_qs(route.calls[0].request.content.decode()).items()
    }
    assert (body["track[0]"], body["track[1]"]) == ("Archangel", "Etched Headplate")
    assert (body["timestamp[0]"], body["timestamp[1]"]) == ("1", "2")
    assert [o.track for o in outcomes] == ["Archangel", "Etched Headplate"]


async def test_scrobbling_more_than_the_specs_batch_limit_is_refused_up_front():
    plays = [Play("Burial", f"track {i}", i) for i in range(MAX_SCROBBLE_BATCH + 1)]
    with pytest.raises(ValueError):
        await scrobbler().scrobble(plays)


@respx.mock
async def test_a_refused_scrobble_keeps_the_reason_last_fm_gave():
    route = respx.post(BASE_URL).mock(
        return_value=Response(
            200, json=scrobble_response(an_entry(code="3"), accepted=0, ignored=1)
        )
    )
    outcomes = await scrobbler().scrobble([Play("Burial", "Near Dark", 1)])
    assert route.called
    assert outcomes[0].ignored == 3  # timestamp too old
    assert not outcomes[0].accepted


@respx.mock
async def test_an_answer_without_per_scrobble_detail_falls_back_to_the_counts():
    """Last.fm has changed response shapes before: the batch totals still say
    what happened, and losing the detail is better than re-posting the batch."""
    route = respx.post(BASE_URL).mock(
        return_value=Response(200, json={"scrobbles": {"@attr": {"accepted": "1", "ignored": "0"}}})
    )
    outcomes = await scrobbler().scrobble([Play("Burial", "Archangel", 1)])
    assert route.called
    assert [o.accepted for o in outcomes] == [True]
    assert outcomes[0].track == "Archangel"  # taken from what was sent


@respx.mock
async def test_an_unintelligible_answer_yields_no_outcome_rather_than_a_wrong_one():
    route = respx.post(BASE_URL).mock(return_value=Response(200, json={"scrobbles": {}}))
    outcomes = await scrobbler().scrobble([Play("Burial", "Archangel", 1)])
    assert route.called
    assert outcomes == []


@respx.mock
async def test_a_stale_session_key_says_to_authorise_again():
    respx.post(BASE_URL).mock(
        return_value=Response(400, json={"error": 9, "message": "Invalid session key"})
    )
    with pytest.raises(NeedsAuth, match="lfm auth"):
        await scrobbler().scrobble([Play("Burial", "Archangel", 1)])


@respx.mock
async def test_a_signature_last_fm_rejects_points_at_the_secret():
    respx.post(BASE_URL).mock(
        return_value=Response(
            400, json={"error": 13, "message": "Invalid method signature supplied"}
        )
    )
    with pytest.raises(BadSignature, match="same Last.fm API account"):
        await scrobbler().scrobble([Play("Burial", "Archangel", 1)])


@respx.mock
async def test_now_playing_posts_the_track_and_its_duration():
    route = respx.post(BASE_URL).mock(return_value=Response(200, json={}))
    await scrobbler().now_playing("Burial", "Archangel", "Untrue", 239)
    body = {
        name: values[0]
        for name, values in parse_qs(route.calls[0].request.content.decode()).items()
    }
    assert body["method"] == "track.updateNowPlaying"
    assert (body["artist"], body["track"], body["album"], body["duration"]) == (
        "Burial",
        "Archangel",
        "Untrue",
        "239",
    )


@respx.mock
async def test_the_auth_calls_read_the_token_and_the_session_they_need():
    """Neither carries a session key: there is not one yet, which is the point."""
    route = respx.post(BASE_URL).mock(
        side_effect=[
            Response(200, json={"token": "a-token"}),
            Response(
                200, json={"session": {"name": "someone", "key": "a-session", "subscriber": "0"}}
            ),
        ]
    )
    without_session = Scrobbler("test-key", "test-secret", "")
    assert await without_session.auth_token() == "a-token"
    assert await without_session.auth_session("a-token") == ("a-session", "someone")
    for call in route.calls:
        body = parse_qs(call.request.content.decode())
        assert "sk" not in body  # nothing to carry, and an empty sk would be signed
        assert "api_sig" in body


async def test_scrobbling_nothing_asks_last_fm_for_nothing():
    assert await scrobbler().scrobble([]) == []


@respx.mock
async def test_a_response_whose_fields_are_plain_strings_is_still_read():
    """The docs' samples are XML; their JSON is not always nested the same way."""
    respx.post(BASE_URL).mock(
        return_value=Response(
            200,
            json=scrobble_response(
                {
                    "artist": "Burial",
                    "track": "Archangel",
                    "album": "Untrue",
                    "timestamp": "1700000000",
                    "ignoredMessage": {"code": "0"},
                }
            ),
        )
    )
    outcomes = await scrobbler().scrobble([Play("Burial", "Archangel", 1_700_000_000)])
    assert [(o.artist, o.track, o.accepted) for o in outcomes] == [("Burial", "Archangel", True)]


@respx.mock
async def test_a_corrected_name_is_noted_as_corrected():
    entry = an_entry()
    entry["artist"] = {"corrected": "1", "#text": "Burial"}
    respx.post(BASE_URL).mock(return_value=Response(200, json=scrobble_response(entry)))
    outcomes = await scrobbler().scrobble([Play("burial", "Archangel", 1)])
    assert outcomes[0].corrected


@respx.mock
async def test_a_nonsense_ignored_code_reads_as_taken_rather_than_refused():
    route = respx.post(BASE_URL).mock(
        return_value=Response(
            200,
            json=scrobble_response(
                {**an_entry(), "ignoredMessage": {"code": "not a number"}}, accepted=1
            ),
        )
    )
    outcomes = await scrobbler().scrobble([Play("Burial", "Archangel", 1)])
    assert route.called
    assert outcomes[0].accepted


@respx.mock
async def test_a_nonsense_batch_count_is_not_used_to_guess_anything():
    route = respx.post(BASE_URL).mock(
        return_value=Response(
            200, json={"scrobbles": {"@attr": {"accepted": "lots", "ignored": "-"}}}
        )
    )
    outcomes = await scrobbler().scrobble([Play("Burial", "Archangel", 1)])
    assert route.called
    assert outcomes == []  # nothing is assumed about a play nobody accounted for


@respx.mock
async def test_an_answer_that_lists_something_that_is_not_a_scrobble_ignores_it():
    route = respx.post(BASE_URL).mock(
        return_value=Response(
            200, json=scrobble_response("nonsense", an_entry("Archangel"), accepted=1)
        )
    )
    outcomes = await scrobbler().scrobble([Play("Burial", "Archangel", 1)])
    assert route.called
    assert [o.track for o in outcomes] == ["Archangel"]


@respx.mock
async def test_the_auth_calls_refuse_to_invent_what_last_fm_did_not_send():
    respx.post(BASE_URL).mock(side_effect=[Response(200, json={}), Response(200, json={})])
    without_session = Scrobbler("test-key", "test-secret", "")
    with pytest.raises(LastfmError, match="no token"):
        await without_session.auth_token()
    with pytest.raises(LastfmError, match="no session key"):
        await without_session.auth_session("a-token")
