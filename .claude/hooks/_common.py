"""Shared constants for the Agent Skills Workflow gating hooks."""

GATED_EXTENSIONS = (".py", ".html", ".js", ".css", ".json", ".svg")
MAX_MARKER_AGE_SECONDS = 30 * 60
SKILL_MARKERS_DIR = ".claude/.skill-markers"

# Substrings checked against staged file paths (lowercased) to decide whether
# a commit touches auth/payment-sensitive code and needs security-and-hardening
# on top of the standard code-review-and-quality gate.
SENSITIVE_PATH_MARKERS = (
    "apps/accounts/",
    "apps/distributors/",
    "apps/wallet/",
    "apps/withdrawal/",
    "apps/notifications/",
    "paystack",
    "mnotify",
)


def skill_marker_name(skill: str) -> str:
    name = skill.split(":")[-1]
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
