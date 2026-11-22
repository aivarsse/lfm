"""cava-backed spectrum for the TUI.

lfm runs `cava` in raw output mode and reads its per-frame bar values. cava
captures the audio monitor, so the visualiser follows whatever is actually
playing — including seeking, and regardless of which process makes the sound.
That is why this is kept out of the player: the player fetches a second copy of
the audio (expensive, and it drifts on seek), whereas cava taps what you hear.

Needs the `cava` binary; without it the visualiser simply does not appear.
"""

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from rich.text import Text

from ..ui.render import blend

CAVA = "cava"
BARS = 32
MAX_BARS = 128  # plenty for a wide terminal; cava allows up to 512
ASCII_MAX = 1000
# cava defaults to 60; matching it keeps the strip from being a frame behind.
FRAMERATE = 60
# How long to wait for cava's first frame before assuming it is alive but slow.
START_TIMEOUT = 0.5
# cava's default is 77, which its own docs call "very slow and smooth". Low is
# snappier; not 0, or the bars flicker.
NOISE_REDUCTION = 20
# ▁..█; the lowest block is what silence draws.
BLOCKS = "▁▂▃▄▅▆▇█"

# The raw-output config that matters; everything else is cava's default.
# Deliberately comment-free: cava parses it with iniparser, and the notes
# would have to live inside a section. `channels` is read from [output],
# not [general] (cava's config.c: iniparser_getstring(ini,
# "output:channels", "stereo")); mono draws lowest to highest left to
# right, where the default stereo mirrors the spectrum around the middle.
CONFIG = """\
[general]
bars = {bars}
framerate = {framerate}
sensitivity = 120
lower_cutoff_freq = 50
higher_cutoff_freq = 12000

[smoothing]
noise_reduction = {noise_reduction}

[output]
channels = mono
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = {ascii_max}
bar_delimiter = 59
frame_delimiter = 10
"""


class Spectrum(Protocol):
    """What the TUI needs from a visualiser source (lets tests inject a fake)."""

    bars: int

    async def start(self) -> bool: ...

    async def levels(self) -> list[float]: ...

    async def stop(self) -> None: ...


SpectrumFactory = Callable[[int], Spectrum]


def find_cava(cava: str = CAVA) -> str | None:
    return shutil.which(cava)


def config_text(bars: int = BARS) -> str:
    return CONFIG.format(
        bars=bars,
        framerate=FRAMERATE,
        noise_reduction=NOISE_REDUCTION,
        ascii_max=ASCII_MAX,
    )


def cava_cmd(config_path: Path | str, cava: str = CAVA) -> list[str]:
    return [cava, "-p", str(config_path)]


def parse_frame(line: str, ascii_max: int = ASCII_MAX, bars: int = BARS) -> list[float]:
    """One raw cava frame ("123;456;…") as 0..1 levels, padded or trimmed to `bars`."""
    values: list[float] = []
    for part in line.strip().split(";"):
        with suppress(ValueError):
            values.append(min(1.0, max(0.0, int(part) / ascii_max)))
    return values[:bars] + [0.0] * max(0, bars - len(values))


# Quiet, middle and loud; the screen hands over its theme's colours so the strip
# belongs to whichever theme is on.
SPECTRUM_COLOURS = ("#6fbf73", "#ffa62b", "#ff6b6b")


def level_style(level: float, palette: Sequence[str] = SPECTRUM_COLOURS) -> str:
    """Green through the accent to red as a band gets louder."""
    low, mid, high = palette[0], palette[len(palette) // 2], palette[-1]
    if level < 0.01:
        return f"{low} dim"  # a floor: the band is there, quietly
    if level < 0.5:
        return blend(low, mid, level / 0.5)
    return blend(mid, high, (level - 0.5) / 0.5)


def spectrum_text(levels: Sequence[float], palette: Sequence[str] = SPECTRUM_COLOURS) -> Text:
    """Eight-bit looking blocks, shaded from quiet to loud."""
    text = Text()
    for level in levels:
        index = min(len(BLOCKS) - 1, int(level * len(BLOCKS)))
        text.append(BLOCKS[index], style=level_style(level, palette))
    return text


def make_cava(bars: int = BARS) -> Spectrum:
    return CavaSpectrum(bars=bars)


class CavaSpectrum:
    """Owns one cava process and keeps its latest frame of bar levels."""

    def __init__(self, *, cava: str = CAVA, bars: int = BARS, ascii_max: int = ASCII_MAX) -> None:
        self.bars = bars
        self._cava = cava
        self._ascii_max = ascii_max
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._levels: list[float] = [0.0] * bars
        self._config: Path | None = None
        self._first_frame = asyncio.Event()

    async def start(self) -> bool:
        binary = find_cava(self._cava)
        if binary is None:
            return False
        handle, path = tempfile.mkstemp(prefix="lfm-cava-", suffix=".conf")
        with os.fdopen(handle, "w") as config:
            config.write(config_text(self.bars))
        self._config = Path(path)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cava_cmd(path, binary),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            await self.stop()
            return False
        self._reader = asyncio.create_task(self._read())
        # cava that dies on startup (no sound device, a bad config, a segfault)
        # would otherwise leave an empty strip with nothing to explain it.
        with suppress(TimeoutError):
            await asyncio.wait_for(self._first_frame.wait(), timeout=START_TIMEOUT)
        if self._proc is None or self._proc.returncode is not None:
            await self.stop()
            return False
        return True

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while line := await self._proc.stdout.readline():
            self._first_frame.set()
            self._levels = parse_frame(line.decode(errors="replace"), self._ascii_max, self.bars)

    async def levels(self) -> list[float]:
        if self._proc is None or self._proc.returncode is not None:
            return [0.0] * self.bars
        return list(self._levels)

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._config is not None:
            self._config.unlink(missing_ok=True)
            self._config = None
        self._proc = None
        self._levels = [0.0] * self.bars
