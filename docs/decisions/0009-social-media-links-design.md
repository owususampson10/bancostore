# ADR-0009: Admin-managed social media links design

## Status

Accepted (2026-08-11)

## Date

2026-08-11

## Context

Task 35 (not in the original plan — requested directly by the user) replaces the storefront
footer's 3 hardcoded, disabled "Coming soon" social icons (`templates/base_store.html`) with an
admin-managed feature: staff can add and delete an unlimited number of social media links, each
with a name, URL, icon, and color. Pasting a URL should auto-detect the platform's icon, with a
manual override always available; the icon's color should be settable via a color picker with a
hex text field.

Several real design gaps needed resolving before implementation, put through a
`doubt-driven-development` adversarial review (a fresh-context `security-auditor` agent) before any
code was written.

## Decision

### 1. Self-hosted curated icon set, not a new dependency

The user was asked directly (not assumed): add a real `simple-icons` pip/npm dependency (this
project's Boundaries require asking first before any new dependency), or embed a curated subset as
plain data with no dependency at all. The user chose the curated approach. `apps/pages/social_icons.py`
embeds major platforms' `<path d="...">` SVG data and official brand hex colors, fetched directly
from the real, currently-published Simple Icons package (CC0-licensed, built exactly for this
purpose) via `curl`, not hand-approximated — verified by unit tests
(`tests/unit/pages/test_social_icons.py::TestSocialIconRegistryIntegrity`) asserting every entry has
real path data, a valid hex default color, and at least one matching domain. A generation-script bug
(single-domain tuples missing their trailing comma, e.g. `domains=("instagram.com")` instead of
`domains=("instagram.com",)` — silently iterating over *characters* of the domain string instead of
the domain itself) was caught by these same tests before merge, not after; see the "Consequences"
section below for the full list of platforms it affected.

A platform not in the curated set (or freely typed by the admin as "Other / Custom") falls back to
the Material Symbols "public" glyph this project already loads everywhere else, never a fabricated
brand mark. LinkedIn is deliberately one of these fallback cases, not a curated icon — see Decision
9 below.

### 2. Model owned by `apps/pages`, admin CRUD owned by `apps/admin_portal`

Mirrors the existing `apps/catalog` (owns `Product`/`Category` models) /
`apps/admin_portal` (owns the Stitch-designed admin UI for managing them) split exactly, established
by Task 26. `apps/pages` already owns storefront/footer-adjacent content (About, Contact); `SocialMediaLink`
extends that ownership naturally. `apps/admin_portal` already owns every other admin-facing screen
(KYC, withdrawals, catalog, orders, platform settings) per Task 22/23's "every admin-facing screen
should look like the rest of the app" precedent — a new, separate admin app for one model would be
inconsistent with that.

### 3. `platform` is a constrained choices slug — the real SVG markup never comes from admin input

`SocialMediaLink.platform` is a `CharField(choices=SOCIAL_ICON_CHOICES)`, restricted at the Django
field level to the curated registry's known slugs. The actual `<path d="...">` string an admin's
browser ever renders is always looked up server-side from the fixed `SOCIAL_ICONS` dict
(`SocialMediaLink.icon` property), never stored in the database and never accepted as free text from
any request. This closes off an entire class of stored-XSS risk by construction, not by escaping —
there is no code path where request-controlled SVG markup could reach the page, regardless of how
carefully (or carelessly) any future caller handles the `platform` value.

### 4. Icon auto-detection is a UX nicety, not a security boundary

`detect_platform_from_url()` (`apps/pages/social_icons.py`) parses a pasted URL's hostname and
matches it against each platform's real domains, exact-or-subdomain only (never a bare substring
check — `facebook.com.evil.example` must not match `facebook.com`). It is exposed as a small,
admin-gated JSON endpoint (`admin_portal:social_link_detect_platform`, `GET`, called via `fetch()`
from the add/edit form's URL field on `blur`) purely to pre-select a value in the picker — the admin
can always override it manually, and the final save is independently re-validated by
`SocialMediaLinkForm.is_valid()` regardless of what this endpoint returned. Gated behind the same
`is_admin_portal_staff` check as every sibling admin_portal view (a `doubt-driven-development`
finding: leaving it open had no legitimate product reason and would have needlessly widened the
blast radius of any future bug in that one view) — but its authorization exists for defense in
depth and consistency, not because an unauthenticated caller could otherwise do anything harmful
with it (it performs no DB write and never echoes the candidate URL back into its response).

### 5. Cached context processor, invalidated by model signals

`apps/pages/context_processors.py::social_media_links` runs on every request across the entire
site (the footer is shared by every storefront page, `base_store.html`), including fully
unauthenticated ones. A `doubt-driven-development` finding flagged an uncached query here as
inconsistent with this project's own scale-conscious precedent (constance settings are Redis-cached
for the same reason). The queryset is cached indefinitely (`timeout=None`) and invalidated by
`post_save`/`post_delete` signals on `SocialMediaLink` (`apps/pages/models.py`) — the same
"admin rarely changes this, cache it and invalidate on write" shape constance's own backend already
uses, not a new caching pattern invented for this feature.

### 6. `MAX_SOCIAL_MEDIA_LINKS = 20` cap, enforced in the form

The user asked for "countless" links with no numeric ceiling in mind — but a
`doubt-driven-development` finding pointed out that an uncapped table (even absent any malice, just
an admin fat-fingering an import or repeatedly clicking Add) degrades the public footer on every
single page load with no floor. `MAX_SOCIAL_MEDIA_LINKS` is a generous, effectively-unlimited-in-
practice ceiling enforced in `SocialMediaLinkForm.clean()`, checked only on *create* (`self.instance.pk`
falsy) — editing an already-existing row is never blocked just because the table happens to be full.

### 7. Full-page POST + redirect CRUD, not htmx partial swaps

An `Explore` investigation found no existing htmx add/delete-row pattern anywhere in
`apps/admin_portal` or `apps/catalog` — every existing `hx-*` usage in this app is `hx-get` for
filter/pagination against a list partial, never per-row create/update/delete. Rather than
introducing a new interaction pattern for one feature, `social_link_create`/`_update`/`_delete`
follow the exact shape `catalog_category_create`/`_edit`/`_delete` already established: a plain POST
that either redirects on success (`messages.success`, matching every other admin_portal CRUD view)
or re-renders the same page with that one form's errors on failure. Delete reuses the shared
`templates/admin_portal/partials/_delete_confirm_modal.html` component verbatim (the same
page-level `x-data` scope — `deleteOpen`/`deleteName`/`deleteUrl`/`deleteKind`/`deleteTrigger` — that
`catalog_category_list.html`/`catalog_product_list.html` already use), not a new confirm dialog.

Each row still edits inline (name click expands/collapses via a plain Alpine `x-show`, no server
round-trip) rather than navigating to a separate edit page, since "click the name to expand the
field" was an explicit user requirement — but the *save* action itself is a real form POST like any
other admin_portal form, not an SPA-style live-patch.

### 8. Icon/color picker rendered entirely server-side, never built as an HTML string in JS

Both the row's "currently selected icon" preview and its picker dropdown's option list are rendered
directly by Django (`templates/pages/_social_icon.html`, looped once per curated icon in the admin
template), toggled visible/hidden purely via Alpine `x-show` bound to a plain string variable
(`platform`). No `x-html`/`innerHTML`-style dynamic markup construction exists anywhere in this
feature — an earlier draft considered passing the icon registry to the client as JSON (via
`json_script`, this project's own established safe-Alpine-data-passing convention from Task 17) and
building the preview/dropdown markup in JavaScript, but was simplified away entirely once it became
clear the same real SVG markup could just be pre-rendered server-side both times and shown/hidden
with CSS, removing an entire class of "is this JS string-building safe" review question rather than
mitigating it.

### 9. LinkedIn is excluded from the curated set entirely

Found by a `code-review-and-quality` pass run after the initial implementation (not caught by the
earlier `doubt-driven-development` security review, which was scoped to injection/auth/caching
risks, not trademark provenance): Simple Icons permanently removed LinkedIn in v14.0.0 following
LinkedIn's own trademark enforcement (confirmed against the real `simple-icons/simple-icons` GitHub
issue tracker, not assumed). The path data this project had originally embedded was accurate — a
byte-for-byte match of what the last pre-removal release shipped, not corrupted or fabricated — but
distributing a permanent copy of a mark the rights holder had removed elsewhere is a real, if likely
small, exposure. Put to the user directly via `AskUserQuestion` rather than decided unilaterally
(two options offered: drop it, or keep it with a corrected sourcing comment); the user chose to drop
it. A LinkedIn link now resolves to the same `"custom"` fallback as any other uncurated platform.

## Alternatives Considered

### Add the real `simple-icons` npm/pip package

Broader icon coverage (thousands of brands vs. ~18), less manual curation. Rejected per explicit
user choice: adds a new dependency this project's Boundaries gate behind asking first, and more
build-time tooling to extract icon SVGs, for coverage this platform's real use case (a handful of
major social platforms) doesn't need.

### Live external CDN for icon SVGs (e.g. `cdn.jsdelivr.net/npm/simple-icons`)

No embedded data at all, always up to date. Rejected: an external runtime dependency for something
rendered on every single storefront page load, and this project already has a documented, real
incident (Task 29's About-page hero image) where an external CDN asset silently broke — self-hosting
avoids repeating that exact failure mode for a much higher-traffic surface (the footer, not one
page).

### htmx partial swaps for add/edit/delete (matching the initial draft design)

Faster perceived UX (no full page reload), consistent with this app's general htmx-heavy storefront
conventions. Rejected for the *admin_portal* CRUD flow specifically: no sibling admin_portal feature
uses this pattern for row-level mutation, and introducing it here would be a one-off inconsistency
rather than reuse. The detect-platform lookup still uses a lightweight `fetch()` call (not htmx) for
the one place a genuine async round-trip is needed.

### Client-side JS mirror of `detect_platform_from_url`'s domain-matching logic

Zero network latency, one fewer endpoint. Considered and rejected in favor of the small `fetch()`-
based endpoint: keeps the matching algorithm in exactly one place (Python, already unit-tested) with
no risk of the JS and Python implementations silently drifting apart as new platforms are curated.

## Consequences

- `apps/pages/social_icons.py` is a real, if small, second source of "the truth" about which
  platforms this project supports, separate from the model's own `choices` — kept in sync by
  construction (`SOCIAL_ICON_CHOICES` is derived directly from `SOCIAL_ICONS.keys()`, not maintained
  as a parallel list).
- Adding a 19th+ platform later means editing exactly one file (`apps/pages/social_icons.py`) and
  re-running the same `curl`-based fetch pattern used to build the original set — no migration
  needed unless the model's own fields change.
- The generation-script bug this ADR documents (missing trailing commas on 9 single-domain
  tuples — `instagram`, `linkedin`, `tiktok`, `snapchat`, `threads`, `github`, `twitch`, `spotify`,
  `medium`) was caught and fixed before merge by
  `tests/unit/pages/test_social_icons.py::TestDetectPlatformFromUrl::test_matches_known_platform_domains`,
  which failed exactly as designed the first time it ran. A live-browser check after the fix
  confirmed auto-detect correctly resolves a pasted `instagram.com` URL to the Instagram icon.
- The footer's social icons are cached indefinitely and only change when an admin edits the list —
  a Redis cache flush/restart (already a documented, accepted risk elsewhere in this codebase, e.g.
  `SESSION_ENGINE`'s Task 30e fix) would transparently rebuild the cache on the next request, not
  lose data.
