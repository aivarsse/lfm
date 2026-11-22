.PHONY: check lint types test test-fast guard
check: lint types test

lint:
	uv run ruff check .
	uv run ruff format --check .

types:
	uv run ty check src tests

test:
	uv run pytest --cov=lfm --cov-branch --cov-report=xml --cov-report=term-missing

# Same suite across workers, for a faster local loop. Not the default: the serial
# run is the reproducible one, and CI keeps it that way.
test-fast:
	uv run pytest -n auto --cov=lfm --cov-branch --cov-report=xml --cov-report=term-missing

# The pull-request guard CI runs, which is a script precisely so it can be run here
# against any two refs: this is the check that decides whether a change weakened the
# gates, and reading it before pushing beats being told by a red job.
guard:
	uv run python .gitea/guard.py --base $${BASE:-origin/main} --head $${HEAD:-HEAD}
