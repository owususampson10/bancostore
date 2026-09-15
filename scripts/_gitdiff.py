"""Shared git-diff plumbing for the CONSTRAINTS.md checks.

`floor_guard.py` and `changed_line_coverage.py` both need the same thing: the
diff between the merge base and the working tree, including untracked files
(plain `git diff` cannot see a brand-new file). They consume it differently --
one wants the text of added and removed lines, the other wants added line
*numbers* -- but the plumbing underneath is identical.

Extracted after the same off-by-one in the `+++ /dev/null` header parse had to
be fixed in both copies. A bug that needs fixing twice is duplication worth
removing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Git writes this as the "new file" header for a deletion. It is not a path,
# and slicing it like one yields the garbage filename "ev/null".
DEV_NULL = "/dev/null"


def git(args: list[str]) -> str | None:
    """Run a git command, returning None rather than raising on failure."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def require_merge_base(base: str, tool: str) -> str:
    """Resolve the merge base, or exit 2 ("could not run").

    Exiting rather than returning a sentinel is deliberate: by contract a
    guard that could not run must never be mistaken for one that passed.
    """
    resolved = git(["merge-base", base, "HEAD"])
    if not resolved:
        print(f"{tool}: no merge base against {base}", file=sys.stderr)
        sys.exit(2)
    return resolved.strip()


def collect_raw_diff(base: str, tool: str) -> str:
    """Unified diff (zero context) of tracked changes plus untracked files."""
    merge_base = require_merge_base(base, tool)
    diff = git(["diff", "--unified=0", merge_base, "--"]) or ""

    untracked = (git(["ls-files", "--others", "--exclude-standard"]) or "").split("\n")
    for filename in filter(None, untracked):
        # --no-index exits non-zero whenever the files differ, which is the
        # normal case here, so this deliberately bypasses git() (which treats
        # a non-zero exit as failure).
        result = subprocess.run(
            ["git", "diff", "--no-index", "--unified=0", DEV_NULL, filename],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        diff += "\n" + result.stdout
    return diff


def parse_new_file_header(line: str) -> str | None:
    """Filename from a `+++ b/path` header, or None for a deleted file.

    Returning None for `+++ /dev/null` is the point of this helper: the naive
    six-character slice turns that header into the path "ev/null", which then
    silently fails every extension and test-path check downstream.
    """
    path = line[4:].strip()
    if path == DEV_NULL:
        return None
    # Git prefixes the new side with "b/" unless --no-prefix is in play.
    return path[2:] if path.startswith("b/") else path
