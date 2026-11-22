"""What the CLI decides before the TUI starts.

The app and its screens take one of these rather than a handful of keyword
arguments each: they are the same few values threaded through several
constructors, and a screen has no business knowing which flag produced them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..playback.scrobble import Scrobbling
from ..playback.visualizer import SpectrumFactory
from .theme import DEFAULT_THEME


@dataclass(frozen=True)
class TuiOptions:
    """The artist to open, how to rank, and which optional pieces to wire up."""

    artist: str | None = None
    limit: int = 30
    min_match: float = 0.0
    theme: str = DEFAULT_THEME
    spectrum: SpectrumFactory | None = None
    save_volume: Callable[[int], str | None] | None = None
    scrobbling: Scrobbling | None = None
