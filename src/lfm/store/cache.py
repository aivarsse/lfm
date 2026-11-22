"""SQLite TTL cache, keyed by method + params. No dependency, ~40 lines."""

import hashlib
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode


def cache_key(method: str, params: dict[str, str]) -> str:
    """Stable key for a method call; params are sorted so order never matters."""
    query = urlencode(sorted(params.items()))
    return hashlib.sha256(f"{method}?{query}".encode()).hexdigest()


class Cache:
    """SQLite TTL store. Best effort throughout: a cache that cannot be read or
    written is never worth failing a command over, so those cases act like a miss.
    """

    def __init__(self, path: Path) -> None:
        self._db: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS entries"
                " (key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)"
            )
        except (sqlite3.Error, OSError):
            if self._db is not None:  # opened, then turned out to be unusable
                with suppress(sqlite3.Error):
                    self._db.close()
            self._db = None  # unreadable, unwritable, or not a database at all

    @property
    def available(self) -> bool:
        """False when the database could not be opened; `lfm cache` should say so."""
        return self._db is not None

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc: object) -> None:
        if self._db is not None:
            self._db.close()

    def get(self, key: str) -> str | None:
        """Return the cached payload, or None if absent, expired or unreadable."""
        if self._db is None:
            return None
        try:
            row = self._db.execute(
                "SELECT payload FROM entries WHERE key = ? AND expires > ?", (key, time.time())
            ).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    def set(self, key: str, payload: str, ttl: int) -> None:
        if self._db is None:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO entries VALUES (?, ?, ?)",
                (key, time.time() + ttl, payload),
            )
            self._db.commit()
        except sqlite3.Error:
            pass  # a cache that cannot be written is a cache that stays empty

    def counts(self) -> tuple[int, int]:
        """(entries, how many of those are still fresh). Expired rows linger until reused."""
        if self._db is None:
            return 0, 0
        try:
            total, fresh = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(expires > ?), 0) FROM entries", (time.time(),)
            ).fetchone()
        except sqlite3.Error:
            return 0, 0
        return int(total), int(fresh)

    def clear(self) -> int:
        """Drop every entry and hand back how many there were."""
        if self._db is None:
            return 0
        try:
            (removed,) = self._db.execute("SELECT COUNT(*) FROM entries").fetchone()
            self._db.execute("DELETE FROM entries")
            self._db.commit()
            self._db.execute("VACUUM")  # hand the disk space back with it
        except sqlite3.Error:
            return 0
        return int(removed)

    def prune(self) -> int:
        """Drop the expired entries only, and hand back how many went.

        Expired rows are ignored by every lookup but keep the file big until
        something happens to replace them, so keeping the fresh ones while
        reclaiming that space needs its own verb. `clear` is the other half:
        everything, fresh or not.
        """
        if self._db is None:
            return 0
        now = time.time()
        try:
            (removed,) = self._db.execute(
                "SELECT COUNT(*) FROM entries WHERE expires <= ?", (now,)
            ).fetchone()
            self._db.execute("DELETE FROM entries WHERE expires <= ?", (now,))
            self._db.commit()
            if removed:
                self._db.execute("VACUUM")
        except sqlite3.Error:
            return 0
        return int(removed)
