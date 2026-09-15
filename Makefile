# Constraint checks, grouped by cost (see CONSTRAINTS.md).
#
# The grouping matters more than the tools: a check that stalls the edit loop
# gets switched off, and a gate people switched off is worse than no gate,
# because the bar still looks like it exists.
#
#   check-fast   after an edit          seconds
#   check-task   when a task is done    under ~90s
#   check-full   in CI                  minutes
#
# CONSTRAINTS.md is the canonical source for every rule below -- it carries the
# reason alongside each command. These targets are convenience wrappers that
# must mirror it. If the two ever drift, the file wins.

BASE ?= origin/main

.PHONY: check-fast check-task check-full coverage guard ratchet

# Formatting, lint and the floor. No test run, no network, no new tooling.
check-fast:
	black --check .
	isort --check .
	ruff check .
	python scripts/floor_guard.py --base $(BASE)

# Adds the suite plus coverage of the lines this change touched. The lcov
# report is written once here and read by the coverage check -- the suite is
# never run twice to produce a number.
check-task: check-fast
	pytest -q --cov=apps --cov=bancostore --cov-report=lcov:coverage.lcov
	python scripts/changed_line_coverage.py --base $(BASE) --min 90 --warn

# Everything, plus the dependency scan. Runs in CI, where a few minutes is fine.
check-full: check-task
	python manage.py makemigrations --check --dry-run
	python manage.py check
	pip-audit -r requirements.txt

# Re-measure project coverage and print the value to record in CONSTRAINTS.md's
# "Measured, not yet enforced" table. Ratchets only ever move up.
ratchet:
	pytest -q --cov=apps --cov=bancostore --cov-report=term | tail -5

# Inspect the working tree for a weakened bar without running anything else.
guard:
	python scripts/floor_guard.py --base $(BASE)
