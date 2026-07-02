# Adversarial Review — AGENT 8 Operator Signal dispatch

**Reviews:** `AGENT_8_OPERATOR_SIGNAL.md`
**Date:** 2026-07-03
**Verdict:** Dispatch is sound and well-grounded. Ship it — but fix the four
**BLOCKER-adjacent** ambiguities below first, or the implementing agent will make
a plausible-but-wrong call. Findings are ordered by severity.

---

## Findings

### F1 (BLOCKER) — Push must fire on the always-run path, not the Telegram path

The dispatch says "a push send is one more channel call in `notify_task_outcome`."
True, but there's a trap: in `notification_service.py` the Telegram send is gated
by `if chat_id and tg:`. The event `emit_event("task_notification", …)` runs
**unconditionally** just above it. **Web-only sessions have no `chat_id`** — if the
implementer bolts the push call inside the `if chat_id` block, push will silently
never fire for exactly the sessions that need it most (the ones driven from the
Web UI, not Telegram).

**Required correction:** the push fan-out must sit on the **unconditional** path
(next to `emit_event`), independent of `chat_id`/`tg`. Add a line to the dispatch:
"Push delivery is independent of Telegram delivery; it must not be nested under
the `chat_id and tg` guard." Also confirm all three call sites
(`orchestrator.py:525/1069/1882`) route through this method — they do.

### F2 (BLOCKER) — "async fan-out that never blocks completion" needs an explicit mechanism

The dispatch demands "must never block task completion" AND "cap fanout
concurrency / per-send timeout." But `notify_task_outcome` is `await`ed inline in
the orchestrator's completion path. A naive `await push_all()` there **does** block
completion for up to (timeout × subscribers). The two requirements silently
conflict.

**Required correction:** specify the mechanism. Either (a) fire-and-forget via
`asyncio.create_task(...)` with an internal `asyncio.gather` bounded by a
`Semaphore` and per-send `asyncio.wait_for`, or (b) a bounded await with a hard
aggregate deadline (e.g. ≤2s total) regardless of N. Pick one in the dispatch so
the agent doesn't invent a blocking loop. Note: fire-and-forget tasks must swallow
their own exceptions (the codebase rule is "never raise into the caller").

### F3 (MAJOR) — VAPID/web-push has NO named dependency or send transport

The dispatch says "delivery helper" and "injected fake sender" but never says
*what actually sends a Web Push*. Web Push requires VAPID JWT signing + encrypted
payload (RFC 8291) — this is non-trivial and must not be hand-rolled. The plan is
untestable/unbuildable until the transport is chosen.

**Required correction:** name the approach — either add the `pywebpush` dependency
(to `pyproject.toml`, and note it's an optional extra so absence = push disabled,
consistent with the "VAPID absent → disabled, don't crash" rule), or explicitly
scope T1 to *store subscriptions + wire the seam + SW handlers* and defer the
actual encrypted send behind a feature flag. Don't leave "how bytes reach the
browser" undefined.

### F4 (MAJOR) — Request-size cap has no enforcement point named

"Cap request body size" is stated but FastAPI does **not** enforce body size by
default, and the other endpoints here use Pydantic `BaseModel` bodies
(`InstructionBody`, etc.) with no size guard. An agent will "add a cap" and
produce a no-op.

**Required correction:** specify enforcement — read/inspect `Content-Length` and
reject > N KB before parsing, or cap via the model + a middleware guard. Give a
concrete bound (subscribe payloads are tiny; 4 KB is generous).

### F5 (MODERATE) — T2 endpoint path is unspecified; risk of scope creep into telemetry

T2 says "aggregate registry/config/telemetry facts" but doesn't name the endpoint
or bound the read. Given `telemetry_store.py` exists, an agent may build an
expensive scan or start *deriving* limits from usage deltas (i.e. inventing quota
— the exact thing the dispatch forbids elsewhere).

**Required correction:** name the route (e.g. `GET /api/backends/usage`), state it
must be O(1)-ish / bounded reads, and repeat the honesty rule inline in the T2
section: **usage counters observed ≠ limits; never derive a limit the provider
didn't state.**

### F6 (MODERATE) — Migration-number collision risk if another branch also adds 20

`main` is at `_CURRENT_VERSION = 19`. If the M1/M2 validation branch or any
in-flight branch also appends migration 20, merging produces two different
migration 20s — the codebase already hit this once (`_ensure_merged_schema` exists
precisely because Web-UI and main *both* used migration 13 for different things).

**Required correction:** add a one-liner to the dispatch: before appending
migration 20, grep all branches (`git log --all -p -S "_CURRENT_VERSION"` /
`grep -rn "(20," src/control/db.py`) to confirm 20 is unclaimed; if taken, use the
next free number and reconcile.

### F7 (MINOR) — SW cache version bump not mentioned

`sw.js` uses `CACHE = "ai-team-shell-v2"` and the `activate` handler deletes any
cache != current. Adding `push`/`notificationclick` handlers changes SW behavior;
existing clients won't pick up the new SW until it byte-changes and activates.
Editing the file is enough to trigger an update, but the dispatch should note:
bump the cache name (→ `-v3`) so the new SW reliably activates, and verify
`skipWaiting`/`clients.claim` still apply.

### F8 (MINOR) — "session URL when known" is underspecified

Push and `notificationclick` both reference "session URL." The web route shape
(`/sessions/:id`?) isn't stated, and for Telegram-origin sessions surfaced only in
Web there may be no canonical URL. Cheap to resolve; name the frontend route so
the SW `notificationclick` opens the right path, and fall back to `/` when unknown.

---

## Cross-cutting checks (pass)

- **Test-cost guard:** dispatch repeats it up front and mandates a fake sender —
  no paid-CLI exposure. Good. Ensure T1/T2 backend tests never import a path that
  spawns Claude/Codex; they don't need to.
- **Scope discipline:** correctly refuses approval-gate wiring (#25), invented
  quota (#30/#33), and M3 start (#5–#9). Consistent with operator directives in
  CONTEXT.md.
- **`MESH_ENABLED=false` invariant:** untouched by all three tasks. Good.
- **Ranking:** defensible. T1 has the clearest user-visible payoff and the seam
  already exists (verified: `task_notification` emitted, SW lacks push handlers).
  T2 addresses a genuine paid-account reliability blind spot. T3 is honest
  validation-debt, correctly ranked last.

## Required edits before implementation

Fold F1–F4 into `AGENT_8_OPERATOR_SIGNAL.md` T1 (they're build-blocking), and
F5–F6 into T2/T1 respectively. F7–F8 can be resolved during build. With those,
the handoff is executable without further clarification.
