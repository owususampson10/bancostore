# ADR-0001: Use Didit's hosted flow for distributor KYC verification

## Status

Accepted (2026-07-13)

## Date

2026-07-13

## Context

Task 11 originally shipped a self-hosted KYC submission form (commit `ebf8e22`): a distributor
uploads Ghana Card front/back and a selfie directly through our own Django form, and an admin
manually eyeballs the images with no automated check at all.

The user then asked whether a real, automated ID-verification check was possible. This raised a
genuine question: should Bancostore build/host its own document-verification logic, or integrate a
third-party identity-verification vendor? Given `SPEC.md`'s explicit boundary ("Never: Auto-approve
... KYC ... even temporarily for admin"), any vendor integration still needed to leave a human
admin as the final decision-maker — the vendor's role is strictly to assist review, not replace it.

## Decision

Integrate **Didit** (didit.me) as the KYC verification vendor, using its **hosted verification
flow** specifically (not its standalone/server-to-server API).

The distributor is redirected to Didit's own page, which captures the ID front/back and a live
selfie and runs face-match + liveness + document checks entirely on Didit's side. Didit redirects
the distributor back via a `callback` URL and separately notifies us via a webhook (both paths call
one idempotent `consume_didit_result(session_id)`, which always re-fetches the authoritative result
server-side rather than trusting the callback/webhook payload — the same pattern already used for
Paystack). The result (status, per-check scores, extracted document data, images) is stored and
shown to the admin in Django Admin, but **never sets `kyc_status` itself** — only the admin's
explicit approve/reject action does.

This replaced the self-hosted upload form entirely (see commit `b2efe57`): the old
`ghana_card_front`/`ghana_card_back`/`selfie` fields, `KycSubmissionForm`, and `submit_kyc` view
were deleted, not kept as a fallback.

## Alternatives Considered

### Keep the self-hosted upload form, no automated check

- Pros: Already built and shipped; zero new external dependency or cost.
- Cons: No automated fraud/authenticity signal at all — every submission relies entirely on an
  admin's unaided visual judgment of a photo.
- Rejected: once the user asked whether automated verification was possible, doing nothing became
  the weaker option given a free tier existed that covered this project's likely early volume.

### Smile Identity / Youverify (other Africa-focused KYC vendors)

- Pros: Both explicitly support Ghana Card; well-established in the West African market.
- Cons: Neither publishes a free production tier — only a free sandbox for testing; real
  verifications are paid/quoted pricing from the first request.
- Rejected: Didit's free tier (500 checks/month) directly fit this project's current stage, with no
  cost commitment needed to start.

### Didit's standalone/server-to-server API (instead of the hosted flow)

- Pros: Would have let the already-built self-hosted upload form (Task 11a v1) stay in place —
  we'd just forward the already-collected images to Didit's API rather than replacing the form.
- Cons: Two blocking limitations, both confirmed against Didit's own API docs during research:
  (1) **no face-match/selfie support at all** — the standalone endpoint only checks the ID document,
  not the selfie; (2) **"No free tier for standalone APIs"**, per Didit's own standalone-API
  reference page — directly contradicting the general "500 free/month" marketing language, which
  applies to the hosted flow.
- Rejected: the free tier and face-match capability the user wanted were both tied specifically to
  the hosted flow, not the standalone API.

## Consequences

- **A second full onboarding-flow redirect**, alongside Paystack's. Distributors now leave the
  Bancostore site twice during onboarding (once for payment, once for KYC) before returning.
- **A new Boundary-tier external dependency**, subject to the same rigor as Paystack/mNotify:
  environment-variable credentials (`DIDIT_API_KEY`, `DIDIT_WEBHOOK_SECRET`, `DIDIT_WORKFLOW_ID`),
  never `django-constance`.
- **Update 2026-07-14 — both assumptions flagged above are now confirmed**, via Didit's real
  primary docs (webhook signature: `X-Signature-V2`) and a real, live verification session run
  through the user's own Didit account (`front_image`/`back_image`/`portrait_image` field names
  were correct). That same live test caught a real, separate bug the test suite couldn't have
  caught: the SSRF allowlist in `apps/distributors/services.py::_assert_safe_media_url` only
  permitted `*.didit.me` hosts, but Didit actually serves images from a specific S3 bucket — every
  image silently failed to download until fixed. A green test suite proves internal consistency,
  not that assumptions match what a real vendor sends; this is the concrete example of why that
  distinction mattered here.
- **Free tier is a soft constraint, not a hard one.** At 500+ verifications/month, real per-check
  cost begins ($0.33/check for the full KYC bundle per Didit's pricing page at decision time) — not
  a blocker at this project's current stage, but worth revisiting if distributor volume grows.
- The self-hosted upload form's code was deleted outright, not kept behind a feature flag. This was
  judged safe specifically because the project is pre-launch (see the `deprecation-and-migration`
  retrospective on this commit) — there were zero real users of the old form between it shipping
  and being replaced. This reasoning would **not** transfer to a live product with real users.
