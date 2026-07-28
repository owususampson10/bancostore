# ADR-0008: Live wallet-balance updates over Django Channels (Task 20d)

## Status

Accepted (2026-07-28)

## Date

2026-07-28

## Context

Task 20d needed the first real-time WebSocket feature in this codebase: a distributor's dashboard
wallet balance should update live, without a page refresh, the moment a commission is credited.
Channels + Redis have been configured since Task 1-3, but `bancostore/asgi.py`'s websocket
`URLRouter` had always been empty — no consumer, no auth wrapping, nothing.

Because this carries real financial data (Ghana Cedi wallet balances from commission payouts),
`doubt-driven-development` ran before any consumer code was written: a single-model fresh-context
review, then an external second opinion (the user ran the same artifact through both ChatGPT and
Gemini independently). Across all three reviews, several real issues surfaced that materially
changed the design from its first draft.

## Decision

**Group scoping, with zero client-supplied input.** The WebSocket route
(`ws/distributors/wallet/`) takes no ID, token, or query parameter at all. `AuthMiddlewareStack`
populates `self.scope["user"]` from the session cookie during the handshake (identical to how
regular HTTP views authenticate); the consumer (`apps/distributors/consumers.py::
WalletBalanceConsumer`) derives the Channels group name (`wallet_{distributor.pk}`, see
`apps/distributors/realtime.py::wallet_group_name`) purely from that server-side session. There is
no client-controlled value to spoof for group membership, by construction — not "validate the
requested group against the owned distributor," but "there is no requested group."

**Reject at `connect()`, using the existing permission check.** `is_distributor(user)`
(`apps.accounts.permissions`) is reused directly rather than reinvented, so the same rule that
gates HTTP dashboard access gates the WebSocket. An unauthenticated or non-distributor connection
is closed immediately, never accepted and then silently starved of messages.

**Origin validation.** `channels.security.websocket.AllowedHostsOriginValidator` wraps the stack in
`asgi.py`, using the existing `ALLOWED_HOSTS` setting — closing a cross-site WebSocket hijacking
(CSWSH) gap that session-cookie auth alone does not close (browsers attach cookies to WebSocket
handshakes regardless of the initiating origin).

**Decoupled notification via signal, not a direct call from the wallet service.**
`apps/wallet/services.py::credit()`/`debit()` are untouched — the wallet app has no import of, or
dependency on, Channels/consumers/WebSockets. `apps/distributors/signals.py` listens for
`WalletTransaction`'s `post_save` instead (registered in `DistributorsConfig.ready()`), since every
`WalletTransaction` row is, by this ledger's own append-only design, a real balance change. Known,
documented limitation: Django signals do not fire on `bulk_create()` — every current caller uses a
single `.create()`, so this holds today, but a future bulk-write path for `WalletTransaction` would
silently produce no live update. Not solved now; flagged for whoever builds that path next.

**Deferred via `transaction.on_commit()`, querying fresh at execution time.** The signal fires
synchronously inside the same `atomic()` block `credit()`/`debit()` already use — sending
immediately would announce a balance change that could still roll back. The callback also does
*not* capture a balance value at registration time; it queries `Wallet.objects.get(...).balance`
fresh when it actually executes, after commit. This closes a subtler race the external reviews
caught: if two credits commit in close succession, whichever callback runs last (not necessarily
in commit order) must still report the *true current* balance, not a value computed earlier.

**Immediate balance push on connect.** Beyond joining the group, `connect()` also sends the
distributor's current balance right away. Without this, a reconnect (page refresh, network blip)
would leave a stale balance on screen until the next unrelated wallet event — a gap the external
review (ChatGPT) specifically caught.

**Failure isolation.** `group_send` failures (a Redis hiccup) are caught and logged, never allowed
to propagate — mirroring Task 12's SMS-notification-failure isolation exactly. The money already
moved correctly by the time this callback runs; a live-update delivery failure is a UI staleness
problem, not a data problem, and must never look like the credit itself failed (a real risk for
Task 13/14's batch drivers, which treat unexpected exceptions as cycle failures).

## Alternatives Considered

- **Calling `channel_layer.group_send` directly from `credit()`/`debit()`.** Rejected: couples the
  financial core to a UI/real-time concern, and every future money-movement code path would need to
  remember to add the same call by hand. The signal-based approach isn't perfectly automatic either
  (see the `bulk_create` limitation above), but it covers every current caller for free and keeps
  the coupling one-directional (distributors → wallet's signal, not wallet → Channels).
- **A URL-parameterized group with server-side validation** ("does this user own the requested
  distributor ID?"), as originally sketched in the task description. Rejected in favor of no
  parameter at all: eliminating the input eliminates the entire class of validation bugs a
  parameterized version would need to keep getting right.

## Consequences

- **Local dev requires accessing the app via `daphne` (port 8001), not `runserver` (port 8000),**
  to exercise this feature at all — this repo's documented local setup runs them as two separate
  processes. A relative `ws://` URL from a page served on 8000 has nothing to connect to. Recorded
  as a new gotcha in `CLAUDE.md` rather than silently discovered again later.
- **Known, accepted gap: an already-open socket outlives session revocation.** If a distributor
  logs out in another tab, or an admin deactivates their account, an already-connected WebSocket
  keeps receiving updates until it happens to disconnect — Channels' auth check runs once, at the
  handshake, not per message. Full session-revocation-aware disconnection was judged out of scope
  for this task; flagged here rather than silently accepted.
- **No message sequencing/versioning.** Each message carries the current balance (not a delta), so
  a late or reordered message is self-correcting in practice — but there is no monotonic
  sequence number a client could use to detect or discard a genuinely stale message under unusual
  delivery reordering. Not added for this task; low practical risk given the query-fresh-at-send
  design above, but worth revisiting if a future feature needs stronger delivery guarantees.
- **Frontend reconnects with exponential backoff** (capped at 30s) rather than giving up after one
  dropped connection, per both external reviews flagging the original draft's lack of any.
