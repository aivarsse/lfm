"""Recently looked-up artists, so the prompt can complete them.

This is state, not configuration: it lives beside the cache and is best effort
— failing to read or write it never breaks a lookup.
"""

import json
from pathlib import Path

from .. import paths

# Plenty for completion without growing without bound.
MAX_HISTORY = 200


def history_path() -> Path:
    return paths.cache_file("history.json")


class History:
    """Most-recent-first artist names, deduplicated case-insensitively."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or history_path()
        self._names = self._load()

    def _load(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        names = data.get("artists", []) if isinstance(data, dict) else []
        return [name for name in names if isinstance(name, str)]

    def names(self) -> list[str]:
        """The remembered artists, most recently seen first."""
        return list(self._names)

    def add(self, artist: str) -> None:
        name = artist.strip()
        if not name:
            return
        folded = name.casefold()
        self._names = [name, *(n for n in self._names if n.casefold() != folded)]
        del self._names[MAX_HISTORY:]
        self._save()

    def remove(self, artist: str) -> bool:
        """Forget one artist, case-insensitively; True if it was remembered."""
        folded = artist.strip().casefold()
        if not folded:
            return False
        remaining = [n for n in self._names if n.casefold() != folded]
        if len(remaining) == len(self._names):
            return False
        self._names = remaining
        self._save()
        return True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps({"artists": self._names}, indent=2) + "\n")
            temporary.replace(self.path)
        except OSError:
            pass  # a convenience, never worth failing a command over
