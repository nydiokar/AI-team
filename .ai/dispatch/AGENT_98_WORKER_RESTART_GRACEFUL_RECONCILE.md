```yaml
job_id: AGENT_98_WORKER_RESTART_GRACEFUL_RECONCILE
created_at: "2026-10-07T09:59:04.000000+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: "incident-investigation"
depends_on: []
results_ref: DISPATCH_LOG.md#A98             # -> DISPATCH_LOG.md section with the verdict prose
evidence:
  tests/test_a98_restart_reconcile.py   # this packet (scorecard + plan)
updated_at: "2026-10-07T11:06:27.405377+00:00"
```

# DISPATCH — A98 · Worker-restart graceful reconcile (the "one restart → big mess" incident)

**Level:** 3 (orchestration + worker driver + new session-death trigger; migration-light; live restart
drill required) · **Type:** incident remediation (multi-objective)
**Authored:** 2026-10-07 (investigation of the Horse worker restart night of 2026-10-06→07)
**Depends on:** — (A54/A55 already merged + enabled; this fixes their trigger blindness)
**Branch:** one `feat/<slug>` per objective, PR + self-merge per objective.
**Default posture (operator ruling):** every fix here ships **ON BY DEFAULT**; add a flag only to
*disable* it (opt-out), never opt-in.

> **Why this packet exists.** On 2026-10-06T23:51Z Manager `220479345b5f` (Horse, `fable`) opened Case
> `00c48658d479443a8210dc705be7dad3` and drove a healthy review loop with worker `b6240c496a45`
> (Horse, `opus`) until **~2026-10-07T01:48Z**, when the **Horse worker daemon restarted once**
> (incarnation `ed16bb16…`→`924fd5b2…`, proc `e118e10f`→`f7237fa4`, ~35 s down; cause almost
> certainly OOM — a background TRAIN/tune pipeline + a heavy worker script; confirm on Horse). The
> restart destroyed the in-memory SDK driver objects. What followed was **not** a coherent recovery:
> it was a mess of repeated failed resumes, an incidental worker recovery, a permanently dead Manager,
> and real money spent. This packet is the dedicated, multi-objective remediation.

---

## SCORECARD — did the system handle the restart adequately? (verdict: NO, on the dimension that matters)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| A | Detection | ❌ Poor | No node offline/online or incarnation-change event logged. Discovered only via failed turns (worker +9 min; Manager **+1 h 3 m**, at its next due turn). |
| B | Manager (control-plane) recovery | ❌ **Failed** | No fallback resume, no respawn, zero approvals, no `manager_unavailable`/durable flow event. Manager `error`/`driver_lost`; Case still open >11 h. |
| C | Worker (exec-plane) recovery | ⚠️ Incidental, not designed | Came back only because it *happened* to get normal turns (in-flight rework `task_12d052f6` + a `watch_job` notify `task_39e71b2`) that ran before `driver_status` latched `lost`. Every restart-recovery turn it got **failed**. |
| D | Cost | ❌ Expensive (worker) | Manager fatals were free (empty usage). Worker `task_12d052f6` = ~1 h opus, **23.7M cached-input + 86k output**, redoing CP1 — tens of $; matches the operator's ~1.2M cache-write in the UI. ×N workers multiplies. |
| E | Safety / self-inflicted | ⚠️ Mixed | No duplicate Manager / double-exec, but: racing wake dispatch (two fails at 01:58:59), **ungated worker commits** (committed with a dead Manager), repeated cold resumes re-blowing cache. |
| F | Observability / operator alert | ❌ Absent | State locally honest, but **no Telegram/push**; operator never told a worker restarted. |
| G | Bounded / deduped retries | ⚠️ Partial | Manager 2 attempts 5 h apart — identical & doomed (no escalation after #1). Worker flapped 5×. |

**One-line verdict:** the system *degraded honestly* (no lies in state, no double-execution) but
**did not recover coherently** — and it was **not cheap** on the worker. "Survived by luck, re-tried a
corpse, spent real money, told no one."

## ROOT CAUSE — the split brain (two code locations)

1. **Worker refuses, names a fix, does nothing** — `src/backends/claude_code.py:309`. When
   `session.driver_status == "lost"` and the driver is the SDK continuous driver, `resume_session`
   returns `error_class="session_lost"`: *"…cannot be resumed by the continuous driver. Start a new
   session or explicitly request fallback resume."* The named fallback (`print_resume` =
   CLI `--resume <backend_session_id>` off the on-disk store) **works**, but **nothing auto-invokes it**.
2. **Gateway respawn is blind to this death shape** — `src/orchestrator.py:~2127`. A55 crash-respawn
   fires only for `session is None or status in (CLOSED, CANCELLED)`. A restart-lost session is
   `ERROR`/`driver_lost` → never matches → the wake path instead re-injects
   `<prior_context source="restart-recovery">` (`:7764+`, `RESTART_CONTEXT_RESTORE`, ON by default)
   into the corpse, which guard #1 then refuses. The knob (`CASE_CONTINUATION_ENABLED=1`) *is on*.
3. **No outside orchestrator for the restart event.** Nothing consumes "node incarnation changed" as a
   single coherent signal. Each session hits the broken resume path independently and repeatedly → the
   mess. Self-healing on each half is not enough; the healing must be *orchestrated once per restart*.

## OBJECTIVES (priority order; each its own PR, each ON BY DEFAULT)

- **O1 — Fork a restart-lost session onto a FRESH `create_session` (gateway-side; keystone). ✅ SHIPPED.**
  *Chosen approach (cleaner than the worker-side fallback-resume variant and gateway-only).* In
  `_mesh_dispatch_payload`, a non-enrolled session with `driver_status=="lost"` now dispatches
  `create_session` (fresh subprocess → role re-boot + A54 boot-reconcile + the already-built
  `<prior_context>` injection) instead of `resume_session`, which the worker guard
  (`claude_code.py:309`) refuses into a corpse. This sidesteps the refuse-guard entirely and matches
  the documented restart-recovery design intent (`orchestrator.py:7742`). On by default; opt-out
  `RESTART_LOST_SESSION_FORK_DISABLED`. This alone would have recovered BOTH sessions in the incident.
  *(The worker-side auto-`print_resume` fallback is left as a possible defense-in-depth follow-up; not
  needed once O1 routes around the guard.)*
- **O2 — Route `session_lost` to re-establish (gateway-side).** Treat `ERROR` + `driver_status=="lost"`
  as a dead session eligible for A55 crash-respawn/re-establish; a `session_lost` fatal result must
  trigger the re-establish path, not log+drop. Uses existing `CASE_CONTINUATION_ENABLED`.
- **O3 — Incarnation-change reconciliation (the coherent event).** On observing a node's incarnation
  flip, mark that node's in-memory-backed sessions `driver_status="lost"` ONCE and enqueue ONE
  single-flight, bounded re-establish per session: prefer O1 fallback-resume; else O2 fork/respawn from
  `get_case_brief`. This replaces the per-session flapping with one holistic pass.
- **O4 — Don't re-inject into a corpse / de-dupe.** Suppress or redirect `RESTART_CONTEXT_RESTORE`
  injection when `driver_status=="lost"` (don't spend building context for a doomed turn); single-flight
  the wake on a lost session so 02:51 + 07:57 identical re-pokes can't recur.
- **O5 — Operator notification.** Push + Telegram on detected node restart + the re-establish outcome,
  reusing the existing notification seam (as quota-pause already does).
- **O6 — Cost guard.** Re-establish is single-flight + idempotent; never repeatedly cold-resume the same
  session (each re-creates the full cache); check committed git state before redoing work.
- **O7 — Operational (overlaps the Horse-side investigation).** Worker-daemon memory watchdog
  (RSS/cgroup limit, restart-on-pressure) + a gateway alarm on unexpected node restart. **This is the
  "operationally missing but needed" outcome:** there was no watchdog and no restart trail.

## ACCEPTANCE (proof, not vibes)

1. **Live restart drill (the headline proof).** On a rebuilt container + a PM2 worker on new code:
   dispatch a cheap (`haiku`) worker a small task, **immediately restart its worker process**, and show
   it reconciles via ONE coherent path (fallback-resume or clean fork) — no "session_lost" dead-end, no
   resume storm, no duplicate work, operator notified. Capture the flow_events + task rows + cost.
2. Targeted unit tests for O1 (lost → fallback resume, not refusal) and O2 (`ERROR`+`driver_lost`
   reaches `_handle_dead_manager_session`). No full/e2e suite.
3. Cost: the drill's re-establish creates the prompt cache **at most once** per session.
4. Each shipped objective is ON by default; its opt-out flag is registered and defaults to the ON
   behaviour.

## RESERVED DECISIONS (surface, do not guess)
- Fork-vs-resume policy when the on-disk backend session is itself gone (cache TTL + staleness): resume
  if present and fresh, else fork/respawn from brief. Confirm the freshness threshold with cost in mind.
- Whether an ungated worker should keep executing when its Manager is dead (O-safety) — likely pause or
  flag deliverables as un-reviewed; needs an owner ruling.

## Milestone

**O1 — SHIPPED + LIVE (2026-10-07).** PR #193 merged to `main` (`c73c51f`); gateway + task-server
rebuilt and recreated on `ai-team:prod-c73c51f` (schema 42, no migration; rollback `ai-team:pre-c73c51f`;
DB backup `~/ai-team-data/backups/mesh-pre-c73c51f-20261007T1016Z.db`). Verified live: both containers
healthy, `/health` ok, both nodes (kanebra-worker, Horse) recovered online, `RESTART_LOST_SESSION_FORK_DISABLED`
registered in the running image and `restart_lost_session_fork_disabled()` resolves `False` (fork ON) in
the live process. 5 targeted tests + adjacent suites green. **Gateway-only — no worker restart needed.**

**O1 live end-to-end drill — PENDING (operator decision).** Both online workers are production
(policy forbids restarting kanebra-worker/Horse). A clean drill needs a dedicated throwaway worker
(`worker_main.py` with a distinct `WORKER_NODE_ID`, pinned so it claims only the drill session):
create a haiku session pinned to it → run one turn (establish `backend_session_id`) → restart that
worker → confirm the next task dispatches `create_session` (not `resume_session`), forks fresh with
injected `<prior_context>`, and continues. Deferred to avoid spawning a stray worker on the shared tree
without a go-ahead.

**O2 — BUILT (crash-respawn for ERROR+driver_lost).** The A55 wake-path respawn trigger (only
`None/CLOSED/CANCELLED`) now also fires for a session left `ERROR` + `driver_status=='lost'` by a
restart (discriminated by `driver_lost` so a genuine ERROR is untouched). Covers the case where O1's
fresh-fork itself failed, or a `session_lost` turn marked the Manager ERROR before O1 could fork — the
incident's 02:51 + 07:57 identical re-pokes. Opt-out `RESPAWN_ON_RESTART_ERROR_DISABLED` (default ON).
Predicate extracted to `_is_restart_dead_session` (5 unit tests).

**O3 — ALREADY BUILT (verified), no change needed.** Incarnation-change reconciliation is complete:
`NodeRegistry.register` (`src/control/node_registry.py:126`) diffs old vs new incarnation and, on
change, releases claims + revokes managed grants + `mark_driver_sessions_lost_for_node`
(`db.py`) → sets `driver_status='lost'` on that node's idle/awaiting SDK sessions. This is HOW the
incident's sessions got `driver_status='lost'`. The missing half was the *reaction* (O1/O2), not the
detection.

**O4 — SATISFIED by O1 + existing single-flight (no new code).** With O1 a lost session's wake
dispatches `create_session` (fresh), so restart-context is injected into a FRESH session, never a
corpse. The wake path already skips BUSY sessions and holds a single-flight claim lease, so the
02:51/07:57 re-poke + 01:58:59 double-dispatch cannot recur (a fork in flight → session BUSY → later
wakes skip). Documented here rather than adding a redundant guard.

**O5 — BUILT (operator notification).** `TaskOrchestrator._detect_node_restarts_once` (in the stale-busy
reconcile loop) tracks per-node incarnations and fires `NotificationService.notify_restart` (best-effort
Web Push + Telegram, mirrors the quota-resume seam) ONCE per detected flip — the signal that was missing
(the operator got no Telegram when Horse restarted). Opt-out `RESTART_NOTIFY_DISABLED` (default ON).
First sighting seeds a baseline (no spurious notify). 3 unit tests.

**O6 — SATISFIED by O1 + single-flight (no new code).** O1 forks ONCE (`create_session` mints one new
backend session = one cache creation); on success `driver_status→live`, so later turns resume warm.
The expensive repeated cold-resume (the incident's ~1.2M cache-write) was the OLD resume-into-corpse
loop, which O1 removes. BUSY-skip + claim lease prevent a concurrent second fork. Residual: the single
fork still pays one cache creation — unavoidable when the in-memory driver is gone.

**O7 — BUILT (worker memory watchdog).** `WorkerAgent._memory_watchdog_sample` samples the worker
process-tree RSS each heartbeat, warns (`event=worker_memory_pressure`, throttled 1/min) and rides a
`memory` field on the heartbeat `live_state` (LiveStatePayload is `extra="allow"`) when over threshold
(`WORKER_MEMORY_WATCHDOG_PCT` default 85, or `WORKER_MEMORY_WATCHDOG_MB`). This is the OOM early-warning
trail that was entirely absent. Opt-out `WORKER_MEMORY_WATCHDOG_DISABLED` (default ON). 2 unit tests.
*(Gateway-side restart detection already logs `event=node_restart_detected` via O5; node-offline already
logs `event=node_offline`.)*

**Flags registered (all default to the ON behaviour):** `RESTART_LOST_SESSION_FORK_DISABLED` (O1),
`RESPAWN_ON_RESTART_ERROR_DISABLED` (O2), `RESTART_NOTIFY_DISABLED` (O5),
`WORKER_MEMORY_WATCHDOG_DISABLED` (O7) — all in `RUNTIME_FLAG_DEFINITIONS`.

**Remaining to fully close:** deploy O2/O5/O7 (gateway redeploy for O2/O5; O7 is worker-side — lands on
the next worker restart, operator-gated) + the live restart drill with a dedicated throwaway worker.

## Closure (2026-10-07)

**All objectives delivered. O1/O2/O5 live on the gateway; O3 pre-existing; O4/O6 satisfied by design;
O7 code shipped (worker-side, activates on the next operator-gated worker restart).**

- **PRs:** #193 (O1) + #194 (O2/O5/O7) merged to `main`. Gateway + task-server rebuilt/recreated on
  `ai-team:prod-621acd6` (schema 42, no migration; rollback `ai-team:pre-621acd6`; DB backup
  `~/ai-team-data/backups/mesh-pre-621acd6-20261007T1055Z.db`). Both healthy, `/health` ok, both nodes
  online.
- **Flags persisted + operator-visible** (all `value=0` ⇒ ON behaviour) in `runtime_flags`:
  `RESTART_LOST_SESSION_FORK_DISABLED`, `RESPAWN_ON_RESTART_ERROR_DISABLED`, `RESTART_NOTIFY_DISABLED`,
  `WORKER_MEMORY_WATCHDOG_DISABLED`.
- **Live A/B drill (O1), the headline proof** — throwaway haiku session `0bb2b40a951c` on kanebra-worker,
  `driver_status` set to `'lost'` to reproduce the exact restart state (safe: one scratch row; the
  separate-worker variant was aborted after it hit the `.env` node-id override — a duplicate-node
  footgun — with NO collateral damage, incarnation unchanged):
  - **O1 ON (default):** turn `task_7e53a1ab` → `action=create_session` → **completed**, reply
    "RECOVERED"; session recovered `lost→live`, `awaiting_input`.
  - **O1 OFF (A/B):** turn `task_2e816ff9` → `action=resume_session` → **failed/fatal**, error
    *"[kanebra-worker] Claude session was lost after a worker restart and cannot be resumed by the
    continuous driver…"* — the exact incident dead-end.
  - ⇒ The routing fix is demonstrably what converts the incident's permanent-death into graceful
    recovery. Boundary (honest): the simulate kept kanebra-worker's in-memory driver alive, so
    create_session reused the backend session rather than minting a fresh one; the gateway *routing*
    (the fix) is proven, the fresh-subprocess mint after a real in-memory loss is covered by unit tests
    + `_get_or_create`'s existing respawn path, not re-proven live (would need a real worker restart,
    avoided to protect production).
- **Tests:** 15 targeted (`test_restart_lost_fork.py` 5, `test_a98_restart_reconcile.py` 10) + regression
  across continuation/respawn/quota/wake/turn-queue/push/control-api/affinity/restart-context — green.
- **Operator-gated remainder:** O7 (worker memory watchdog) + O1/O2 worker-side benefit run from the
  image but the **workers run old in-memory code until restarted** — a node-carrier restart is
  operator-gated (hard rule). Surface a worker restart to activate O7's `event=worker_memory_pressure`
  trail and have the Horse/kanebra workers on A98 code. The gateway-side fixes (O1/O2/O5) are fully live
  now regardless of worker code.

## SCOPE OUT
- The A82 managed-turn (message-queue) cutover — these sessions are NOT enrolled; this fix targets the
  **current** legacy continuous-driver path so it stops being a mess today. Re-verify under A82 later.
- Closing Case `00c48658…` — operator is deliberately keeping it open to review the delivered results.
