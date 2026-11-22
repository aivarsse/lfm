"""The YouTube source you picked for a track, remembered between sessions.

Same shape as history: state beside the cache, read and written best effort, so
a missing or broken file never stops playback. The watch URL is stored rather
than a resolved stream URL because the latter expires within hours.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .. import paths

# Deliberate choices, so this grows slowly; trim the oldest if it ever does not.
MAX_SOURCES = 500


def sources_path() -> Path:
    return paths.cache_file("sources.json")


@dataclass(frozen=True)
class Source:
    title: str
    url: str


class Sources:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or sources_path()
        self._chosen = self._load()

    @staticmethod
    def _key(artist: str, track: str) -> str:
        return f"{artist.strip().casefold()}\t{track.strip().casefold()}"

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        chosen = data.get("chosen", {}) if isinstance(data, dict) else {}
        if not isinstance(chosen, dict):
            return {}
        return {
            key: value
            for key, value in chosen.items()
            if isinstance(value, dict) and "url" in value and "title" in value
        }

    def chosen(self, artist: str, track: str) -> Source | None:
        """The source picked for this track, if any."""
        entry = self._chosen.get(self._key(artist, track))
        if entry is None:
            return None
        return Source(title=str(entry["title"]), url=str(entry["url"]))

    def remember(self, artist: str, track: str, *, title: str, url: str) -> None:
        self._chosen[self._key(artist, track)] = {"title": title, "url": url}
        for stale in list(self._chosen)[: max(0, len(self._chosen) - MAX_SOURCES)]:
            del self._chosen[stale]
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps({"chosen": self._chosen}, indent=2) + "\n")
            temporary.replace(self.path)
        except OSError:
            pass  # a convenience, never worth failing playback over
