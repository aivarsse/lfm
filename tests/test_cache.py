from pathlib import Path

from lfm.store.cache import Cache, cache_key


def test_roundtrip(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("k", '{"a": 1}', ttl=60)
        assert cache.get("k") == '{"a": 1}'


def test_missing_key_returns_none(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        assert cache.get("nope") is None


def test_expired_entry_is_a_miss(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("k", "payload", ttl=0)  # expires immediately
        assert cache.get("k") is None


def test_set_overwrites(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("k", "old", ttl=60)
        cache.set("k", "new", ttl=60)
        assert cache.get("k") == "new"


def test_key_ignores_param_order():
    assert cache_key("m", {"a": "1", "b": "2"}) == cache_key("m", {"b": "2", "a": "1"})


def test_key_separates_methods_and_params():
    assert cache_key("m1", {"a": "1"}) != cache_key("m2", {"a": "1"})
    assert cache_key("m", {"a": "1"}) != cache_key("m", {"a": "2"})


def test_a_corrupt_database_behaves_like_an_empty_one(tmp_path: Path):
    path = tmp_path / "cache.db"
    path.write_bytes(b"this is not a sqlite database")
    with Cache(path) as cache:
        assert cache.available is False
        assert cache.get("k") is None
        assert cache.counts() == (0, 0)
        assert cache.clear() == 0
        assert cache.prune() == 0
        cache.set("k", "payload", ttl=60)  # must not raise


def test_an_unusable_path_behaves_like_an_empty_cache(tmp_path: Path):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    with Cache(not_a_dir / "cache.db") as cache:
        assert cache.available is False
        assert cache.get("k") is None
        assert cache.counts() == (0, 0)
        assert cache.clear() == 0
        assert cache.prune() == 0
        cache.set("k", "payload", ttl=60)


def test_a_broken_handle_during_use_is_a_miss_too(tmp_path: Path):
    # open fine, then fail underneath: the guard has to hold at query time as well
    with Cache(tmp_path / "cache.db") as cache:
        assert cache.available is True
        assert cache._db is not None
        cache._db.close()
        assert cache.get("k") is None
        assert cache.counts() == (0, 0)
        assert cache.clear() == 0
        assert cache.prune() == 0
        cache.set("k", "payload", ttl=60)  # must not raise


def test_counts_separates_the_expired_entries(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("fresh", "payload", ttl=60)
        cache.set("stale", "payload", ttl=0)
        assert cache.counts() == (2, 1)


def test_clear_removes_everything_and_reports_the_count(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("a", "payload", ttl=60)
        cache.set("b", "payload", ttl=60)
        assert cache.clear() == 2
        assert cache.counts() == (0, 0)
        assert cache.clear() == 0


def test_prune_removes_only_the_expired_entries(tmp_path: Path):
    """The point of prune over clear: what is still fresh survives it."""
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("fresh", "payload", ttl=60)
        cache.set("stale", "payload", ttl=0)
        assert cache.prune() == 1
        assert cache.counts() == (1, 1)  # the count still adds up to what is there
        assert cache.get("fresh") == "payload"
        assert cache.get("stale") is None


def test_prune_with_nothing_expired_is_a_no_op(tmp_path: Path):
    with Cache(tmp_path / "cache.db") as cache:
        cache.set("fresh", "payload", ttl=60)
        assert cache.prune() == 0
        assert cache.counts() == (1, 1)


def test_prune_reclaims_the_space_it_frees(tmp_path: Path):
    """A DELETE alone leaves the pages behind, which is half of what prune is for."""
    path = tmp_path / "cache.db"
    with Cache(path) as cache:
        cache.set("stale", "x" * 400_000, ttl=0)
        bloated = path.stat().st_size
        assert cache.prune() == 1
        assert path.stat().st_size < bloated
