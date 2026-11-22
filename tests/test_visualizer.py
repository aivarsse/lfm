import asyncio
from pathlib import Path

import pytest

from lfm.playback.visualizer import (
    BARS,
    CavaSpectrum,
    cava_cmd,
    config_text,
    find_cava,
    level_style,
    parse_frame,
    spectrum_text,
)

FAKE_CAVA = """#!/bin/sh
while true; do
  printf '0;250;500;1000\\n'
  sleep 0.01
done
"""


@pytest.fixture
def fake_cava(tmp_path: Path) -> str:
    script = tmp_path / "cava"
    script.write_text(FAKE_CAVA)
    script.chmod(0o755)
    return str(script)


def test_config_asks_cava_for_raw_ascii_frames():
    config = config_text(bars=8)
    assert "bars = 8" in config
    assert "method = raw" in config
    assert "data_format = ascii" in config
    assert "raw_target = /dev/stdout" in config


def test_config_keeps_the_strip_snappy():
    config = config_text()
    assert "framerate = 60" in config  # cava's default; we were sampling at 30
    # cava's default noise_reduction of 77 is documented as "very slow"
    assert "noise_reduction = 20" in config


def test_cava_cmd_passes_the_config():
    assert cava_cmd("/tmp/x.conf", "cava") == ["cava", "-p", "/tmp/x.conf"]


def test_find_cava():
    assert find_cava("/nonexistent/cava") is None
    assert find_cava("/bin/sh") == "/bin/sh"


def test_parse_frame_scales_to_zero_one():
    assert parse_frame("0;250;500;1000", bars=4) == [0.0, 0.25, 0.5, 1.0]


def test_parse_frame_pads_and_trims_to_bar_count():
    assert parse_frame("1000", bars=3) == [1.0, 0.0, 0.0]
    assert parse_frame("0;0;0;0;0", bars=2) == [0.0, 0.0]


def test_parse_frame_ignores_junk_entries():
    assert parse_frame("0;;oops;1000", bars=3) == [0.0, 1.0, 0.0]


def test_spectrum_text_shades_from_quiet_to_loud():
    palette = ("#00ff00", "#ffa500", "#ff0000")
    text = spectrum_text([0.0, 1.0], palette=palette)
    assert str(text) == "▁█"
    assert str(text.spans[0].style) == "#00ff00 dim"  # silence: a floor, not a colour
    assert str(text.spans[-1].style) == "#ff0000"  # full scale is the loud colour
    assert level_style(0.5, palette) == "#ffa500"  # and the middle lands on the middle


async def test_missing_cava_is_reported_not_fatal():
    spectrum = CavaSpectrum(cava="/nonexistent/cava")
    assert await spectrum.start() is False
    assert await spectrum.levels() == [0.0] * BARS


async def test_cava_frames_become_levels(fake_cava: str):
    spectrum = CavaSpectrum(cava=fake_cava, bars=4)
    assert await spectrum.start() is True
    levels: list[float] = []
    for _ in range(100):
        levels = await spectrum.levels()
        if levels == [0.0, 0.25, 0.5, 1.0]:
            break
        await asyncio.sleep(0.02)
    assert levels == [0.0, 0.25, 0.5, 1.0]

    await spectrum.stop()
    assert await spectrum.levels() == [0.0] * 4


def test_channels_belongs_to_the_output_section():
    """cava reads output:channels; in [general] it was silently ignored (stereo)."""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in config_text(bars=8).splitlines():
        if line.startswith("["):
            current = line.strip("[]")
            sections[current] = []
        elif line.strip():
            sections[current].append(line)
    assert "channels = mono" in sections["output"]
    assert not any(line.startswith("channels") for line in sections["general"])


def test_silence_draws_the_lowest_block_and_full_scale_the_highest():
    assert spectrum_text([0.0]).plain == "▁"
    assert spectrum_text([1.0]).plain == "█"  # BLOCKS[0] is reachable again


async def test_start_says_no_when_cava_dies_at_once(tmp_path: Path):
    script = tmp_path / "cava"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    spectrum = CavaSpectrum(cava=str(script))
    assert await spectrum.start() is False
    assert await spectrum.levels() == [0.0] * BARS
