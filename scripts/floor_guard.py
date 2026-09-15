#!/usr/bin/env python3
"""Diff-scoped enforcement of the CONSTRAINTS.md floor.

Adapted from the `constraint-driven-development` skill's reference
implementation (references/floor-guard.md), which ships as Node. This is a
Python port for one reason: the floor runs in the fast edit loop, and every
other check in that loop (black/isort/ruff/pytest) is Python already -- making
the cheapest, most-often-run gate the only one that needs Node would be the
wrong dependency to add. The *contract* is deliberately identical to the
reference; only the patterns and the host language differ.

Contract (unchanged from the reference):
  - Input is the diff between the merge base and the working tree, including
    untracked files. Reading plain `git diff` alone would miss brand-new files.
  - Detects the five "cheap road to green" moves: a weakened threshold in
    CONSTRAINTS.md, a test made easier, a silenced checker, unfinished work,
    and a new Exceptions row.
  - Exit codes: 0 clean, 1 at least one floor violation, 2 the guard could not
    run. A 2 must never read as a 0.
  - Reports the rule and the location, never a matched secret value.
  - Tightening the bar is silent; only moves that LOWER it are surfaced.

Usage:
    python scripts/floor_guard.py [--base <ref>]      # default base: origin/main
"""

from __future__ import annotations

import argparse
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

# Python puts a script's own directory on sys.path, so this resolves whenever
# the guard is run the documented way (`python scripts/floor_guard.py`). Left
# as a plain import deliberately: the alternative is a sys.path insert plus an
# E402 suppression comment, and silencing a linter in this file of all files
# would be the exact move it exists to catch.
from _gitdiff import (
    REPO_ROOT,
    collect_raw_diff,
    git,
    parse_new_file_header,
    require_merge_base,
)

# Only source files are pattern-scanned. Without this, `tasks/todo.md` (which
# legitimately contains the word TODO several hundred times) and the project's
# own prose docs would bury every real finding in noise.
SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".html",
    ".css",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".sh",
}

# 3. A checker got silenced. Python-first, plus the frontend forms that can
# appear in this codebase's templates and Vite config.
SUPPRESSIONS = re.compile(
    r"#\s*noqa"
    r"|#\s*type:\s*ignore"
    r"|#\s*nosec"
    r"|#\s*pragma:\s*no\s*cover"  # the coverage equivalent of `istanbul ignore`
    r"|nosemgrep"
    r"|gitleaks:allow"
    r"|eslint-disable"
    r"|stylelint-disable"
    r"|@ts-ignore"
)

# 4. Work is unfinished. `except: pass` matters more than usual here -- this is
# a financial platform, and a swallowed exception in the commission/wallet path
# turns a failed money movement into silence.
STUBS = re.compile(
    r"raise\s+NotImplementedError"
    r"|except[^:]*:\s*pass\s*$"
    r"|\bTODO\b"
    r"|\bFIXME\b"
    r"|\bXXX\b"
)

# 2. A test got easier. `skipif` is caught by the `skip` prefix on purpose:
# this repo has exactly one legitimate skipif (WeasyPrint/Pango, CI-only), and
# it is already committed, so a diff-scoped guard never sees it. A NEW one
# should be argued for out loud.
SKIPS = re.compile(
    r"@pytest\.mark\.skip"
    r"|@pytest\.mark\.xfail"
    r"|pytest\.skip\("
    r"|@unittest\.skip"
    r"|\.skip\("
    r"|\bxdescribe\(|\bxit\("
)

# High-confidence secret shapes only. gitleaks does the real scan in CI (it
# installs machine-wide, which is unreliable on this project's macOS 12 dev
# box -- see CONSTRAINTS.md's `Runs at` column). This local check exists so an
# obviously-live Paystack/mNotify/Didit key cannot reach a commit while the
# real scanner is a push away. Matches are NEVER printed (contract above).
SECRETS = re.compile(
    r"\bsk_live_[0-9a-zA-Z]{8,}"
    r"|\bsk_test_[0-9a-zA-Z]{8,}"
    r"|\bpk_live_[0-9a-zA-Z]{8,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
)

# A literal assigned to a secret-shaped name. Excluded: anything reading from
# the environment, and this project's own deliberate CI placeholders.
SECRET_ASSIGNMENT = re.compile(
    r"(SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|TOKEN|PRIVATE_KEY)\w*\s*[=:]\s*"
    r"[\"'][^\"']{16,}[\"']",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_ALLOWED = re.compile(
    r"os\.environ|os\.getenv|env\(|config\(|getenv"
    r"|ci-only-dummy|placeholder|example\.com|changeme|your-.*-here",
    re.IGNORECASE,
)

TEST_FILE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$")
ASSERTION = re.compile(r"\bassert\b|\bassertRaises\b|pytest\.raises")
EXCEPTION_ROW = re.compile(r"^\|\s*(W|E)\d+\s*\|")


def load_ignores() -> list[str]:
    """One glob per line. A tracked exemption file, never a loosened rule."""
    path = REPO_ROOT / ".constraintsignore"
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def is_ignored(filename: str, globs: list[str]) -> bool:
    return any(fnmatch(filename, pattern) for pattern in globs)


def is_source(filename: str) -> bool:
    return Path(filename).suffix in SOURCE_SUFFIXES


def collect_diff(base: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (added, removed) as (filename, line_text) pairs.

    Covers tracked changes and untracked new files alike. Binary files produce
    no +/- lines from `git diff`, so they fall out naturally.
    """
    diff = collect_raw_diff(base, "floor-guard")

    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    current: str | None = None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            current = parse_new_file_header(line)
        elif line.startswith("--- "):
            continue
        elif current is None:
            # A deleted file's hunk. Its removals are covered by
            # deleted_test_files(); attributing them to a path that no longer
            # exists would only produce misleading findings.
            continue
        elif line.startswith("+"):
            added.append((current, line[1:]))
        elif line.startswith("-"):
            removed.append((current, line[1:]))
    return added, removed


def deleted_test_files(base: str) -> list[str]:
    merge_base = require_merge_base(base, "floor-guard")
    out = git(["diff", "--diff-filter=D", "--name-only", merge_base, "--"]) or ""
    return [f for f in out.split("\n") if f and TEST_FILE.search(f)]


def numbers(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]


def constraints_file_is_new(base: str) -> bool:
    """True when CONSTRAINTS.md did not exist at the merge base.

    The new-exception rule exists to catch a row added to an ESTABLISHED bar.
    On the commit that first creates CONSTRAINTS.md, every row is necessarily
    new and there is no prior bar to weaken -- the whole file is under review
    as one unit. Narrow on purpose: this never applies to a file that already
    existed, which is the case the rule is actually aimed at.
    """
    merge_base = require_merge_base(base, "floor-guard")
    existing = git(["ls-tree", "--name-only", merge_base, "CONSTRAINTS.md"])
    return not (existing or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()

    if git(["rev-parse", "--git-dir"]) is None:
        print("floor-guard: not a git repository", file=sys.stderr)
        return 2

    added, removed = collect_diff(args.base)
    ignores = load_ignores()
    bootstrapping = constraints_file_is_new(args.base)
    findings: list[tuple[str, str, str]] = []

    def flag(rule: str, filename: str, text: str) -> None:
        findings.append((rule, filename, text.strip()[:120]))

    for filename, text in added:
        if is_ignored(filename, ignores):
            continue

        # Secrets are checked in every added line regardless of extension --
        # a key pasted into a .env.example or a deploy note is still a key.
        if SECRETS.search(text) or (
            SECRET_ASSIGNMENT.search(text)
            and not SECRET_ASSIGNMENT_ALLOWED.search(text)
        ):
            # Location and rule only. Never the value.
            flag("secret-in-source", filename, "<redacted>")

        if (
            not bootstrapping
            and filename.endswith("CONSTRAINTS.md")
            and EXCEPTION_ROW.match(text.strip())
        ):
            flag("new-exception", filename, text)

        if not is_source(filename):
            continue
        if SUPPRESSIONS.search(text):
            flag("silenced-checker", filename, text)
        if STUBS.search(text):
            flag("unfinished-work", filename, text)
        if SKIPS.search(text):
            flag("test-made-easier", filename, text)

    # An assertion pulled out of a test file that still exists.
    for filename, text in removed:
        if is_ignored(filename, ignores):
            continue
        if TEST_FILE.search(filename) and ASSERTION.search(text):
            flag("assertion-removed", filename, text)

    for filename in deleted_test_files(args.base):
        flag("test-file-deleted", filename, "entire test file removed")

    # A threshold in CONSTRAINTS.md that moved down. Matched by row label so a
    # reordered table does not read as a change.
    removed_rows = [(f, t) for f, t in removed if f.endswith("CONSTRAINTS.md")]
    added_rows = [(f, t) for f, t in added if f.endswith("CONSTRAINTS.md")]
    for filename, old in removed_rows:
        label = re.split(r"[|:]", old)[0].strip()
        if not label:
            continue
        match = next(
            (
                new
                for _, new in added_rows
                if re.split(r"[|:]", new)[0].strip() == label
            ),
            None,
        )
        if match is None:
            continue
        old_nums, new_nums = numbers(old), numbers(match)
        if any(
            new < old_nums[i] for i, new in enumerate(new_nums) if i < len(old_nums)
        ):
            flag("threshold-lowered", filename, f"{old.strip()}  ->  {match.strip()}")

    if not findings:
        print("floor-guard: clean")
        return 0

    print(f"floor-guard: {len(findings)} floor violation(s):", file=sys.stderr)
    for rule, filename, text in findings:
        print(f"  [{rule}] {filename}: {text}", file=sys.stderr)
    print(
        "\nEach is a move that lowers the bar. Fix the code, or route it "
        "through a tracked exception in CONSTRAINTS.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
