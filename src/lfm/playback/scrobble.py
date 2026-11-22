"""What counts as a play, and the plays still to be sent.

The rule is Last.fm's, quoted from the Scrobbling 2.0 specification: a track is
scrobbled when it is longer than 30 seconds *and* it has been played for at
least half its duration, or for 4 minutes, whichever comes first. The same page
says the scrobble may be sent as soon as those conditions are met rather than at
the end of the track — which is how this works, because it then survives a skip,
a quit or a crash without anything having to run on the way out.

Nothing here touches the network; `client.Scrobbler` sends. This module decides
what is worth sending, keeps it until Last.fm has answered for it, and remembers
what was answered. That last part is the whole of the undo story: the API has no
way to remove a scrobble, so the record of what was sent is what you work from
when removing one by hand, and the outbox is what there is to throw away while
nothing has been sent yet.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .. import paths

if TYPE_CHECKING:  # the network half; imported only for the type, to stay import-light
    from ..api.client import ScrobbleOutcome

# The specification's numbers. Longer than MIN_DURATION, and played for half the
# track or PLAY_CEILING, whichever comes first.
MIN_DURATION = 30.0
PLAY_CEILING = 240.0

# What Last.fm means by each ignoredMessage code, for the ledger and the CLI.
IGNORED_REASONS = {
    1: "artist ignored",
    2: "track ignored",
    3: "timestamp too old",
    4: "timestamp too new",
    5: "daily scrobble limit reached",
}


def counts_as_played(duration: float, played: float) -> bool:
    """Is `played` seconds of a `duration`-second track a scrobble?

    An unknown duration (0, as mpv reports before it has looked at the file) is
    not a scrobble: the rule cannot be checked, and guessing it is worse than
    losing one play.
    """
    if duration <= MIN_DURATION:
        return False
    return played >= min(duration / 2, PLAY_CEILING)


def outbox_path() -> Path:
    return paths.cache_file("outbox.json")


def ledger_path() -> Path:
    return paths.cache_file("scrobbled.jsonl")


@dataclass(frozen=True)
class Play:
    """One play, in the shape Last.fm wants: the time it *started*, in UTC.

    `started_at` is the timestamp that goes on the wire, so when a scrobble is
    sent hours late it still lands at the time the track was played.
    """

    artist: str
    track: str
    started_at: int
    album: str = ""
    duration: int = 0

    def key(self) -> tuple[str, str, int]:
        """What makes two plays the same play, for taking one out of the outbox."""
        return (self.artist.casefold(), self.track.casefold(), self.started_at)

    def line(self) -> str:
        """How one play reads in a list, for `lfm scrobble --dry-run`."""
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started_at))
        album = f"  ·  {self.album}" if self.album else ""
        return f"{self.artist} — {self.track}{album}  ·  {when}"


@dataclass
class Session:
    """One loaded track, fed the player's position until it counts as a play.

    The TUI makes one of these when a track starts and feeds every status
    reading into it; `observe` answers with the `Play` on the reading where the
    rule is first met, and with None on every other reading, so a track cannot
    be scrobbled twice however often the bar repaints.
    """

    artist: str
    track: str
    album: str = ""
    started_at: int = field(default_factory=lambda: int(time.time()))
    played: float = 0.0
    reported: bool = False

    def opening(self) -> Play:
        """The play as it stands at the start: what `track.updateNowPlaying` wants."""
        return Play(
            artist=self.artist, track=self.track, started_at=self.started_at, album=self.album
        )

    def observe(self, position: float, duration: float) -> Play | None:
        """Feed one reading of the player: (position, duration) in seconds."""
        if self.reported:
            return None
        # The furthest point reached, not a sum of deltas: a rewind does not
        # un-listen to the part already heard. A forward seek therefore counts
        # as having played, which is what other clients do with the same rule.
        self.played = max(self.played, position)
        if not counts_as_played(duration, self.played):
            return None
        self.reported = True
        return Play(
            artist=self.artist,
            track=self.track,
            started_at=self.started_at,
            album=self.album,
            duration=int(duration),
        )


class Outbox:
    """Plays Last.fm has not answered for yet, oldest first.

    Deliberately a plain JSON file: it is the part of scrobbling that is meant
    to be undoable, so it has to be readable, and editable, without lfm's help.
    Entries leave only once Last.fm has answered for them.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or outbox_path()

    def _load(self) -> list[Play]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []  # unreadable or corrupt: an empty queue, never a crash
        entries = data.get("plays", []) if isinstance(data, dict) else []
        plays = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                plays.append(Play(**entry))
            except TypeError:
                continue  # a hand-edited entry missing a field is dropped
        return plays

    def entries(self) -> list[Play]:
        """What is waiting, oldest first, exactly as Last.fm will receive it."""
        return self._load()

    def add(self, play: Play) -> None:
        """Queue one play, ignoring a play already queued."""
        plays = self._load()
        if any(queued.key() == play.key() for queued in plays):
            return
        plays.append(play)
        self._save(plays)

    def discard(self, plays: Iterable[Play]) -> int:
        """Take these plays out of the queue, by identity; how many were there."""
        keys = {play.key() for play in plays}
        queued = self._load()
        kept = [one for one in queued if one.key() not in keys]
        removed = len(queued) - len(kept)
        if removed:
            self._save(kept)
        return removed

    def clear(self) -> int:
        """Throw the whole queue away, sent or not; how many plays that was."""
        count = len(self._load())
        if count or self.path.exists():
            self._save([])
        return count

    def _save(self, plays: list[Play]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"plays": [asdict(p) for p in plays]}, indent=2) + "\n")
        except OSError:
            pass  # an unwritable queue loses a scrobble, never a session


class Ledger:
    """One line per play Last.fm has answered for, appended in answer order.

    This exists to be read back later, by a person: the API cannot delete a
    scrobble, so what was sent, and what Last.fm said about it, is the only way
    to find a play again once it is on the profile.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ledger_path()

    def entries(self) -> list[dict[str, object]]:
        """Everything sent so far, in the order Last.fm answered for it."""
        try:
            text = self.path.read_text()
        except OSError:
            return []
        rows = []
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn or hand-edited line is skipped, not fatal
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def record(self, play: Play, outcome: ScrobbleOutcome | None, status: str) -> None:
        """Note one play's fate: `accepted`, `ignored` or `failed`."""
        row = {
            "sent_at": int(time.time()),
            "status": status,
            **asdict(play),
        }
        if outcome is not None:
            row["ignored"] = outcome.ignored
            row["reason"] = IGNORED_REASONS.get(outcome.ignored, "")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def clear(self) -> int:
        """Forget the record; how many lines that was."""
        count = len(self.entries())
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        return count


@dataclass(frozen=True)
class Drain:
    """What one attempt to send did."""

    accepted: int = 0
    ignored: int = 0
    waiting: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        """One line for the CLI and for the TUI's note."""
        if self.error:
            sent = f"sent {self.accepted}" if self.accepted else "sent nothing"
            return f"{self.error}  ·  {sent}, {self.waiting} still queued"
        if not self.accepted and not self.ignored:
            return "nothing to scrobble"
        parts = []
        if self.accepted:
            parts.append(f"scrobbled {self.accepted}")
        if self.ignored:
            parts.append(f"{self.ignored} refused by last.fm")
        if self.waiting:
            parts.append(f"{self.waiting} still queued")
        return ", ".join(parts)


class Wire(Protocol):
    """What sending a scrobble needs of the client, so a test can stand in.

    `client.Scrobbler` is the real one; this exists so the queue and its rules
    can be exercised without a network, which is where nearly all of the logic
    that can go wrong actually lives.
    """

    async def now_playing(
        self, artist: str, track: str, album: str = "", duration: int = 0
    ) -> None: ...

    async def scrobble(self, plays: Sequence[Play]) -> list[ScrobbleOutcome]: ...


class Scrobbling:
    """Decide, queue, send: the write path behind one object.

    The TUI is handed one of these, or nothing at all. Nothing at all is what
    lfm does by default — scrobbling off, or no secret, or no session key — and
    then no play is ever queued anywhere.
    """

    def __init__(
        self,
        scrobbler: Wire,
        outbox: Outbox | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.scrobbler = scrobbler
        self.outbox = outbox or Outbox()
        self.ledger = ledger or Ledger()

    def queue(self, play: Play) -> None:
        """Put one play in the outbox. Sending is a separate, later step."""
        self.outbox.add(play)

    async def now_playing(self, play: Play) -> None:
        """Say what started playing; best effort, and never allowed to raise.

        The spec's own advice is not to retry these and not to treat them as
        important: a line on a profile is decoration, and a stale one is worse
        than none, so a failure here is dropped rather than queued.
        """
        try:
            await self.scrobbler.now_playing(play.artist, play.track, play.album, play.duration)
        except Exception:
            pass

    async def drain(self) -> Drain:
        """Send what is queued, in batches, keeping whatever is not taken.

        A batch that Last.fm answers for leaves the outbox either way: taken
        plays are recorded as accepted, refused ones as ignored with the reason,
        because neither will ever go through differently on a second try. A
        batch that fails outright stays queued for the next attempt.
        """
        from ..api.client import MAX_SCROBBLE_BATCH  # the spec's batch limit, which is the wire's

        accepted = ignored = 0
        while True:
            batch = self.outbox.entries()[:MAX_SCROBBLE_BATCH]
            if not batch:
                break
            try:
                outcomes = await self.scrobbler.scrobble(batch)
            except Exception as exc:  # a failed send leaves the queue for next time
                return Drain(
                    accepted=accepted,
                    ignored=ignored,
                    waiting=len(self.outbox.entries()),
                    error=str(exc),
                )
            if len(outcomes) != len(batch):  # Last.fm answered for a different set
                return Drain(
                    accepted=accepted,
                    ignored=ignored,
                    waiting=len(self.outbox.entries()),
                    error=f"last.fm answered for {len(outcomes)} of {len(batch)} scrobbles",
                )
            for play, outcome in zip(batch, outcomes, strict=True):
                taken = outcome.accepted
                self.ledger.record(play, outcome, "accepted" if taken else "ignored")
                accepted += int(taken)
                ignored += int(not taken)
            self.outbox.discard(batch)
        return Drain(accepted=accepted, ignored=ignored, waiting=len(self.outbox.entries()))


__all__ = [
    "IGNORED_REASONS",
    "MIN_DURATION",
    "PLAY_CEILING",
    "Drain",
    "Ledger",
    "Outbox",
    "Play",
    "Scrobbling",
    "Session",
    "Wire",
    "counts_as_played",
    "ledger_path",
    "outbox_path",
]
