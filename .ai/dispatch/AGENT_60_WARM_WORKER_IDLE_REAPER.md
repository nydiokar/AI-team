```yaml
job_id: AGENT_60_WARM_WORKER_IDLE_REAPER
created_at: "2026-07-30T03:07:45+03:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: "claude"
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_warm_worker_idle_reaper.py"]
updated_at: "2026-08-21T15:10:00.000000+00:00"
```

# DISPATCH — A60 · Warm-worker idle-reaper (§7 resource leak)

**Level:** 3 (worker lifecycle) · **Type:** code
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** — (independent; complements A52/A55 autonomy). **Unblocks:** safe *long* autonomous runs.
**Ultimate goal this serves:** `SPEC_COMPLETION_PLAN.md` §0 bounded-resources clause; the §7 deferral from A48.

> **Outcome, not a script.** A48 (PR #26) deliberately made joined worker sessions **warm** on Case-close —
> closed only by the Manager's explicit `release_worker`. That was the right call for re-dialogue, but it left
> a **written §7 deferral**: warm workers hold a backend slot with **no idle-reaper**, so accumulation is
> bounded only on Manager discipline. Once autonomous runs get long (A52+), that is a real resource leak.

## Why (intent)
CONTEXT.md §7 note (2026-07-18 STATUS block) and the A48 closure both record: "warm workers hold a backend
slot with no idle-reaper — unbounded accumulation gated only on Manager discipline. Acceptable now; size a
bound/idle-reaper if live load shows it." Autonomous continuation (M3.4, now merged) is exactly the "live
load gets long" condition that promotes this from acceptable-deferral to must-fix.

## TASK
Add a bounded idle-reaper for **warm worker sessions**: a warm worker idle (no turn, not joined to any OPEN
Case) beyond a configurable TTL is closed and its affiliation cleared — reusing the existing session-close +
affiliation-clear plumbing (the same path `close_case`/`release_worker` already use), not a new lifecycle.
A worker still joined to an open Case, or mid-turn, is never reaped.

## TYPE
code. Branch `feat/warm-worker-idle-reaper`; PR at close, merge yourself.

## SEAM MAP (verified read-only 2026-07-30 — execute-ready)
- **Loop to hook:** `orchestrator._stale_busy_reconciliation_loop` (`src/orchestrator.py:916`, interval
  `config.mesh.session_reconcile_interval_sec` default 60s; started via `_start_stale_busy_reconciler` ~:905).
  Mirror this shape — do NOT add a second scheduler. (The M3.4 wake-dispatcher `_wake_dispatcher_loop` ~:689
  is the other template.)
- **Identify an idle warm worker (all must hold):** `sessions.status IN ('idle','awaiting_input')` (NOT `busy`)
  · `sessions.case_role='worker'` · idle beyond TTL by `sessions.updated_at` (stamped every turn via
  `session_store.save(touch=True)`, `src/services/session_store.py:106` — reliable) · AND its Case is closed/none
  (join `flow_runs.status IN _CLOSED_STATUSES`, or `current_case_id IS NULL`). A worker still joined to an OPEN
  Case is NEVER reaped.
- **Primitives:** `db.set_session_case(session_id, None, None)` (`src/control/db.py:858`, atomic, idempotent,
  clobber-safe) clears affiliation; `session_service.close_session(session_id, backends=…)` (used by
  `orchestrator._close_worker_session_on_case_close`, `src/orchestrator.py:2527`) closes the session.
- **⚠️ close vs clear-only:** the §7 leak is a held **backend slot** — clearing affiliation alone does NOT free
  it. The reaper MUST `close_session` (then clear affiliation), i.e. it reverses A48's warm-keep *after a long
  idle TTL*. That is the intended semantics (warm for re-dialogue up to TTL, then reclaimed).
- **Config pattern to mirror:** `getattr(config.mesh, "warm_worker_idle_ttl_sec", <default>)` (cf.
  `MESH_AFFINITY_OFFLINE_GRACE_SEC`, `session_reconcile_interval_sec`). **`0` ⇒ disabled ⇒ byte-identical.**

## SEQUENCING / URGENCY (owner note 2026-07-30)
Not urgent **yet**: the leak only bites once *long autonomous* runs exist, and M3.4 continuation
(`CASE_CONTINUATION_ENABLED`) is still **OFF**. Pull this the moment A52 activates continuation (or A55
crash-respawn lands). **Single biggest risk:** a reap-close racing a Manager re-dispatch to that warm worker —
mitigate with (a) a **generous default TTL** (≥1h) so active workflows have wide margin, (b) reap only
`status='idle'` (never mid-turn), (c) the open-Case guard above. TTL should ideally be sized against one real
autonomous run's idle profile rather than guessed — hence deferring the build until continuation is live.

## CONTEXT (reuse verbatim)
- Warm-keep policy: A48 / PR #26 (`feat/manager-decided-worker-close`) — removed auto-close-on-Case-close.
- `release_worker` guard + session→case index: PR #27 (`/api/work/affiliations/sessions`), `case_id`-scoped.
- Existing reaper template: the mesh **stale-claim reaper** / incarnation sweep (T4, `feedback_worker_restart_drops_claim`)
  and the M3.4 stale-busy reconciler loop — mirror the periodic-sweep shape, do not fork a new scheduler.
- Session close + affiliation clear: the same seam `close_case` uses to close `case_role='worker'` sessions (PR #22).

## ACCEPTANCE (proof, not vibes)
1. A warm worker idle > TTL and NOT joined to an open Case is reaped: session closed, affiliation cleared,
   backend slot freed; an event is recorded (reuse existing vocab).
2. A warm worker joined to an OPEN Case, or with activity inside the TTL, is **never** reaped.
3. TTL is configurable with a default; default chosen so it does not fight normal re-dialogue latency.
4. Idempotent; a second sweep over an already-reaped worker is a no-op. Flag/default leaves behavior safe.
5. Targeted pytest green (worker lifecycle + affiliation + close tests).

## REALITY CONSTRAINTS
- **Do NOT** reap the Manager session or any non-worker session. Scope strictly to `case_role='worker'`, idle,
  Case-unbound (or closed-Case) sessions.
- **Do NOT restart or disrupt a live worker** — reaping is for idle warm sessions only; §0/CLAUDE.md worker-restart
  rule still holds (a live worker restart is an operator decision).
- Reuse the periodic reconciler loop; do not add a second scheduler thread.

## RESERVED DECISIONS
- **R1 — TTL default + whether it is flag-gated.** Recommendation: ship with a generous default TTL and a config
  knob; a flag is optional since the behavior is a bounded cleanup, not a semantics change — engine owner decides.

## SCOPE OUT
Turn/cost governor (A53); wake-dispatch (A52); any change to the warm-keep *policy* itself (A48 stands).

## TRAIL / EVIDENCE (fill at close)
- Branch `feat/warm-worker-idle-reaper` · PR: TBD at close.
- TTL knob: `MESH_WARM_WORKER_IDLE_TTL_SEC` → `config.mesh.warm_worker_idle_ttl_sec`, default `3600`
  (1h), `0` disables (`config/settings.py`).
- Reaper seam reused: `_stale_busy_reconciliation_loop` (`src/orchestrator.py`) — added a second
  best-effort call to `_reap_idle_warm_workers_once()` after the existing stale-busy pass each sweep.
  No second scheduler.
- DB query: `MeshDB.list_idle_warm_workers(idle_before_iso)` (`src/control/db.py`) — mirrors
  `list_stale_busy_sessions`' shape; `LEFT JOIN flow_runs` so `current_case_id IS NULL` (never
  joined) or `flow_runs.status IN _CLOSED_STATUSES` both qualify, an OPEN Case never does.
  `status IN ('idle', 'awaiting_input')` only — a `busy` (mid-turn) session is excluded by the
  query itself, not filtered after.
- Close path: `_reap_idle_warm_workers_once` re-checks the live in-memory session (status +
  case_role + a live re-read of the flow_run status) before calling
  `session_service.close_session(...)` — same guard shape as `_close_worker_session_on_case_close`.
  Remote-worker teardown already exists via `session_service`'s injected `_dispatch_remote_close`
  (enqueues a `close_session` mesh task to the owning node); this reaper is a new *caller* of that
  existing path, not a new close mechanism.
- **Motivating incident (2026-08-21):** found 3 real orphaned `claude.exe` SDK sessions on this
  operator's `tokens_ingest` worker node, pooled since 2026-08-17/18/20 — each with live CPU ticks,
  a child Python/MCP process tree, one with a bound TCP port. Root cause: sessions were closed on
  the gateway side but `_dispatch_remote_close`'s claim that "the worker's boot reaper reclaims the
  process on restart regardless, so no leak survives" only holds if the worker restarts — this
  worker daemon had 6 days uptime with zero restarts, so the boot reaper never ran. Confirms the §7
  deferral this packet describes is not hypothetical.
- Idempotent by construction: `close_session` sets `status = CLOSED`; the query only matches
  `idle`/`awaiting_input`, so a second sweep over an already-reaped session is a natural no-op —
  no separate dedup needed.
- pytest: `tests/test_warm_worker_idle_reaper.py` (8 new, DB-query + orchestrator-sweep coverage) +
  `tests/test_mesh_reconcile_spool.py` + `tests/test_session_affiliations.py` — 22/22 green,
  targeted run only (test-cost guard — no e2e, no full suite).

---
## Milestone (burndown)
- [x] idle warm-worker reaper on the existing sweep loop (TTL-bounded)
- [x] never reaps open-Case-joined / mid-turn / non-worker sessions
- [x] idempotent + configurable TTL + safe default
- [x] targeted pytest green
- [ ] PR opened + merged

## Closure (fill on completion)
Pulled forward from "ready" (deferred pending live M3.4 load) after directly observing the leak it
predicts: 3 real orphaned worker sessions on a 6-day-uptime worker daemon, confirming
`_dispatch_remote_close`'s restart-reclaim assumption does not hold without an actual restart.
Implemented as scoped in SEAM MAP: reused `_stale_busy_reconciliation_loop`, no new scheduler; reuses
`session_service.close_session` + `_clear_session_case_affiliation`, no new close mechanism. PR open
next.
