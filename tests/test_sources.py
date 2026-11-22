from lfm.store.sources import MAX_SOURCES, Sources


def test_nothing_chosen_yet(tmp_path):
    assert Sources(tmp_path / "s.json").chosen("Burial", "Archangel") is None


def test_remember_and_look_up(tmp_path):
    sources = Sources(tmp_path / "s.json")
    sources.remember("Burial", "Archangel", title="Archangel", url="https://youtu.be/aaa")
    chosen = sources.chosen("Burial", "Archangel")
    assert chosen is not None
    assert (chosen.title, chosen.url) == ("Archangel", "https://youtu.be/aaa")


def test_lookup_is_case_insensitive_and_per_track(tmp_path):
    sources = Sources(tmp_path / "s.json")
    sources.remember("Burial", "Archangel", title="A", url="https://youtu.be/aaa")
    assert sources.chosen("burial", "archangel") is not None
    assert sources.chosen("Burial", "Near Dark") is None
    assert sources.chosen("Kode9", "Archangel") is None


def test_choices_survive_a_reload(tmp_path):
    path = tmp_path / "s.json"
    Sources(path).remember("Burial", "Archangel", title="A", url="https://youtu.be/aaa")
    chosen = Sources(path).chosen("Burial", "Archangel")
    assert chosen is not None and chosen.url == "https://youtu.be/aaa"


def test_choosing_again_replaces_the_source(tmp_path):
    sources = Sources(tmp_path / "s.json")
    sources.remember("Burial", "Archangel", title="First", url="https://youtu.be/aaa")
    sources.remember("Burial", "Archangel", title="Second", url="https://youtu.be/bbb")
    chosen = sources.chosen("Burial", "Archangel")
    assert chosen is not None and chosen.url == "https://youtu.be/bbb"
    assert len(sources._chosen) == 1


def test_a_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not json")
    assert Sources(path).chosen("Burial", "Archangel") is None


def test_entries_without_a_url_are_dropped(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"chosen": {"burial\\tarchangel": {"title": "A"}}}')
    assert Sources(path).chosen("Burial", "Archangel") is None


def test_a_failed_write_is_not_fatal(tmp_path):
    path = tmp_path / "s.json"
    path.mkdir()  # writing fails, and that is fine
    sources = Sources(path)
    sources.remember("Burial", "Archangel", title="A", url="https://youtu.be/aaa")
    assert sources.chosen("Burial", "Archangel") is not None


def test_old_choices_are_trimmed(tmp_path):
    sources = Sources(tmp_path / "s.json")
    for i in range(MAX_SOURCES + 5):
        sources.remember("Artist", f"Track {i}", title="T", url=f"https://youtu.be/{i}")
    assert len(sources._chosen) == MAX_SOURCES
    assert sources.chosen("Artist", "Track 0") is None
    assert sources.chosen("Artist", f"Track {MAX_SOURCES + 4}") is not None
