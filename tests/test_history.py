from lfm.store.history import MAX_HISTORY, History


def test_missing_file_is_empty(tmp_path):
    assert History(tmp_path / "nope.json").names() == []


def test_most_recent_first_and_deduplicated(tmp_path):
    history = History(tmp_path / "h.json")
    history.add("Burial")
    history.add("Kode9")
    history.add("burial")  # the same artist, latest spelling kept
    assert history.names() == ["burial", "Kode9"]


def test_blank_names_are_ignored(tmp_path):
    history = History(tmp_path / "h.json")
    history.add("   ")
    assert history.names() == []


def test_history_is_capped(tmp_path):
    history = History(tmp_path / "h.json")
    for i in range(MAX_HISTORY + 10):
        history.add(f"artist {i}")
    assert len(history.names()) == MAX_HISTORY
    assert history.names()[0] == f"artist {MAX_HISTORY + 9}"


def test_it_round_trips_through_the_file(tmp_path):
    path = tmp_path / "h.json"
    History(path).add("Burial")
    assert History(path).names() == ["Burial"]


def test_a_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("{ not json")
    assert History(path).names() == []


def test_a_failed_write_is_not_fatal(tmp_path):
    path = tmp_path / "h.json"
    path.mkdir()  # a directory where the file should be, so writing fails
    history = History(path)
    history.add("Burial")
    assert history.names() == ["Burial"]


def test_remove_forgets_one_artist_case_insensitively(tmp_path):
    path = tmp_path / "h.json"
    history = History(path)
    history.add("Burial")
    history.add("Kode9")
    assert history.remove("burial") is True
    assert history.names() == ["Kode9"]
    assert History(path).names() == ["Kode9"]  # and it stays forgotten


def test_remove_of_an_unknown_artist_changes_nothing(tmp_path):
    history = History(tmp_path / "h.json")
    history.add("Burial")
    assert history.remove("Nope") is False
    assert history.remove("   ") is False
    assert history.names() == ["Burial"]
