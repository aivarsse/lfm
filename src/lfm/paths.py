"""Where lfm keeps its files: one place for the cache-dir dance.

platformdirs is imported inside each function on purpose. It is only needed when a
file is actually read or written, and importing it up front would land on every
`lfm --help`.
"""

from __future__ import annotations

from pathlib import Path


def config_file(name: str) -> Path:
    """A file in lfm's config directory (`~/.config/lfm`)."""
    from platformdirs import user_config_dir

    return Path(user_config_dir("lfm")) / name


def cache_file(name: str) -> Path:
    """A file in lfm's cache directory (`~/.cache/lfm`): cache, history, sources, scrobbles."""
    from platformdirs import user_cache_dir

    return Path(user_cache_dir("lfm")) / name
