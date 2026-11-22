"""The scrobbling rule, the queue and the record: everything but the wire.

The rule's numbers are Last.fm's, from the Scrobbling 2.0 spec: a track is
scrobbled when it is longer than 30 seconds *and* has been played for at least
half its duration, or for 4 minutes, whichever comes first.
"""

import json

from lfm.api.client import MAX_SCROBBLE_BATCH, ScrobbleOutcome
from lfm.playback.scrobble import (
    IGNORED_REASONS,
    Drain,
    Ledger,
    Outbox,
    Play,
    Scrobbling,
    Session,
    counts_as_played,
)


def play(title: str = "Archangel", started_at: int = 1_700_000_000) -> Play:
    return Play(artist="Burial", track=title, started_at=started_at, album="Untrue", duration=239)


class FakeScrobbler:
    """Stands in for client.Scrobbler: answers a batch the way a test says."""

    def __init__(self, outcomes=None, fail: Exception | None = None) -> None:
        self.batches: list[list[Play]] = []
        self.playing: list[tuple[str, str]] = []
        self._outcomes = outcomes
        self._fail = fail

    async def scrobble(self, plays):
        self.batches.append(list(plays))
        if self._fail is not None:
            raise self._fail
        if self._outcomes is not None:
            return self._outcomes(plays)
        return [ScrobbleOutcome(artist=p.artist, track=p.track) for p in plays]

    async def now_playing(self, artist, track, album="", duration=0) -> None:
        self.playing.append((artist, track))


# --- the rule ---------------------------------------------------------------


def test_thirty_seconds_is_not_longer_than_thirty_seconds():
    assert counts_as_played(30.0, 30.0) is False
    assert counts_as_played(30.1, 30.1) is True


def test_half_the_track_or_four_minutes_whichever_comes_first():
    # Half of a three-minute track is ninety seconds.
    assert counts_as_played(180.0, 89.9) is False
    assert counts_as_played(180.0, 90.0) is True
    # Half of a twenty-minute track is ten minutes, but the ceiling wins at four.
    assert counts_as_played(1200.0, 239.9) is False
    assert counts_as_played(1200.0, 240.0) is True


def test_a_duration_nobody_knows_is_never_a_scrobble():
    """mpv reports 0 until it has looked at the file: guess nothing."""
    assert counts_as_played(0.0, 300.0) is False


# --- a track's session ------------------------------------------------------


def test_a_session_reports_a_play_once_it_counts_and_only_once():
    session = Session("Burial", "Archangel", "Untrue", started_at=1_700_000_000)
    assert session.observe(10.0, 239.0) is None  # playing, nowhere near half
    reported = session.observe(239.0, 239.0)
    assert reported is not None
    assert (reported.artist, reported.track, reported.album) == ("Burial", "Archangel", "Untrue")
    assert reported.started_at == 1_700_000_000  # the time it started, not the time it is sent
    # The bar repaints a few times a second: later readings must add nothing.
    assert session.observe(239.0, 239.0) is None
    assert session.observe(240.0, 239.0) is None


def test_a_forward_seek_counts_as_having_played():
    """The rule is read against the furthest point reached, so skipping ahead
    past halfway counts. Other clients do the same; it is a choice, so it is
    pinned here rather than left to be discovered."""
    session = Session("Burial", "Etched Headplate", started_at=0)
    assert session.observe(20.0, 239.0) is None
    assert session.observe(200.0, 239.0) is not None


def test_the_opening_of_a_session_is_what_now_playing_wants():
    session = Session("Burial", "Archangel", "Untrue", started_at=99)
    assert session.opening() == Play("Burial", "Archangel", 99, "Untrue", 0)


def test_a_session_with_an_unknown_duration_never_reports():
    session = Session("Burial", "Archangel", started_at=0)
    assert session.observe(300.0, 0.0) is None


# --- the queue --------------------------------------------------------------


def test_the_outbox_keeps_the_order_and_forgets_only_what_it_was_given(tmp_path):
    outbox = Outbox(tmp_path / "outbox.json")
    first, second = play("Archangel", 1), play("Etched Headplate", 2)
    outbox.add(first)
    outbox.add(second)
    assert [p.track for p in outbox.entries()] == ["Archangel", "Etched Headplate"]
    assert outbox.discard([first]) == 1
    assert [p.track for p in outbox.entries()] == ["Etched Headplate"]
    assert outbox.clear() == 1
    assert outbox.entries() == []


def test_the_outbox_will_not_queue_the_same_play_twice(tmp_path):
    outbox = Outbox(tmp_path / "outbox.json")
    outbox.add(play())
    outbox.add(play())  # same artist, track and start: the same play
    assert len(outbox.entries()) == 1


def test_an_unreadable_outbox_is_an_empty_queue_not_a_crash(tmp_path):
    path = tmp_path / "outbox.json"
    path.write_text("{ this was never json")
    assert Outbox(path).entries() == []


def test_an_entry_missing_a_field_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "outbox.json"
    path.write_text(
        json.dumps(
            {
                "plays": [
                    {"artist": "Burial", "track": "Archangel", "started_at": 1},
                    {"artist": "Burial"},  # hand-edited, or half-written
                ]
            }
        )
    )
    assert [p.track for p in Outbox(path).entries()] == ["Archangel"]


# --- the record of what was sent --------------------------------------------


def test_the_ledger_records_what_was_sent_and_what_last_fm_said(tmp_path):
    ledger = Ledger(tmp_path / "scrobbled.jsonl")
    ledger.record(play(), ScrobbleOutcome(artist="Burial", track="Archangel"), "accepted")
    ledger.record(play("Near Dark", 2), ScrobbleOutcome(track="Near Dark", ignored=3), "ignored")
    rows = ledger.entries()
    assert [r["status"] for r in rows] == ["accepted", "ignored"]
    assert rows[0]["track"] == "Archangel"
    assert rows[0]["started_at"] == 1_700_000_000
    assert rows[1]["reason"] == IGNORED_REASONS[3] == "timestamp too old"
    assert ledger.clear() == 2
    assert ledger.entries() == []


def test_a_torn_ledger_line_does_not_hide_the_ones_around_it(tmp_path):
    path = tmp_path / "scrobbled.jsonl"
    path.write_text('{"status": "accepted", "track": "Archangel"}\n{ half a line\n')
    assert [r["track"] for r in Ledger(path).entries()] == ["Archangel"]


# --- sending ----------------------------------------------------------------


async def test_draining_sends_everything_and_empties_the_queue(tmp_path):
    outbox, ledger = Outbox(tmp_path / "outbox.json"), Ledger(tmp_path / "led.jsonl")
    outbox.add(play("Archangel", 1))
    outbox.add(play("Etched Headplate", 2))
    scrobbler = FakeScrobbler()
    drained = await Scrobbling(scrobbler, outbox, ledger).drain()
    assert drained == Drain(accepted=2, ignored=0, waiting=0)
    assert [p.track for p in scrobbler.batches[0]] == ["Archangel", "Etched Headplate"]
    assert outbox.entries() == []
    assert [r["status"] for r in ledger.entries()] == ["accepted", "accepted"]


async def test_a_batch_is_sent_at_the_limit_the_spec_gives(tmp_path):
    outbox = Outbox(tmp_path / "outbox.json")
    for index in range(MAX_SCROBBLE_BATCH + 1):
        outbox.add(play(f"track {index}", index + 1))
    scrobbler = FakeScrobbler()
    drained = await Scrobbling(scrobbler, outbox, Ledger(tmp_path / "led.jsonl")).drain()
    assert [len(batch) for batch in scrobbler.batches] == [MAX_SCROBBLE_BATCH, 1]
    assert drained.accepted == MAX_SCROBBLE_BATCH + 1
    assert outbox.entries() == []


async def test_a_play_last_fm_refuses_leaves_the_queue_but_is_still_recorded(tmp_path):
    """Ignored means 'never, whatever you do': keeping it would queue it for ever."""
    outbox, ledger = Outbox(tmp_path / "outbox.json"), Ledger(tmp_path / "led.jsonl")

    def outcomes(plays):
        return [ScrobbleOutcome(track=p.track, ignored=1) for p in plays]

    outbox.add(play("Near Dark", 2))
    drained = await Scrobbling(FakeScrobbler(outcomes), outbox, ledger).drain()
    assert (drained.accepted, drained.ignored, drained.waiting) == (0, 1, 0)
    assert outbox.entries() == []
    assert ledger.entries()[0]["status"] == "ignored"


async def test_a_failed_send_keeps_the_plays_for_next_time(tmp_path):
    outbox, ledger = Outbox(tmp_path / "outbox.json"), Ledger(tmp_path / "led.jsonl")
    outbox.add(play("Archangel", 1))
    drained = await Scrobbling(
        FakeScrobbler(fail=RuntimeError("still offline")), outbox, ledger
    ).drain()
    assert not drained.ok
    assert "still offline" in drained.error
    assert [p.track for p in outbox.entries()] == ["Archangel"]  # nothing lost
    assert ledger.entries() == []  # and nothing claimed either


async def test_an_answer_that_names_none_of_the_plays_keeps_them_queued(tmp_path):
    """Half an answer is not an answer: a play that may have been accepted is not
    thrown away, because sending it again is visible and losing it is not."""
    outbox = Outbox(tmp_path / "outbox.json")
    outbox.add(play("Archangel", 1))
    drained = await Scrobbling(FakeScrobbler(lambda plays: []), outbox, Ledger()).drain()
    assert not drained.ok
    assert [p.track for p in outbox.entries()] == ["Archangel"]


async def test_draining_an_empty_queue_asks_last_fm_nothing(tmp_path):
    scrobbler = FakeScrobbler()
    drained = await Scrobbling(scrobbler, Outbox(tmp_path / "outbox.json"), Ledger()).drain()
    assert drained == Drain(waiting=0)
    assert scrobbler.batches == []
    assert drained.summary() == "nothing to scrobble"


async def test_now_playing_is_best_effort_and_never_raises(tmp_path):
    """The spec says not to retry these; a broken one must not break playback."""

    class Broken(FakeScrobbler):
        async def now_playing(self, *args, **kwargs):
            raise RuntimeError("no")

    await Scrobbling(Broken(), Outbox(tmp_path / "outbox.json"), Ledger()).now_playing(play())


def test_a_drain_with_no_error_says_what_it_did():
    assert Drain(accepted=3).summary() == "scrobbled 3"
    assert Drain(accepted=1, waiting=2).summary() == "scrobbled 1, 2 still queued"
    assert Drain(error="offline", waiting=2).summary().startswith("offline")


def test_a_drain_that_had_something_refused_says_that_too():
    assert Drain(accepted=1, ignored=2).summary() == "scrobbled 1, 2 refused by last.fm"


def test_an_outbox_entry_that_is_not_a_play_is_skipped(tmp_path):
    """The file is meant to be editable by hand, so nonsense in it is expected."""
    path = tmp_path / "outbox.json"
    path.write_text(
        json.dumps(
            {
                "plays": [
                    "not a play at all",
                    {"artist": "Burial", "track": "Archangel", "started_at": 1},
                ]
            }
        )
    )
    assert [p.track for p in Outbox(path).entries()] == ["Archangel"]


def test_an_unwritable_outbox_or_ledger_never_breaks_playback(tmp_path):
    """Both files are best effort: losing the record is bad, crashing is worse."""
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    outbox = Outbox(directory)  # a path nothing can be written to
    outbox.add(play())
    assert outbox.clear() == 0
    ledger = Ledger(directory)
    ledger.record(play(), ScrobbleOutcome(), "accepted")
    assert ledger.entries() == []
    assert ledger.clear() == 0


def test_a_play_reads_as_one_line_for_a_person():
    assert (
        Play("Burial", "Archangel", 1_700_000_000, "Untrue")
        .line()
        .startswith("Burial — Archangel  ·  Untrue  ·  ")
    )
    # No album is no album: nothing is invented to fill the gap.
    assert Play("Burial", "Archangel", 1_700_000_000).line().startswith("Burial — Archangel  ·  ")


async def test_queueing_and_sending_are_separate_steps(tmp_path):
    outbox = Outbox(tmp_path / "outbox.json")
    scrobbler = FakeScrobbler()
    scrobbling = Scrobbling(scrobbler, outbox, Ledger(tmp_path / "led.jsonl"))
    scrobbling.queue(play("Archangel", 1))
    assert [p.track for p in outbox.entries()] == ["Archangel"]  # written down first
    assert scrobbler.batches == []  # and not sent by queueing
    await scrobbling.drain()
    assert scrobbler.batches != []
