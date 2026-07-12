#!/usr/bin/env python3
"""PreToolUse hook for Edit|Write|MultiEdit.

Denies the edit unless an agent-skill has been invoked recently, per
CLAUDE.md's Agent Skills Workflow ("the matching skill invoked fresh at
the point it applies, every time, not once per session"). Self-discipline
alone failed twice in one session before this was added — this makes the
requirement mechanical instead of a promise.

Freshness is tracked via the mtime of .claude/.skill-marker, touched by a
companion PostToolUse hook on the Skill tool (see .claude/settings.json).
No "small change" exception: any gated file extension is blocked equally.
"""

import json
import os
import sys
import time

GATED_EXTENSIONS = (".py", ".html", ".js", ".css", ".json", ".svg")
MAX_MARKER_AGE_SECONDS = 30 * 60
MARKER_PATH = ".claude/.skill-marker"


def main():
    data = json.load(sys.stdin)
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not file_path.endswith(GATED_EXTENSIONS):
        print(json.dumps({"continue": True}))
        return

    age = MAX_MARKER_AGE_SECONDS + 1
    if os.path.exists(MARKER_PATH):
        age = time.time() - os.path.getmtime(MARKER_PATH)

    if age > MAX_MARKER_AGE_SECONDS:
        reason = (
            "No agent-skill invoked in the last 30 minutes. Per CLAUDE.md's Agent Skills "
            "Workflow, invoke the matching skill (frontend-ui-engineering, "
            "test-driven-development, debugging-and-error-recovery, code-review-and-quality, "
            "code-simplification, etc.) before this edit -- no exception for small changes."
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
