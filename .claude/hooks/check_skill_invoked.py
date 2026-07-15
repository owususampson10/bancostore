#!/usr/bin/env python3
"""PreToolUse hook for Edit|Write|MultiEdit.

Denies the edit unless an agent-skill has been invoked recently, per
CLAUDE.md's Agent Skills Workflow ("the matching skill invoked fresh at
the point it applies, every time, not once per session"). Self-discipline
alone failed twice in one session before this was added — this makes the
requirement mechanical instead of a promise.

Freshness is tracked via per-skill marker files in .claude/.skill-markers/,
touched by a companion PostToolUse hook on the Skill tool (see
record_skill_invocation.py and .claude/settings.json). This gate only checks
that *some* skill fired recently; check_commit_review.py separately checks
*which* skill fired before a commit. No "small change" exception: any gated
file extension is blocked equally.
"""

import glob
import json
import os
import sys
import time

from _common import GATED_EXTENSIONS, MAX_MARKER_AGE_SECONDS, SKILL_MARKERS_DIR


def freshest_marker_age():
    markers = glob.glob(os.path.join(SKILL_MARKERS_DIR, "*"))
    if not markers:
        return MAX_MARKER_AGE_SECONDS + 1
    return time.time() - max(os.path.getmtime(m) for m in markers)


def main():
    data = json.load(sys.stdin)
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not file_path.endswith(GATED_EXTENSIONS):
        print(json.dumps({"continue": True}))
        return

    age = freshest_marker_age()

    if age > MAX_MARKER_AGE_SECONDS:
        reason = (
            "No agent-skill invoked in the last 30 minutes. Per CLAUDE.md's "
            "Agent Skills Workflow, invoke the matching skill "
            "(frontend-ui-engineering, test-driven-development, "
            "debugging-and-error-recovery, code-review-and-quality, "
            "code-simplification, etc.) before this edit -- no exception "
            "for small changes."
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
