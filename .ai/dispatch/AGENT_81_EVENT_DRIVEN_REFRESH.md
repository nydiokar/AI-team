```yaml
job_id: AGENT_81_EVENT_DRIVEN_REFRESH
created_at: "2026-09-15T09:35:42.131929+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A81             # -> DISPATCH_LOG.md section with the verdict prose
evidence: docs/EVENT_DRIVEN_READ_REFRESH.md,tests/test_transcript_read_a81.py,web/src/lib/liveInvalidation.test.ts,src/control/db.py                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-09-15T14:07:19.864668+00:00"
```

# DISPATCH — AGENT_81_EVENT_DRIVEN_REFRESH

**Date:** 2026-09-15
**Depends on:** — (builds on A79 `AGENT_79_PWA_LIVE_REFRESH_UX.md`, already merged)

**Goal:** The Web UI's read-model queries poll the gateway on fixed timers (≈30 `refetchInterval`
hooks; the chat/transcript at **3 s**) even though A79 already ships an SSE→`invalidateQueries`
bridge (`web/src/lib/liveInvalidation.ts`) that refreshes the exact same query keys when a real
event arrives. That double coverage is a constant, mostly-idle API/DB read storm (each poll runs a
`SELECT` against `mesh_tasks`, the same DB whose write-lock contention we just spent PRs #135–#141
reducing). **Retire the redundant polling in favour of the event bridge that already exists, with a
slow safety-net poll so a missed/dropped SSE event can never strand stale data — then close the two
server-side inefficiencies the transcript read still carries.** Make the app event-driven and
production-grade, not timer-driven.

> **THIS IS NOT A "rip out setInterval and ship" TASK. Plan first, then falsify your plan.**
> The naive version (delete every `refetchInterval`) will silently regress freshness the moment the
> SSE stream drops a frame or the tab was backgrounded during an event. You are explicitly required
> to design for the failure modes below and prove they're handled, or the job is not done.

## Context you MUST verify before writing code (do not trust this prose — confirm against the tree)
- A79 bridge exists: `web/src/lib/liveInvalidation.ts` (`collectLiveInvalidationTargets` +
  `invalidateLiveTargets`) already invalidates `["session-messages", id]`, `["session-turns", id]`,
  `["sessions"]`, `["tasks"]`, `["jobs"]`, `["work-*", caseId]`, `["approvals"]`. Read it. Read its
  test `web/src/lib/liveInvalidation.test.ts`.
- The SSE transport: `web/src/hooks/useEventStream.ts` + `web/src/hooks/eventStreamContext.tsx`
  (single app-wide `EventSource` on `/api/events/stream`; token rides as `?token=`). **Confirm how it
  behaves on reconnect and whether it can miss events during a disconnect** — that gap is the whole
  reason a safety net is required.
- The pollers: `grep -rn "refetchInterval" web/src` (~30 sites). Intervals live as `POLL_MS`,
  `SLOW_POLL_MS`, `LIVE_POLL_MS`, `DETAIL_POLL_MS` in `useLiveData.ts`, `useWork.ts`, `useCost.ts`,
  `useSystemAlerts.ts`. The chat is `useSessionMessages` (`useLiveData.ts`, `POLL_MS=3000`).
- Server read the chat hits: `GET /api/sessions/{id}/messages` → `src/control/transcript.get_transcript`
  → `MeshDB.get_session_turns` (`src/control/db.py`, `SELECT ... FROM mesh_tasks ... ORDER BY
  created_at ASC`). Note it currently `SELECT`s columns incl. the large `payload`/`result` blobs.
- Just-shipped related fix (do NOT redo, but be consistent with it): PR #141 made transcript ordering
  send-time stable. Any client-side merge you touch (`useSessionTimeline.ts`) must preserve that order.

## Task — plan-first, then implement in reviewable increments
1. **Design doc first (short, in `docs/`).** Before touching code, write a 1-page plan:
   the event→query-key coverage matrix (which SSE events invalidate which keys — cross-checked
   against `liveInvalidation.ts`, flag any query key that NO event currently covers), the chosen
   safety-net interval policy, the reconnect-resync strategy, and the rollback switch. List the
   assumptions you are making and how each is falsified. Get the coverage matrix RIGHT — a query key
   with no event that also loses its poll goes permanently stale.
2. **Replace fixed polling with event-driven + slow safety net.** For queries fully covered by the
   bridge, drop the aggressive interval (3–5 s) to a single long safety poll (pick and justify, e.g.
   ~60 s; align with `@tanstack/react-query` idioms — `refetchOnWindowFocus`, `refetchOnReconnect`,
   `staleTime`, and `refetchIntervalInBackground:false` which the codebase already relies on for
   battery). Do NOT invent a bespoke timer system; use the query client that's already there.
3. **Reconnect resync (the critical correctness step).** On SSE (re)connect after any gap,
   invalidate the affected/visible read-models once so a dropped event window cannot leave stale
   data. Make this observable (a dev log/metric) so freshness is provable, not assumed.
4. **Server efficiency, tied to the same read path:**
   a. Slim the transcript projection: the message-LIST view does not need the full `payload`/`result`
      blobs — select only the fields the list renders (prompt/reply/timestamps/usage-summary). Verify
      what `get_transcript` actually returns to the client before cutting.
   b. Add a composite index `mesh_tasks(session_id, created_at)` (migration, additive) so
      `get_session_turns` is a covered range scan instead of filter-then-sort. Confirm the current
      indexes first (`idx_mesh_tasks_session`, `idx_mesh_tasks_created` exist separately today).
   c. OPTIONAL if cheap and clean: conditional-GET (ETag/`If-None-Match`) or a `since` cursor on the
      messages endpoint so an unchanged safety-net poll returns 304/empty. Only if it doesn't
      balloon scope — otherwise write it up as a follow-up, don't half-build it.
5. **Prove it.** Web tests for the invalidation/reconnect logic (extend `liveInvalidation.test.ts`
   pattern; pure functions, no DOM/EventSource harness where possible). Backend tests for the slim
   projection + the index-backed query. Production web build clean. Manually confirm: a live turn
   still appears within ~1 s via SSE with the 3 s poll GONE, and killing the SSE then restoring it
   resyncs.

## Ground rules for the executing agent (read these as hard constraints)
- **Plan upfront and invalidate your own assumptions.** State what you believe, then verify each
  against the actual code/DB before building. A wrong assumption here (e.g. "every query key is
  covered by an event") silently breaks freshness — that is worse than the current polling.
- **Reach for the current, correct idiom, not a generic 2020-era pattern.** Use the existing
  react-query invalidation + the existing single-EventSource transport. Do NOT introduce a second
  socket, a homegrown pub/sub, a global mutable event bus, or `setInterval` soup. If you think a
  bigger mechanism is warranted, justify it against what's already there and surface it — don't just
  build it.
- **Minimal diff, incremental, reversible.** Keep a rollback: the safety-net interval means reverting
  to aggressive polling is a one-constant change. Do not delete the poll infrastructure wholesale in
  one commit; migrate hook-by-hook so each step is verifiable.
- **Do not regress the just-fixed ordering (PR #141)** or the DB-contention work (PRs #135–#140).
- **Cross-layer honesty:** a green web test does not prove the event actually fires from the gateway.
  Trace one real event end-to-end (gateway emit → `/api/events/stream` → `liveInvalidation` → query
  refetch) and state which seams you verified live vs. only in unit tests.
- **Branch/PR/close policy** per repo `CLAUDE.md`: `feat/<slug>` branch, targeted `pytest` on touched
  modules only (**never** the full/e2e suite — cost guard), web typecheck + production build, PR, and
  self-merge; restart the gateway to deploy the server-side piece (worker untouched).

## Current Behavior
- Read-models refresh on BOTH a fixed timer (≈30 `refetchInterval` hooks; chat at 3 s) AND SSE
  invalidation (A79). The timer is redundant whenever the event path covers the key, and it drives
  constant `mesh_tasks` reads.
- `get_transcript`/`get_session_turns` reads the full row (incl. large blobs) for the list view and
  filters `session_id` then sorts `created_at` without a composite index.

## Root Cause
Freshness was originally poll-only; A79 added the event bridge but the polls were left in place as
belt-and-suspenders, so the app never actually became event-driven. Making it event-driven safely
requires a reconnect-resync + a slow safety net, plus tidying the read path the chat hammers.

## Done when
- Aggressive per-query polling (3–5 s) is replaced by event-driven refresh + a justified slow
  safety-net poll; reconnect-resync in place and observable; a live turn appears within ~1 s with the
  3 s chat poll removed, and an SSE drop/restore visibly resyncs.
- Transcript list projection no longer ships the big blobs; `mesh_tasks(session_id, created_at)`
  composite index added (migration).
- 1-page design/plan doc in `docs/`; web + backend tests green; production web build clean; PR merged;
  gateway restarted for the server piece.
- Set `evidence:` to the design doc path, the new/updated test files, and the migration file; point
  `results_ref:` at the DISPATCH_LOG closure row.
