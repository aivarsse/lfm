"""The guard's verdicts, pinned where they can be run.

`.gitea/guard.py` is the pull-request job that flags a change for weakening the gates.
It is tooling rather than shipped code, so it is loaded by path; the cases below are the
two false verdicts it used to return (#145) plus the ones it has to keep returning.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).parents[1] / ".gitea" / "guard.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the guard shells out to git")


def load_guard():
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_guard()


# --- the counts themselves -------------------------------------------------------


def test_each_test_is_counted_on_its_own():
    source = (
        "def test_one():\n"
        "    assert 1\n"
        "    assert 2\n"
        "\n"
        "\n"
        "async def test_two():\n"
        "    assert 3\n"
        "\n"
        "\n"
        "def helper():\n"
        "    assert 4  # not a test, so not counted\n"
    )
    assert guard.test_asserts(source) == {"test_one": 2, "test_two": 1}


def test_a_multi_line_assertion_is_one_assertion():
    source = "def test_one():\n    assert 1 == 1, (\n        'a message about assert'\n    )\n"
    assert guard.test_asserts(source) == {"test_one": 1}


def test_a_file_that_will_not_parse_counts_as_nothing():
    assert guard.test_asserts("def test_one(:\n") == {}


# --- pyproject.toml --------------------------------------------------------------


def test_a_version_only_change_is_a_release_not_a_config_change():
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "index 111..222 100644\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -5,7 +5,7 @@\n"
        " [project]\n"
        ' name = "lfm"\n'
        '-version = "0.40.8"\n'
        '+version = "0.40.9"\n'
        ' description = "x"\n'
    )
    assert guard.is_a_version_bump(diff) is True


def test_anything_else_in_pyproject_still_wants_a_person():
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@\n"
        '-version = "0.40.8"\n'
        '+version = "0.40.9"\n'
        '+    "httpx>=0.28",\n'
    )
    assert guard.is_a_version_bump(diff) is False


def test_deleting_the_version_line_outright_is_not_a_release():
    diff = '--- a/pyproject.toml\n+++ b/pyproject.toml\n@@\n-version = "0.40.8"\n'
    assert guard.is_a_version_bump(diff) is False


# --- end to end, against a scratch repository ------------------------------------

TWO_TESTS = "def test_one():\n    assert 1\n    assert 2\n\n\ndef test_two():\n    assert 3\n"


def write(repo: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    write(repo, files)
    for args in (("add", "-A"), ("commit", "-qm", message)):
        subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True)


def repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "guard@test"),
        ("config", "user.name", "guard"),
    ):
        subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True)
    commit(repo, files, "base")
    return repo


def run_guard(repo: Path) -> subprocess.CompletedProcess:
    """The guard, exactly as the workflow runs it: two refs and an exit code."""
    return subprocess.run(
        [sys.executable, str(GUARD), "--base", "HEAD~1", "--head", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_a_gutted_test_is_caught_even_when_the_suite_grows(tmp_path):
    """#145: the count was summed, so adding a test hid one being emptied."""
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(
        repo,
        {
            "tests/test_x.py": (
                "def test_one():\n"
                "    assert 1\n"
                "\n"
                "\n"
                "def test_two():\n"
                "    assert 3\n"
                "    assert 4\n"
                "    assert 5\n"
            )
        },
        "weaken one test and strengthen another",
    )

    result = run_guard(repo)

    assert result.returncode == 1
    assert "Assertions lost" in result.stdout
    assert "`test_one`: 2 → 1" in result.stdout


def test_adding_a_test_is_not_a_finding(tmp_path):
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(repo, {"tests/test_y.py": "def test_three():\n    assert 1\n"}, "add a test file")

    result = run_guard(repo)

    assert result.returncode == 0
    assert "### Test files changed" in result.stdout  # reported, not failed on


def test_moving_a_test_between_files_is_not_a_finding(tmp_path):
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(
        repo,
        {
            "tests/test_x.py": "def test_two():\n    assert 3\n",
            "tests/test_y.py": "def test_one():\n    assert 1\n    assert 2\n",
        },
        "move a test",
    )

    assert run_guard(repo).returncode == 0


def test_losing_a_test_is_a_finding(tmp_path):
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(
        repo, {"tests/test_x.py": "def test_one():\n    assert 1\n    assert 2\n"}, "drop a test"
    )

    result = run_guard(repo)

    assert result.returncode == 1
    assert "Tests that no longer exist" in result.stdout
    assert "test_two" in result.stdout


def test_a_version_bump_alone_is_not_a_finding(tmp_path):
    """Every release PR was red on this, for the one line that changes nothing but a number."""
    repo = repo_with(tmp_path, {"pyproject.toml": 'name = "lfm"\nversion = "0.40.8"\n'})
    commit(repo, {"pyproject.toml": 'name = "lfm"\nversion = "0.40.9"\n'}, "release")

    result = run_guard(repo)

    assert result.returncode == 0, result.stdout
    assert "CI or config changed" not in result.stdout


def test_a_real_pyproject_change_is_still_a_finding(tmp_path):
    repo = repo_with(tmp_path, {"pyproject.toml": 'name = "lfm"\nversion = "0.40.8"\n'})
    commit(
        repo,
        {"pyproject.toml": 'name = "lfm"\nversion = "0.40.9"\ndependencies = ["httpx"]\n'},
        "a dependency and a release",
    )

    result = run_guard(repo)

    assert result.returncode == 1
    assert "CI or config changed" in result.stdout
    assert "pyproject.toml" in result.stdout


def test_a_change_to_the_gates_themselves_is_a_finding(tmp_path):
    repo = repo_with(tmp_path, {".gitea/workflows/ci.yml": "name: CI\n"})
    commit(repo, {".gitea/workflows/ci.yml": "name: CI\non: push\n"}, "touch the workflow")

    result = run_guard(repo)

    assert result.returncode == 1
    assert ".gitea/workflows/ci.yml" in result.stdout


def test_an_uncommitted_change_is_called_out_rather_than_read_as_an_all_clear(tmp_path):
    """The report is about commits; a dirty tree must not read like a pass."""
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(repo, {"tests/test_y.py": "def test_three():\n    assert 1\n"}, "give HEAD a parent")
    write(repo, {"tests/test_x.py": "def test_one():\n    assert 1\n"})  # never committed

    result = run_guard(repo)

    assert result.returncode == 0  # there is nothing committed to judge
    assert "uncommitted changes" in result.stderr


def test_a_ref_that_will_not_resolve_is_a_message_not_a_traceback(tmp_path):
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})

    result = subprocess.run(
        [sys.executable, str(GUARD), "--base", "no/such/ref", "--head", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "cannot compare no/such/ref with HEAD" in result.stderr
    assert "Traceback" not in result.stderr


def test_an_added_suppression_is_a_finding(tmp_path):
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(
        repo,
        {
            "tests/test_x.py": (
                "import pytest\n"
                "\n"
                "\n"
                '@pytest.mark.skip(reason="later")\n'
                "def test_one():\n"
                "    assert 1\n"
                "    assert 2\n"
                "\n"
                "\n"
                "def test_two():\n"
                "    assert 3\n"
            )
        },
        "skip a test",
    )

    result = run_guard(repo)

    assert result.returncode == 1
    assert "Suppressions added" in result.stdout


def test_a_test_whose_own_data_mentions_a_skip_is_not_a_suppression(tmp_path):
    """The first version of this check flagged the fixture string, not a switched-off test."""
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(
        repo,
        {
            "tests/test_x.py": (
                "SKIPPED_MARKER = '@pytest.mark.skip(reason=\"later\")'\n"
                "\n"
                "\n"
                "def test_one():\n"
                "    assert SKIPPED_MARKER\n"
                "    assert 2\n"
                "\n"
                "\n"
                "def test_two():\n"
                "    assert 3\n"
            )
        },
        "a test about suppressions, containing none",
    )

    result = run_guard(repo)

    assert result.returncode == 0, result.stdout
    assert "Suppressions added" not in result.stdout


def test_a_module_level_conditional_skip_is_not_a_suppression(tmp_path):
    """`pytestmark = pytest.mark.skipif(<tool absent>)` is how this suite says 'not here'."""
    repo = repo_with(tmp_path, {"tests/test_x.py": TWO_TESTS})
    commit(
        repo,
        {
            "tests/test_x.py": (
                "import shutil\n"
                "\n"
                "import pytest\n"
                "\n"
                "pytestmark = pytest.mark.skipif(\n"
                "    shutil.which('git') is None, reason='git is not installed'\n"
                ")\n"
                "\n"
                "\n"
                "def test_one():\n"
                "    assert 1\n"
                "    assert 2\n"
                "\n"
                "\n"
                "def test_two():\n"
                "    assert 3\n"
            )
        },
        "skip where a tool is missing",
    )

    result = run_guard(repo)

    assert result.returncode == 0, result.stdout
    assert "Suppressions added" not in result.stdout
