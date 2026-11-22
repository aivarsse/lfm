"""The one opt-in test that talks to the real Last.fm.

    LFM_LIVE=1 uv run pytest tests/test_live.py

It uses the key in $LFM_API_KEY, and is skipped by default, so the
suite stays offline and CI needs no secret. What it catches is Last.fm changing the shape
of a response, which the respx fixtures cannot: they encode what we believe it sends.
"""

import os

import pytest

from lfm.api.client import LastfmClient
from lfm.config import ENV_KEY

pytestmark = pytest.mark.skipif(
    os.environ.get("LFM_LIVE") != "1",
    reason="set LFM_LIVE=1 to run against the live API",
)


async def test_live_similar_artists_come_back():
    api_key = os.environ.get(ENV_KEY, "").strip()
    if not api_key:
        pytest.skip(f"set {ENV_KEY} to run the live test")
    client = LastfmClient(api_key, refresh=True)  # refresh: a live test must not read cache
    artists = await client.similar("Burial", limit=3)
    assert artists
