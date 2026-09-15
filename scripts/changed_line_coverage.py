#!/usr/bin/env python3
"""Coverage of the lines this change touched, not the whole repo.

Project-wide coverage is a number the current change inherited; coverage of
changed lines is one it can actually move. This reads the lcov report the test
run already wrote and intersects it with the diff -- it never runs the suite a
second time, which is the fastest way to make a coverage gate unbearable.

Usage:
    pytest --cov=apps --cov=bancostore --cov-report=lcov:coverage.lcov
    python scripts/changed_line_coverage.py [--base <ref>] [--min 80] [--warn]

Exit codes: 0 pass, 1 below threshold, 2 could not run (no report, no diff).
With --warn, a below-threshold result reports and still exits 0.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# See the note in floor_guard.py: plain import, no sys.path shim, no noqa.
from _gitdiff import REPO_ROOT, collect_raw_diff, parse_new_file_header

# Only application code is gated. Migrations are generated, and the tests
# themselves are not what a coverage number is measuring.
MEASURED_PREFIXES = ("apps/", "bancostore/")
EXCLUDED = re.compile(r"/migrations/|/tests?/|conftest\.py$")


def parse_lcov(path: Path) -> dict[str, dict[int, int]]:
    """{relative_path: {line_number: hit_count}} from an lcov tracefile."""
    coverage: dict[str, dict[int, int]] = {}
    current: dict[int, int] | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            filename = line[3:]
            try:
                filename = str(Path(filename).resolve().relative_to(REPO_ROOT))
            except ValueError:
                pass  # Already relative, or outside the repo.
            current = coverage.setdefault(filename, {})
        elif line.startswith("DA:") and current is not None:
            number, _, hits = line[3:].partition(",")
            try:
                current[int(number)] = int(hits)
            except ValueError:
                continue
        elif line == "end_of_record":
            current = None
    return coverage


def changed_lines(base: str) -> dict[str, set[int]]:
    """{relative_path: {added line numbers}} between the merge base and now."""
    diff = collect_raw_diff(base, "changed-line-coverage")

    result: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            current = parse_new_file_header(line)
        elif line.startswith("@@"):
            # @@ -old,count +new,count @@ -- current is None for a deleted
            # file, whose lines no longer exist to be covered by anything.
            if current is None:
                continue
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) else 1
            if count:
                result.setdefault(current, set()).update(range(start, start + count))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    # 90, not the conventional 80: this codebase measured 94.9% when the bar
    # was set, so an 80% rule would let new code land below the standard
    # already being met. See CONSTRAINTS.md, "Why these numbers".
    parser.add_argument("--min", type=float, default=90.0)
    parser.add_argument("--report", default="coverage.lcov")
    parser.add_argument(
        "--warn",
        action="store_true",
        help="report a shortfall but exit 0 (see CONSTRAINTS.md: warn phase)",
    )
    args = parser.parse_args()

    report = REPO_ROOT / args.report
    if not report.exists():
        print(
            f"changed-line-coverage: no {args.report}. Run pytest with "
            f"--cov-report=lcov:{args.report} first.",
            file=sys.stderr,
        )
        return 2

    coverage = parse_lcov(report)
    changed = changed_lines(args.base)

    covered = missed = 0
    gaps: list[tuple[str, list[int]]] = []

    for filename, lines in sorted(changed.items()):
        if not filename.startswith(MEASURED_PREFIXES) or EXCLUDED.search(filename):
            continue
        file_coverage = coverage.get(filename)
        if not file_coverage:
            continue
        # Only executable lines appear in the lcov DA records, so blank lines,
        # comments and docstrings drop out here without special-casing.
        uncovered = []
        for number in sorted(lines):
            if number not in file_coverage:
                continue
            if file_coverage[number] > 0:
                covered += 1
            else:
                missed += 1
                uncovered.append(number)
        if uncovered:
            gaps.append((filename, uncovered))

    total = covered + missed
    if total == 0:
        print("changed-line-coverage: no measurable application lines changed")
        return 0

    percent = 100.0 * covered / total
    print(f"changed-line-coverage: {percent:.1f}% ({covered}/{total} lines)")

    if percent >= args.min:
        return 0

    print(
        f"\nBelow the {args.min:.0f}% floor. Uncovered lines this change added:",
        file=sys.stderr,
    )
    for filename, lines in gaps:
        preview = ", ".join(str(n) for n in lines[:12])
        suffix = f" (+{len(lines) - 12} more)" if len(lines) > 12 else ""
        print(f"  {filename}: {preview}{suffix}", file=sys.stderr)

    if args.warn:
        print("\n(warn mode -- not blocking)", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
