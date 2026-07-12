# Feedback: vertical-slice build process

Referenced from `tasks/todo.md` at the points where each mistake happened. Two
entries so far, same underlying failure: a planned sequence of small,
independently-verified slices collapsed into one large pass, so a bug in an
early slice shipped silently inside the ones built on top of it.

## 2026-07-10 — Task 4: don't ship a backend slice with a placeholder UI

The customer registration/login backend was built and tested first, with
django-allauth's bare default templates standing in for the real UI "for
now." The user caught this as a broken vertical slice — a slice isn't done
until the real, designed UI ships with it, not deferred to a later pass.
Fixed by fetching the real Stitch screens and building them in the same
pass before calling Task 4 done.

**Rule:** a vertical slice includes its UI. "Backend now, real UI later"
is two slices pretending to be one, and the seam is exactly where bugs
hide.

## 2026-07-12 — Task 8: don't collapse a/b/c into one unreviewed pass

Task 8 was planned as three slices — 8a (site shell + home), 8b (listing
with HTMX search/filter/sort), 8c (detail page) — specifically so each
could be verified before the next was built on top of it (see `tasks/plan.md`
Phase 2). In practice all three were implemented, tested, and "verified" in
one continuous pass, then the whole thing was reviewed only after the user
did their own manual pass and found four real bugs: 8a's view never passed
`categories` to its own template (silent — Django doesn't error on an
undefined context variable), and that same class of gap wasn't caught before
8b/8c were built using the same patterns.

Tests were written *alongside* the code in the same motion, not as a
separate checkpoint — so the tests proved the code did what the code
author (also the test author, in the same breath) already believed it did,
not what the acceptance criteria in `tasks/todo.md` actually required. Two
further acceptance-criteria-vs-test gaps (home's "primary image, GHS
price," detail's "primary image first") were found the same way once
checked line-by-line against the checklist.

**Rule:** when a task is planned as N slices, ship and verify slice 1
before starting slice 2 — "verify" means: full test suite green, then a
real browser check, *then* re-read the acceptance criteria line by line
and confirm each one has a test that would fail without the fix, not just
that the page looks right. Don't write the implementation and its test in
the same breath; write the test against the acceptance criterion's literal
wording first, then make it pass. A slice that "feels done" because it
renders correctly in one screenshot is not the same as a slice whose
checklist items each have a falsifiable test.
