#!/usr/bin/env python3
"""PreToolUse hook for Bash.

Targets the specific phase where a real bug shipped: a pagination fix
landed without code-review-and-quality ever being invoked before the
commit. Denies `git commit` unless code-review-and-quality has fired
recently, and additionally requires security-and-hardening if any staged,
gated file touches an auth/payment-sensitive path (see
SENSITIVE_PATH_MARKERS in _common.py).

Only gates commits that stage at least one file with a GATED_EXTENSIONS
suffix -- a docs-only commit is not blocked, matching check_skill_invoked.py.
"""

import json
import os
import re
import subprocess
import sys
import time

from _common import (
    GATED_EXTENSIONS,
    MAX_MARKER_AGE_SECONDS,
    SENSITIVE_PATH_MARKERS,
    SKILL_MARKERS_DIR,
)

COMMIT_RE = re.compile(r"(?:^|[;&|])\s*git\s+(-C\s+\S+\s+)?commit\b")
ADD_RE = re.compile(r"git\s+add\s+([^;&|]*)")
WILDCARD_ADD_FLAGS = {"-A", "--all", "-u", "--update", "."}
COMMIT_ALL_RE = re.compile(r"git\s+commit\s+(?:\S+\s+)*(-a\b|--all\b|-am\b)")


def marker_age(skill_name):
    path = os.path.join(SKILL_MARKERS_DIR, skill_name)
    if not os.path.exists(path):
        return MAX_MARKER_AGE_SECONDS + 1
    return time.time() - os.path.getmtime(path)


def staged_files():
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.splitlines() if f]


def files_about_to_be_added(command):
    """Files a `git add` in this same command would stage before the commit runs.

    The hook fires before the whole command executes, so `git diff --cached`
    alone misses a chained `git add X && git commit` -- X isn't staged yet at
    check time. Returns (paths, unknown): unknown is True when the add can't
    be resolved to specific extensions (wildcard/-A/-u/. or `commit -a`), in
    which case the caller should fail closed instead of trusting extensions.
    """
    paths = []
    unknown = bool(COMMIT_ALL_RE.search(command))
    for match in ADD_RE.finditer(command):
        for arg in match.group(1).split():
            if arg in WILDCARD_ADD_FLAGS:
                unknown = True
            elif not arg.startswith("-"):
                paths.append(arg)
    return paths, unknown


def main():
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")

    if not COMMIT_RE.search(command):
        print(json.dumps({"continue": True}))
        return

    pending_paths, unknown_scope = files_about_to_be_added(command)
    gated_files = [f for f in staged_files() if f.endswith(GATED_EXTENSIONS)]
    gated_files += [p for p in pending_paths if p.endswith(GATED_EXTENSIONS)]

    if not gated_files and not unknown_scope:
        print(json.dumps({"continue": True}))
        return

    missing = []
    if marker_age("code-review-and-quality") > MAX_MARKER_AGE_SECONDS:
        missing.append("code-review-and-quality")

    touches_sensitive = unknown_scope or any(
        marker in gated_file.lower()
        for gated_file in gated_files
        for marker in SENSITIVE_PATH_MARKERS
    )
    if (
        touches_sensitive
        and marker_age("security-and-hardening") > MAX_MARKER_AGE_SECONDS
    ):
        missing.append("security-and-hardening")

    if missing:
        if gated_files:
            shown = ", ".join(gated_files[:5])
            if len(gated_files) > 5:
                shown += ", ..."
        else:
            shown = "an unresolvable `git add` scope (wildcard/-A/-a)"
        verb = "haven't" if len(missing) > 1 else "hasn't"
        reason = (
            f"git commit blocked: staged changes include {shown}, but "
            f"{' and '.join(missing)} {verb} been invoked recently. Per "
            "CLAUDE.md's Agent Skills Workflow, run it before committing."
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    else:
        print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
