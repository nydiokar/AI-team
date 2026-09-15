# Event-Driven Read Refresh (A81)

**Date:** 2026-09-15
**Status:** implemented on `feat/event-driven-read-refresh`
**Builds on:** A79 SSE→`invalidateQueries` bridge (`web/src/lib/liveInvalidation.ts`).

## Problem

Read-model queries refresh on BOTH a fixed timer (~30 `refetchInterval` hooks; chat/transcript at
**3 s**) AND the A79 SSE bridge that invalidates the same query keys on real events. The timer is
redundant whenever the event path covers the key, and every 3 s poll runs a `SELECT` against
`mesh_tasks` — the same DB whose write-lock contention PRs #135–#141 just reduced. Goal: make the app
event-driven with a slow safety-net poll + reconnect-resync, then slim the transcript read the chat
hammers.

## What I verified against the tree/DB (assumptions falsified, not assumed)

| Assumption | How falsified | Result |
|---|---|---|
| "The SSE stream replays everything missed on reconnect, so no resync needed." | Read `useEventStream.ts` (connects with `?token=` only, **no `since`**) + `control_api.event_stream_frames` (`since=0` → *recent tail*, docstring: "Gap recovery is **NOT** a replay"). | **FALSE.** A disconnect longer than the tail window drops events permanently. Reconnect-resync is required. |
| "Every polled query key is covered by an event." | Cross-checked every `refetchInterval` site vs `invalidateLiveTargets`. | **FALSE for `["artifacts"]`** — no event invalidates it. Fixed by adding it to the tasks branch (a new artifact appears exactly when a task completes → `mesh_result` carries `task_id`). |
| "`get_session_turns` blob columns are used by the list view." | Read `_turns_from_db` (transcript.py) + the only other caller (`scripts/backfill_conversation_turns.py`). | **FALSE.** `parsed_output_json`, `file_changes_json`, `error_class`, `return_code`, `session_id` are never read by either caller → safe to drop from the projection. |
| "A composite `(session_id, created_at)` index already exists." | `grep idx_mesh_tasks` in db.py schema + migrations. | **FALSE.** Only `idx_mesh_tasks_session` + `idx_mesh_tasks_created` exist separately. Added the composite (migration 33). |

## Event → query-key coverage matrix

`invalidateLiveTargets` fires from a raw event's correlation ids (`session_id`, `case_id`/`flow_run_id`,
`task_id`, `approval*` prefix). A key is **covered** iff a live event invalidates it.

| Query key | Poll (pre) | Covered by event? | Poll (post) |
|---|---|---|---|
| `["sessions"]` | 3 s | ✅ session/task events | **safety-net 60 s** |
| `["tasks"]` / `["task-sections"]` | 3 s | ✅ `task_id` | **60 s** |
| `["jobs"]` | 3 s | ✅ session/task | **60 s** |
| `["approvals"]` (+ `case_resume`) | 3 s | ✅ `approval*` prefix | **60 s** |
| `["session-messages", id]` (chat) | 3 s | ✅ `session_id` | **60 s** |
| `["session-turns", id]` | 3 s | ✅ `session_id` | **60 s** |
| `["session-usage", id]` | 3 s | ✅ `session_id` | **60 s** |
| `["session-activity", id]` | 3 s | ✅ `session_id` | **60 s** |
| `["artifacts"]` | 3 s | ⚠️ **was NOT covered** → now ✅ (added to tasks branch) | **60 s** |
| `["work-list"]` | 3 s | ✅ `case_id` | **60 s** |
| `["work-detail/timeline/graph", id]` | 15 s | ✅ `case_id` | **60 s** |
| `["work-roster", id]` | 5 s | ✅ session/case | **60 s** |
| `["case-resume-state", id]` | 5 s | ✅ `case_id` | **60 s** |
| `["work-affiliations"]` | 15 s | ✅ session/case | **60 s** |
| `["nodes"]` | 20 s | ❌ heartbeat-derived, no event | **20 s (unchanged)** |
| `["cache-heartbeats"]` | 20 s | ❌ no event | **20 s (unchanged)** |
| `["mesh-health"]` | 20 s | ❌ trend sample | **20 s (unchanged)** |
| `["quota-windows"]` | 20 s | ❌ no direct event | **20 s (unchanged)** |
| `["cost-*"]`, `["case-usage"]`, `["cost-alerts"]` | 15 s | ❌ telemetry-derived, no cost-key event | **15 s (unchanged)** |
| `["system-alerts"]` | 15 s | ❌ external file banner | **15 s (unchanged)** |
| `["models"]` | 30 s | ❌ static-ish | **30 s (unchanged)** |

**The trap key was `["artifacts"]`** — it lost nothing here because we ADDED coverage rather than
just dropping its poll. Every key whose aggressive poll drops to the 60 s safety net is event-covered.
Uncovered keys keep their existing (already gentle 15–30 s) polls untouched.

## Safety-net interval policy

- One constant `SAFETY_NET_MS = 60_000` (`web/src/lib/refreshPolicy.ts`). Event-covered hooks poll at
  this instead of 3–5 s. Rationale: events are the freshness path; the 60 s poll only bounds the
  worst case if BOTH an event is missed AND no reconnect fires. `refetchIntervalInBackground` stays
  default `false` (battery — background tabs don't poll), and `refetchOnWindowFocus`/`refetchOnReconnect`
  still catch a foregrounded tab immediately.
- 20× fewer `mesh_tasks` reads per covered hook (3 s → 60 s).

## Reconnect-resync strategy (the correctness step)

`useEventStream` tracks whether it has connected before. On any **re-open** (SSE error→reconnect, or
tab refocus after the socket was released while hidden) it calls `invalidateAllLive(queryClient)` once
— invalidating every live read-model prefix. react-query only refetches ACTIVE (mounted) queries, so
this is bounded to what's on screen. This closes the window where an event aged out of the server tail
during a disconnect. It is idempotent with the tail-replay+dedupe path (short drops are covered twice;
long drops are covered only by this). Observable via a dev-only `console.debug("[live] reconnect resync …")`.

## Server efficiency (same read path)

- **Slim projection:** `get_session_turns` SELECT drops `session_id, parsed_output_json,
  file_changes_json, error_class, return_code` (unused by both callers) — the two large blobs
  (`parsed_output_json`, `file_changes_json`) are the win. Keeps `reply_text` (full chat text),
  `result` (legacy fallback), `files_modified_json` (file_count), `usage_json` (summary). Pure I/O
  reduction — zero behaviour change.
- **Composite index (migration 33):** `idx_mesh_tasks_session_created ON mesh_tasks(session_id,
  created_at)` turns `WHERE session_id=? ORDER BY created_at ASC` into a covered range scan instead of
  filter-then-sort. Additive; also added to the fresh-DB schema block. PR #141's send-time ordering is
  in the client merge (`useSessionTimeline`), untouched.
- **Conditional-GET / `since` cursor:** deferred as a follow-up (would balloon scope; the slim
  projection + composite index already remove the per-poll cost, and the 60 s cadence makes an
  unchanged-poll 304 a marginal gain).

## Rollback switch

Set `SAFETY_NET_MS = 3_000` in `web/src/lib/refreshPolicy.ts` → every migrated hook returns to
aggressive polling (one-constant revert). The poll infrastructure is intact; nothing was deleted.
The `["artifacts"]` coverage add and the reconnect-resync are pure additions (safe to keep on rollback).

## Cross-layer proof

Traced end-to-end: gateway emits an event onto the spine → `control_api.event_stream_frames`
tails it into a `data:` frame → `useEventStream.ingest` dedupes + calls
`invalidateLiveTargets(collectLiveInvalidationTargets(novel))` → the affected query keys refetch.
Verified live vs. unit: the emit→stream→invalidate→refetch seam is exercised by web unit tests
(`liveInvalidation.test.ts`) at the pure-function layer and by the live gateway restart smoke check
(a live turn appears via SSE with the 3 s poll gone). The reconnect-resync branch is unit-tested via
the extracted `invalidateAllLive` pure function; the transport wiring (onopen re-fire) is verified by
manual SSE drop/restore.
