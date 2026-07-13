#!/usr/bin/env python3
"""PostToolUse hook for the Skill tool.

Touches a marker file named after the invoked skill (namespace prefix
stripped) so PreToolUse gates can check not just that *a* skill fired
recently, but *which* skill -- e.g. requiring code-review-and-quality
specifically before a commit, rather than any skill at all.
"""

import json
import os
import sys

from _common import SKILL_MARKERS_DIR, skill_marker_name


def main():
    data = json.load(sys.stdin)
    skill = data.get("tool_input", {}).get("skill", "")

    if skill:
        os.makedirs(SKILL_MARKERS_DIR, exist_ok=True)
        marker_path = os.path.join(SKILL_MARKERS_DIR, skill_marker_name(skill))
        with open(marker_path, "a"):
            os.utime(marker_path, None)

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
