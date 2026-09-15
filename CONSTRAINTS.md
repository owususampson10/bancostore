# Constraints

Last reviewed: 2026-09-15 by @owususampson10

This file is the canonical record of what "good enough to ship" means for
Bancostore. `SPEC.md` says what to build and the test suite proves it works;
this file defines the bar both are held to, decided once rather than argued
about per pull request.

Bancostore is a financial platform — a background job moves real money through
Paystack every ten minutes, minus GRA withholding tax. The bar is set with that
in mind, not as generic hygiene.

---

## Floor (always enforced, blocking, no setup required)

These pass on the codebase as it stands today, so enforcing them from day one
gates nothing that already exists. Checked by `scripts/floor_guard.py`, which is
diff-scoped — it only ever looks at what a change touched.

- **No new suppression comments**: `# noqa`, `# type: ignore`, `# nosec`,
  `# pragma: no cover`, `nosemgrep`, `gitleaks:allow`, `eslint-disable`,
  `stylelint-disable`
- **No unimplemented stubs**: `raise NotImplementedError`, `except ...: pass`,
  a bare `TODO`/`FIXME`/`XXX` standing where an implementation should be. An
  empty `except` matters more than usual here — a swallowed exception in the
  commission or wallet path turns a failed money movement into silence.
- **No skipped, deleted, or weakened tests** without a reason in the commit
  message: a new `@pytest.mark.skip`/`skipif`/`xfail`, a deleted test file, or
  an assertion removed from a test that stayed
- **No secrets in source** — Paystack, mNotify, Didit, or Django `SECRET_KEY`
  material. Matches are reported by rule and location only, never by value.
- **This file does not get weakened to make a change pass.** A threshold edited
  downwards, or a new Exceptions row, is itself a finding.

Tightening the bar is silent. Loosening it is loud.

### Why these five

They are not a general code-quality list. They are the five moves an agent
actually makes when it hits a red check and takes the cheapest road to green.
This repo has lived through the failure: `tests/feature/distributors/test_earnings_history.py`
asserted a hard-coded pre-hash asset filename, silently broke at Task 36a's Vite
cache-busting migration, and was written off as "known pre-existing flakiness"
across Tasks 44 through 48 before anyone actually fixed it. A diff-scoped guard
would have flagged the weakening the day it happened.

---

## Enforced with numbers

Every row names the command that produces the verdict. A dimension with a number
and no command in the `Checked by` column is an aspiration, not a constraint.

| Dimension | Rule | Checked by | Runs at | Status |
|-----------|------|-----------|---------|--------|
| Formatting | Zero diffs | `black --check .` | every edit, CI | **block** |
| Import order | Zero diffs | `isort --check .` | every edit, CI | **block** |
| Lint | Zero errors (`E`,`F`,`W`) | `ruff check .` | every edit, CI | **block** |
| Floor | Zero violations | `python scripts/floor_guard.py --base origin/main` | every edit, CI | **block** |
| Tests | Full suite green on **real MySQL** | `pytest -q` | CI | **block** |
| Migrations | No un-generated model changes | `python manage.py makemigrations --check --dry-run` | CI | **block** |
| Django checks | Zero issues | `python manage.py check` | CI | **block** |
| Coverage | Changed lines ≥ 90% | `python scripts/changed_line_coverage.py --base origin/main --min 90` | task end, CI | warn → block 2026-09-29 |
| Security: deps | Nothing new above today's baseline | `pip-audit -r requirements.txt` | CI | warn (see E3) |

### Why these numbers

- **Changed lines ≥ 90%, not the conventional 80%** — this codebase measured
  **94.9%** on the day the bar was set. An 80% rule would have permitted every
  new change to land below the standard already being met, which is a ratchet
  pointing the wrong way. 90% sits just under today's level: high enough that
  new logic needs a test, low enough that a config line or a `__str__` doesn't
  fail a build. It applies to the lines a change touched, never the whole repo —
  project coverage is a number a change inherited, changed-line coverage is one
  it can actually move.
- **Warn until 2026-09-29** — a two-week window to see the real numbers on live
  work before either becomes a gate. A threshold adopted before anyone has
  watched it run is a threshold people learn to route around. Both rows get
  re-decided on that date, not silently extended.
- **Real MySQL for the suite** — non-negotiable and already the case. SQLite
  drops `SELECT ... FOR UPDATE` entirely (`has_select_for_update` is `False`),
  so every concurrency guarantee in the wallet, PV-ledger and commission code is
  unverified locally no matter how many tests pass.

### Elevated rigor (unchanged from SPEC.md, restated here so it is in one place)

`apps/commissions/services.py`, `apps/wallet/services.py`, and
`apps/withdrawal/services.py` require a test for **every code path** before
merge — weak-leg selection, carry-forward, 180-day expiry, weekly cap, rounding,
monthly-100-PV eligibility. The 80% changed-line rule is a floor for the rest of
the codebase, not a ceiling for these three files.

---

## Measured, not yet enforced

Recorded so the next change can be compared against reality rather than an
invented target. A ratchet asks for no decision: record where you are, then
refuse to get worse.

| Metric | Today (2026-09-15) | Direction |
|--------|--------------------|-----------|
| Project coverage (`apps/`, `bancostore/`) | **94.9%** (6,483 of 6,830 statements, 246 files) | must not fall (0.5% tolerance) |
| Tests passing | **1,810** (6 skipped) | must not fall |
| pip-audit findings | **1** in 1 package — `weasyprint` 69.0 (PYSEC-2026-3940). Was 7 across 2 before Task 55 cleared all 3 `django-allauth` advisories. | must not grow |
| Full suite runtime, with coverage | ~38 min (this Mac, SQLite) | informational |

Tolerance is 0.5% on coverage, to absorb drift when an unrelated file moves the
number. Re-measure with `make ratchet`.

The pip-audit figure must come from what `pip-audit -r requirements.txt`
resolves, **not** from auditing a local `venv`: `requirements.txt` pins direct
dependencies only, so an older venv keeps stale transitive versions a fresh
resolve would not pick. On 2026-09-15 the local venv reported 17 findings in 5
packages (old `sqlparse`, `tornado`, `cryptography`, plus `pip` itself) against
the resolved set's 1. On a machine that can't take a full `-r` reinstall, the
equivalent is `pip install --dry-run --ignore-installed --report` to resolve,
then `pip-audit -r <resolved pins> --no-deps --disable-pip`.

---

## External vs. self-judged

A bar made entirely of checks the project grades itself against is worth less
than one with an outside opinion in it. Ranked by the only question that
matters — can a change make this pass by writing code that doesn't work?

- **External** (can't be argued with): `pip-audit` reads a real vulnerability
  database. Real-MySQL CI is an external environment, not a mock.
- **Project** (a human owns the config): `ruff`, `black`, `isort`, the floor
  guard's patterns.
- **Suite** (genuinely circular): this project's own ~1,770 tests, and the
  coverage number computed from them.

**Known weakness:** only one genuinely external constraint is wired today.
Semgrep (code scanning), gitleaks (full secret scanning) and Lighthouse
(performance, needs the production URL) would each add one. See E4.

---

## Exceptions

| ID | Rule | Path | Reason | Owner | Expires |
|----|------|------|--------|-------|---------|
| E1 | Floor: no skipped tests | 6 `skipif(not WEASYPRINT_AVAILABLE)` tests across `tests/unit/test_exports.py`, `tests/feature/reporting/test_sales_revenue_report.py`, `tests/feature/admin_portal/test_order_management.py`, `tests/feature/admin_portal/test_withdrawal_review.py` | WeasyPrint `dlopen()`s Pango at import; macOS 12 is an unsupported Homebrew Tier-3 config, so these run in CI (apt Pango) and skip only locally. All 6 share that single root cause — no unexplained skips exist. Already committed, so the diff-scoped guard never sees them; recorded here for honesty. | @owususampson10 | 2026-12-14 |
| E2 | Lint: `E501` line length | `apps/pages/social_icons.py` | Curated, fetched-not-authored SVG path data (Task 35). Each `<path d="...">` is one unbreakable literal from real Simple Icons source, not code to reformat. | @owususampson10 | 2026-12-14 |
| E3 | Security: deps | `requirements.txt` | 7 pre-existing pip-audit findings — `django-allauth` 0.63.6 (3 advisories, fixed in 65.13.0/65.14.1) and `weasyprint` 69.0 (fixed in 70.0). Blocking on these today would gate every future PR on an upgrade that has not been triaged. Baseline recorded above; new findings are the gate. | @owususampson10 | 2026-10-15 |
| E4 | External constraints | repo-wide | Only one external check is wired. gitleaks and osv-scanner install machine-wide via Homebrew, unreliable on this macOS 12 dev box; Lighthouse needs a running URL. Decision pending on whether to wire them CI-only. | @owususampson10 | 2026-10-15 |

An exception with an owner and a date unblocks you. Deleting the constraint
unblocks everyone, forever.

---

## Where each check runs

Running everything everywhere is the single biggest way to make this
intolerable. A check that stalls the loop gets switched off, and a gate someone
switched off is worse than no gate, because the bar still looks like it exists.

| Phase | Command | What runs | Budget |
|-------|---------|-----------|--------|
| Edit | `make check-fast` | black, isort, ruff, floor guard | seconds |
| Task end | `make check-task` | the above + suite + changed-line coverage | ~90s target |
| CI | `make check-full` | everything + migrations, system checks, pip-audit | minutes |

`make check-task` currently runs the full suite, which is well over the 90s
budget on this machine (~35 minutes with coverage instrumentation). Scope it to
the touched app during a task — `pytest tests/feature/orders` — and let CI carry
the full run. That is the intended split, not a shortfall.

---

## Common rationalizations

| Excuse | Reality |
|--------|---------|
| "The tests are the constraints" | Tests you wrote prove you agree with yourself. They say nothing about coverage of new code or dependency risk. |
| "We'll add constraints once the code settles" | Code settles around whatever was allowed while it was moving. |
| "This will slow things down" | Only if slow checks sit in the fast loop. That's a placement error, not an argument. |
| "It's a known flake" | It was known for five tasks in this repo and it was a real bug. Prove it's environmental by isolating it, or fix it. |
| "I'll remember our standards" | The agent writing most of the code won't. |
