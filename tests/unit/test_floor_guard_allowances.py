"""Task 60. FLOOR-ALLOW: the escape clause CONSTRAINTS.md already documents.

CONSTRAINTS.md's floor rule reads "No skipped, deleted, or weakened tests
**without a reason in the commit message**". The guard never implemented
that second half -- it flagged every assertion change unconditionally,
which made it stricter than the written rule and left no sanctioned way
to correct a test that was itself wrong.

These tests cover the parser only. They deliberately do NOT shell out to
git: the parsing is the part that decides whether a real weakening slips
through, and it must be testable without constructing a repository.
"""

import pytest

# scripts/ is on the path via pyproject.toml's pytest `pythonpath` setting
# -- see the comment there for why this imports as a top-level module
# rather than scripts.floor_guard.
from floor_guard import allowed_findings, parse_floor_allowances


def test_no_allowances_in_an_ordinary_commit_message():
    message = "Fix the thing\n\nA perfectly normal commit body.\n"

    assert parse_floor_allowances(message) == set()


def test_parses_a_rule_and_path_pair():
    message = (
        "Correct a test that pinned a bug\n\n"
        "FLOOR-ALLOW: assertion-removed tests/feature/orders/test_checkout.py\n"
        "Reason: the assertion pinned the exact broken value.\n"
    )

    assert parse_floor_allowances(message) == {
        ("assertion-removed", "tests/feature/orders/test_checkout.py")
    }


def test_parses_several_allowances_across_several_commits():
    message = (
        "FLOOR-ALLOW: assertion-removed tests/a/test_one.py\n"
        "Reason: the first one was wrong.\n"
        "FLOOR-ALLOW: test-file-deleted tests/b/test_two.py\n"
        "Reason: the second one was wrong too.\n"
    )

    assert parse_floor_allowances(message) == {
        ("assertion-removed", "tests/a/test_one.py"),
        ("test-file-deleted", "tests/b/test_two.py"),
    }


def test_is_case_insensitive_on_the_marker_but_not_the_path():
    message = (
        "floor-allow: assertion-removed tests/a/test_one.py\n"
        "reason: lowercase markers are still markers.\n"
    )

    assert parse_floor_allowances(message) == {
        ("assertion-removed", "tests/a/test_one.py")
    }


def test_ignores_a_marker_with_no_path():
    """A bare "FLOOR-ALLOW: assertion-removed" must not become a blanket
    pass for every file in the change -- that would be the
    .constraintsignore failure mode this exists to avoid."""
    message = "FLOOR-ALLOW: assertion-removed\nReason: no path was given.\n"

    assert parse_floor_allowances(message) == set()


def test_ignores_an_unknown_rule_name():
    """A typo must fail closed. Silently accepting "assertion-removeed"
    would mean the author believes they filed an allowance while the
    guard believes it blocked nothing."""
    message = (
        "FLOOR-ALLOW: assertion-removeed tests/a/test_one.py\n"
        "Reason: a typo in the rule name must not pass.\n"
    )

    assert parse_floor_allowances(message) == set()


@pytest.mark.parametrize(
    "rule",
    ["secret-in-source", "silenced-checker", "unfinished-work", "threshold-lowered"],
)
def test_the_most_serious_rules_can_never_be_allowed_away(rule):
    """FLOOR-ALLOW covers ONLY the test-correction rules. A committed
    secret or a lowered threshold is never something a commit message
    should be able to wave through -- CONSTRAINTS.md's "reason in the
    commit message" clause is scoped to skipped/deleted/weakened tests,
    and nothing else."""
    message = (
        f"FLOOR-ALLOW: {rule} apps/anything.py\n"
        "Reason: a reason must never unlock a non-test rule.\n"
    )

    assert parse_floor_allowances(message) == set()


def test_allowed_findings_removes_only_the_matching_finding():
    findings = [
        ("assertion-removed", "tests/a/test_one.py", "assert x"),
        ("assertion-removed", "tests/b/test_two.py", "assert y"),
    ]
    allowances = {("assertion-removed", "tests/a/test_one.py")}

    remaining = allowed_findings(findings, allowances)

    assert remaining == [("assertion-removed", "tests/b/test_two.py", "assert y")]


def test_allowed_findings_will_not_let_one_rule_excuse_another():
    """An allowance for assertion-removed in a file must not also excuse
    that same file being deleted outright."""
    findings = [("test-file-deleted", "tests/a/test_one.py", "removed")]
    allowances = {("assertion-removed", "tests/a/test_one.py")}

    assert allowed_findings(findings, allowances) == findings


def test_allowed_findings_is_unchanged_when_there_are_no_allowances():
    findings = [("assertion-removed", "tests/a/test_one.py", "assert x")]

    assert allowed_findings(findings, set()) == findings


# CodeRabbit (PR #91): an allowance was accepted with no reason at all, so
# "FLOOR-ALLOW: assertion-removed path/to/test.py" on its own could make
# the guard report clean. CONSTRAINTS.md's rule is "without a reason in
# the commit message" -- a reason-free allowance is exactly the thing the
# rule refuses, and the guard's own help text tells authors to write one.


def test_an_allowance_with_no_reason_is_rejected():
    message = "FLOOR-ALLOW: assertion-removed tests/a/test_one.py\n"

    assert parse_floor_allowances(message) == set()


def test_an_allowance_with_an_empty_reason_is_rejected():
    message = "FLOOR-ALLOW: assertion-removed tests/a/test_one.py\nReason:   \n"

    assert parse_floor_allowances(message) == set()


def test_an_allowance_with_a_reason_is_accepted():
    message = (
        "FLOOR-ALLOW: assertion-removed tests/a/test_one.py\n"
        "Reason: the assertion pinned a value that was itself the bug.\n"
    )

    assert parse_floor_allowances(message) == {
        ("assertion-removed", "tests/a/test_one.py")
    }


def test_a_reason_may_span_several_lines():
    message = (
        "FLOOR-ALLOW: assertion-removed tests/a/test_one.py\n"
        "Reason: the first line of the explanation,\n"
        "continuing onto a second line.\n"
    )

    assert parse_floor_allowances(message) == {
        ("assertion-removed", "tests/a/test_one.py")
    }


def test_a_reason_does_not_carry_over_to_a_later_reasonless_allowance():
    """Two allowances, one reason. Only the one that owns the reason is
    accepted -- otherwise a single reason would launder any number of
    unexplained allowances filed after it."""
    message = (
        "FLOOR-ALLOW: assertion-removed tests/a/test_one.py\n"
        "Reason: a real explanation for this one.\n"
        "FLOOR-ALLOW: test-file-deleted tests/b/test_two.py\n"
    )

    assert parse_floor_allowances(message) == {
        ("assertion-removed", "tests/a/test_one.py")
    }
