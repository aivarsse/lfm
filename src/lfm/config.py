"""User configuration from `~/.config/lfm/config.toml`.

Precedence is CLI flag > environment > config file > built-in default. Only the
keys in `DEFAULTS`, plus `api_key` and the two scrobbling credentials
(`secret`, `session_key`), are read; anything else in the file is ignored. A
malformed file or a bad value is reported rather than silently falling back,
since a typo here would otherwise be invisible.

The file holds credentials — the API key, and the secret and session key the
write path needs. A file lfm creates is written 0600; a file that already exists
keeps whatever mode its owner gave it, because lfm rewrites this file whenever a
setting changes and resetting the mode every time would silently undo a
deliberate `chmod`. `lfm config` reports the mode, and `lfm auth` says so once
when it is about to put a session key in a file other local users can read.
"""

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

ENV_KEY = "LFM_API_KEY"
# The write path's two credentials. Environment first, as with the key, so the
# shared secret never has to touch the disk at all if the owner would rather it
# did not — and `lfm auth` is what puts a session key in the file.
ENV_SECRET = "LFM_SECRET"
ENV_SESSION_KEY = "LFM_SESSION_KEY"

DEFAULTS: dict[str, Any] = {
    "limit": 20,
    "min_match": 0.0,
    "output": "table",
    "volume": 100,
    "visualizer": True,
    "theme": "obsidian",
    # Off unless asked for, so that a config file that predates scrobbling — or
    # one whose owner never wanted it — cannot post anything by accident.
    "scrobble": False,
}


class ConfigError(Exception):
    """The config file could not be read or holds an invalid value."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


VALIDATORS: dict[str, Any] = {
    "api_key": lambda v: isinstance(v, str),
    # A secret is a 32-character hex string and a session key likewise; neither
    # is checked for shape here beyond being a string, because the one thing
    # that must not happen is a valid credential being rejected as malformed.
    "secret": lambda v: isinstance(v, str),
    "session_key": lambda v: isinstance(v, str),
    "scrobble": lambda v: isinstance(v, bool),
    "limit": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
    "min_match": lambda v: _is_number(v) and 0 <= v <= 1,
    "output": lambda v: v in {"table", "plain", "json", "links"},
    "volume": lambda v: _is_number(v) and 0 <= v <= 130,
    "visualizer": lambda v: isinstance(v, bool),
    "theme": lambda v: v in {"obsidian", "cream", "midnight"},
}


@dataclass(frozen=True)
class Config:
    path: Path
    values: dict[str, Any] = field(default_factory=dict)
    exists: bool = False

    def get(self, key: str) -> Any:
        return self.values.get(key, DEFAULTS.get(key))

    def api_key(self) -> str:
        """The key from the environment first, then the config file."""
        return self._credential(ENV_KEY, "api_key")

    def secret(self) -> str:
        """The API account's shared secret: without it nothing can be signed."""
        return self._credential(ENV_SECRET, "secret")

    def session_key(self) -> str:
        """The `sk` from `lfm auth`: proof that the owner authorised the writes."""
        return self._credential(ENV_SESSION_KEY, "session_key")

    def scrobbling(self) -> bool:
        """Is everything a scrobble needs actually here?

        Three things have to be true before one play can leave the machine: the
        setting, the secret and the session key. Anything missing means the TUI
        is handed nothing to scrobble with, which is what makes off mean off
        rather than off until something else happens to be set.
        """
        return bool(self.get("scrobble")) and all(
            (self.api_key(), self.secret(), self.session_key())
        )

    def key_source(self) -> str:
        return self._source(ENV_KEY, "api_key")

    def secret_source(self) -> str:
        return self._source(ENV_SECRET, "secret")

    def session_source(self) -> str:
        return self._source(ENV_SESSION_KEY, "session_key")

    def _credential(self, env: str, key: str) -> str:
        return os.environ.get(env, "").strip() or str(self.values.get(key, "")).strip()

    def _source(self, env: str, key: str) -> str:
        """Where a credential would come from — never the credential itself."""
        if os.environ.get(env, "").strip():
            return f"${env}"
        if str(self.values.get(key, "")).strip():
            return str(self.path)
        return "not set"


def config_path() -> Path:
    return paths.config_file("config.toml")


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.is_file():
        return Config(path=path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        # The parser's own words ("Expected newline or end of document after a
        # statement") do not say what to do. Every value in this file is a TOML
        # string, so an unquoted one is the mistake people actually make.
        raise ConfigError(
            f"cannot read {path}: {exc}\n"
            'Every value is a TOML string, so it needs quotes: api_key = "…" — or set '
            f"{ENV_KEY} in the environment and leave the file alone."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    values = {key: value for key, value in data.items() if key in VALIDATORS}
    for key, value in values.items():
        if not VALIDATORS[key](value):
            raise ConfigError(f"invalid {key!r} in {path}: {value!r}")
    return Config(path=path, values=values, exists=True)


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def _mode_to_keep(path: Path) -> int:
    """The mode for the rewritten file: its own if it has one, else 0600.

    The file this writes holds credentials, so a new one starts at 0600 — but a
    file the owner already has keeps the mode they gave it. Resetting the mode
    on every write would undo a deliberate `chmod` (an owner who needs another
    local account to read it, say) each time any setting changed, and it would
    do so silently, which is worse than the exposure it was guarding against.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return 0o600


def save_value(path: Path, key: str, value: Any) -> None:
    """Update one setting in the file, leaving comments and other keys alone.

    Written via a temporary file and a rename so a crash cannot truncate the
    user's config. Raises ConfigError for an unknown key or bad value.
    """
    if key not in VALIDATORS:
        raise ConfigError(f"unknown setting {key!r}")
    if not VALIDATORS[key](value):
        raise ConfigError(f"invalid {key!r}: {value!r}")

    try:
        text = path.read_text() if path.is_file() else ""
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    lines = text.splitlines()
    replacement = f"{key} = {_format(value)}"
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(key) and stripped[len(key) :].lstrip().startswith("="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = _mode_to_keep(path)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text("\n".join(lines) + "\n")
        # The mode has to be set before the rename lands: the temporary file
        # otherwise carries the umask default (002 here — group-writable), which
        # would replace a mode the owner had chosen.
        os.chmod(temporary, mode)
        temporary.replace(path)
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc}") from exc
