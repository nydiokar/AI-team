# AI-Team Gateway — Hot Context

**Last Updated:** 2026-08-10
**Branch policy:** `main` for docs-only; `feat/<slug>` + PR + self-merge for any `src/` or config
change. Restart the **gateway** freely when deploying merged code. Never restart a worker/node-carrier
without surfacing it to the operator first.

---

## How to use this file

This is **shift notes** — a fast-orientation handoff between agents and sessions. It tells you
what is active right now, what the current focus is, and where things live. It is NOT:

- a task board (active jobs live in `.ai/dispatch/` — see below)
- a status dump (resolved work does not belong here; it lives in git + `docs/archive/`)
- a design doc (design lives in `docs/`)

**Keep it honest and trim.** When you close a job, remove it from the "Active Work" table
below — don't leave it here with a ✅. When you add a shift note, delete anything it supersedes.
If you feel the urge to write a multi-paragraph STATUS block, put it in the dispatch packet
instead and leave a one-liner here.

---

## Current Focus

**Task harness specification + dispatch system modernisation.**

Current work:

1. **A65 alert-delivery remediation** — final review found that P3 displays alerts in the Cost tab
   but does not deliver them through the existing browser-push seam stipulated by the packet. Reuse
   bounded `PushService` fanout at terminal task outcome; no new kill path or quota integration.


> **Finding active jobs:** until the dispatch-state-kit is installed, read
> `.ai/dispatch/DISPATCH_LOG.md` → Index table. Each row points to its `AGENT_N_*.md` packet.
> After A51 lands, use the generated board / script instead.

---

## Active Work (open dispatched jobs, as of 2026-08-03)

Only jobs that are genuinely open. Everything merged/done is in git and the dispatch packets.

| Job | Packet | Depends on | Status | What it is |
|---|---|---|---|---|
| **A82** | `AGENT_82_SESSION_TURN_QUEUE.md` | — | active — Stages 3, 4a–4d ACCEPTED 2026-09-26 (`9e25172`, pushed, unmerged); Stage 4e (producers 5 quota/transient retry + 7 respawn) next | Unified durable turn queue on `feat/session-turn-queue` (worktree `~/dev/AI-team-wt/a82`, never merged until reviewed). Stages 1–2 ACCEPT; Stage 3 reworked twice, adversarial review pending. End state = ONE pathway: new Stage 8 cutover deletes protocol 0 (see A87 rulings). **Deferred (§7 trace):** an oversize managed result enters `recovery_required` with a bounded reason, but the full output is not persisted to carrier artifact storage (lives only in worker memory until the handler exits) — must be closed before Stage 8 deletes the legacy path. **Also deferred:** (a) a `run_in_background` shell reparented away from a dead Claude CLI is not covered by the process-gone proof (SDK 0.2.110 exposes no process-group/new-session hook; not patching vendored code) — the next managed turn could overlap it; (b) psutil is declared/pinned on the branch but not installed in the live venv, so `reap_stale_worker_children` is still a no-op until the constraints install runs at deploy. **Stage 4a preconditions/deferrals:** (c) enrollment may only be performed inside the gateway process (per-process presence flag) — Stage 7 must enforce; (d) set `MESH_LOCAL_CARRIER_NODE_ID` = local daemon `WORKER_NODE_ID` before enrolling any session; (e) Stage-6 withdraw of a lineage-pending/partially-lineaged row MUST void/close the child flow_run and clear the session affiliation (else phantom child blocks parent close_case and inflates the task.dispatched gate count); (f) `/api/instructions` body cap is derived ≈3.8 MiB for ALL callers (design said 2 MiB); (g) `node_heartbeat_timeout_sec` must be ≥ 2× the 30 s worker heartbeat; (h) cross-process completion wakes the scheduler within ≤30 s (real wake deferred to A84); (i) Telegram retries are not deduplicated (no stable inbound id). **Stage 4b carries:** (j) operator-stop hold is a durable record, but stale whole-row session saves can still rewrite the displayed status; (k) Stop with no active turn holds nothing (legacy parity); (l) operator `sweep_orphaned_cases` force-closes a stopped Manager's Case; (m) `X-AI-Team-Principal: automation` is self-declared under the shared token — an automation caller that omits it counts as operator until A71 authenticated principals. **Stage 4c carries:** (n) failed/failed_node_offline continuation turns consume the wake even if the Manager never ran (legacy parity); (o) A84 must fold wait_resolved + outbox consumption + token CAS into one txn. **Stage 4d carries:** (p) watched-job notification delivery is at-most-once across gateway restarts (in-memory poll watermark, legacy parity); (q) heartbeat turns ending failed_node_offline/cancelled count no beat (intentional); (r) linked heartbeat leases finalize only while CASE_CONTINUATION or CACHE_HEARTBEAT flag is on. |
| **A75** | `AGENT_75_DASHBOARD_TOKEN_NOT_IN_HTML.md` | A71 design | dispatched | Remove the control token from served dashboard HTML (`window` global); keep TokenGate working via a non-page-inspectable flow. Sequenced after A71's credential design. |
| **A71** | `AGENT_71_MESH_PER_NODE_CREDENTIALS.md` | — | dispatched | Replace the single shared `WORKER_TOKEN` with gateway-issued per-node credentials bound to `node_id` on register/heartbeat/claim/result; refuse cross-node claims; stop spoofed incarnation-bump DoS. Flag-gated default OFF. Worker-side lands on surfaced redeploy (Horse). |
| **A65** | `AGENT_65_COST_MONITORING_VISIBILITY.md` | — | active — final-review remediation | Add the missing bounded browser-push delivery for P3 budget alerts; UI/API and enforcement-off governor seam already landed. |
| **A54** | `AGENT_54_M34_JOB2_RECONSTRUCTION.md` | A52 ✅ | dispatched | `get_case_brief` DB read + auto-reconcile/re-arm at role-boot. Prerequisite for crash-respawn. |
| **A55** | `AGENT_55_M34_JOB3_CRASH_RESPAWN.md` | A54 | dispatched | Respawn a role-full Manager on a dead-session Case. Closes "survive a process restart." |
| **A56** | `AGENT_56_M4_SPEC_AUTHORING_DECOMPOSER.md` | A52 ✅ | dispatched | M4: spec authoring + rubric-scored review + decomposer-as-task-DAG inside ONE Case. |
| **A57** | `AGENT_57_M4_HYBRID_EXECUTOR_SPIKE.md` | A55 | dispatched — GATED | Go/no-go on SDK Dynamic Workflows vs hand-rolled DAG for intra-task parallel executor. |
| **A58** | `AGENT_58_QUOTA_COORDINATOR_ACTIVATION.md` | A61 | **blocked** | Activating the quota coordinator — blocked until A61's real Claude adapter is independently reviewed (A63). |
| **A60** | `AGENT_60_WARM_WORKER_IDLE_REAPER.md` | — | dispatched | Idle-reaper for warm workers (§7 deferral from A48); sequence after M3.4 flag is ON. |
| **A61** | `AGENT_61_QUOTA_COORDINATOR_FINALIZATION.md` | — | built (direct commit `cbbaa10`, no PR) | Real Claude status-line adapter + quota windows API. **Needs A63 independent audit before treating as done.** |
| **A62** | `AGENT_62_RUNTIME_FLAG_REGISTRY_NONBOOLEAN.md` | — | dispatched | Extend `/api/flags` to numeric/string knobs; land `CLAUDE_SDK_MAX_TURNS`/`CLAUDE_SDK_MAX_BUDGET_USD` as first real numeric case. |
| **A63** | `AGENT_63_QUOTA_COORDINATOR_INDEPENDENT_AUDIT.md` | — | dispatched | Independent audit of A61's commit — re-derive every gap claim from the tree, not from the packet. |

**Operator-gated validations still pending:**
- Job 1 live activation: `CASE_CONTINUATION_ENABLED=1` + gateway restart (runbook in `manager.md`).
- Durable-relay e2e: `DURABLE_RELAY_ENABLED=1` + marker→crash→reconcile through live gateway.
- Whole-loop hands-off: bounded autonomous run, no operator poke.

---

## Recent shift notes

**2026-09-25 — Docker migration regressions fixed. PRs #162/#163 MERGED; gateway + task-server recreated on `76a25a9`.**
**UID:** containers dropped to hardcoded uid 10001, host repos are uid 1000 → workers could read but never write
(proven live: 10001 `Permission denied`, 1000 OK). Entrypoint now drops to the owner of the mounted `/app/state`
(never root; fallback 10001; `APP_UID`/`APP_GID` override) — no `.env` edit needed, ownership of
`~/ai-team-data` decides. `controller/` chowned to 1000 → gateway/task-server run as uid 1000.
`workers/kanebra-worker/` chowned to 1000 and the worker recreated (operator-approved) → runs as uid 1000.
**PROVEN via the gateway:** `POST /api/sessions` (claude/haiku, node `kanebra-worker`) + `/api/instructions` → worker
created a file and committed `f40fe06` in a scratch repo; host `git log` + file owner `cifran` confirm. Horse needs the
same chown + recreate if it runs Docker.
**Phantom nodes:** task server registered `socket.gethostname()` (container id) as a gateway self-node; now only when
`local_execution_enabled` (compose sets it false on task-server too). Deleted phantom `e8d0cac9b780` and stale
PM2-era `kanebra` (online in DB since 2026-09-24 22:27Z — nothing marks rows offline that the restarted
registry never loaded). **Deferred:** load the node registry from mesh.db on task-server start so stale rows age
out and a restart doesn't cause a 404/re-register storm. **Banner:** `~/scripts/aiteam-healthcheck.sh` (cron, source
now in `~/dev/server-ops` `e0421cc`, no remote) probed PM2 and wrote to the old repo `state/mesh.db`; now probes
Docker container state and the live `~/ai-team-data/controller/state/mesh.db`. Stale alert #6 resolved.
Branch `feat/docker-production-bundle` (+ its worktree) deleted: identical Docker content, its "deletions" were a stale base.

**2026-09-19 — CI green again; deps locked + upgraded; token injection restored for trusted requests. PRs #154–#159 MERGED + DEPLOYED.**
CI had been red since #150: prod `.venv` sat on fastapi 0.115 (pyproject `>=` only; `pip install -e` never upgrades) while
CI pulled 0.141, which returns 401 not 403 for a missing bearer. **#154** — `_require_auth` owns the missing-token 401
(`auto_error=False`); `constraints.txt` pins the exact stack, CI installs `-c constraints.txt`. **#155** — upgraded to
fastapi 0.141.1 / starlette 1.6.0 / uvicorn 0.53 + all minor bumps; prod venv reinstalled from the lock, strays removed
(`e`, `httpx2`, `httpcore2`, `truststore`). **Held back on purpose (own PRs):** `claude-agent-sdk` 0.2.110 (latest
0.2.157), `mcp` 1.28 (2.x is major), `watchdog`/`python-dotenv` (exact pins). The local `ai-team-worker` shares `.venv`
and was NOT restarted — it runs the old in-memory versions until its next restart (operator's call).
**#156/#157/#158** supersede #150's "never inject": `/` injects the token only when Host is `*.ts.net` /
CONTROL_API_HOST / a tailnet IP literal (blocks DNS rebinding) AND the peer is a REMOTE tailnet device — loopback and
any of this host's own addresses are never trusted (#158: other host-networked services on the
same box share loopback; `tailscale serve` still works because uvicorn swaps in the remote IP from X-Forwarded-For).
Residual (accepted): a local process forging X-Forwarded-For over loopback. Index pages send X-Frame-Options: DENY;
injected page is `no-store`. Your devices need no pairing; `#token=` + TokenGate remain the fallback. The
"DEPLOY PENDING" item below is done. Known pre-existing flake on the Pi: `test_push_notifications.py::
test_fanout_disables_gone_and_records_timeout` (wall-clock < 0.9s; ~1.04s on this host, passes on CI).

**2026-09-18 (night) — Disk I/O proven as the slowness root; token-in-HTML removed; app metrics added. PRs #150 + #151 MERGED, NOT DEPLOYED.**
Evidence: host is a Pi on a USB spinning disk (TOSHIBA MK3275GSX, ~90 IOPS ceiling) shared with the unrelated
`sova` docker stack. `sova-backend` was crash-looping (23 restarts on 09-18, all exit 134 = V8 heap OOM), each
cold start saturating the disk. Stopping it (23:42 EEST): load 8–9→~1, iowait 46–78%→~1%, `sda` util 97%→3–10%,
>1s requests 51/h→0 (10-min window only — confirm with the new metrics). A second disk is planned by the operator.
**#150** — `GET /` no longer injects `window.__DASHBOARD_TOKEN__` (any tailnet peer/crawler got the full API token;
identity headers unusable: all nodes are tagged). Pair a device via `/#token=<token>` or the TokenGate; compare is
constant-time. **#151** — `src/control/app_metrics.py`: route-template request histograms, loop lag, /proc host
sampling → `GET /api/metrics/system` + `logs/metrics.ndjson` (1 rollup/min, ≤4 MB/day, no DB).
**DEPLOY PENDING (operator decides):** `cd web && pnpm build` (web/dist is gitignored; needed for `#token=` pairing)
then `pm2 restart ai-team-gateway`. After it every device sees the TokenGate once. Optional: rotate DASHBOARD_TOKEN
(it was served to every fetcher until now). Deferred: task-server loop lag (own thread) is not probed; no browser-side timing.
**#153** adds `GET /api/metrics/health` (verdict: host disk/memory/thermal/cpu outranks app event-loop/slow-route) + `HostHealthBanner` (only when not ok). `APP_METRICS_ENABLED` registered in the orchestrator flag list + `_MANAGED_ENV_KEYS` (#152); `.env.example` not checked.

**2026-09-18 — Persistent slowness root-caused to the Wake-Dispatcher polling the DB on the
event loop; 2 PRs merged (#145, #147). NOT yet deployed.**
Live evidence (09-17/18): `event=embedded_event_loop_stalled elapsed_ms=5000–9684` ×15 +
recurring faulthandler dumps. The dump proved the gateway MAIN loop running synchronous
SQLite inline: `_wake_dispatcher_loop → _continue_case_once → compute_continuation_tick →
list_flow_events`, and elsewhere `_handle_quota_paused_case → quota_window_state`. It scanned
ALL open Cases every 30s, re-reading each Case's ≤500-row event log + a provider-global quota
snapshot PER CASE — O(cases×events) blocking work whether or not anything changed. One process
⇒ GIL starvation tripped the embedded task-server's stall watchdog and stalled every HTTP path.
**#145** (`a6b7c7b`) offloads the reads to `asyncio.to_thread`; **#147** (`a750c7e`) makes the
tick event-driven: `MeshDB.max_flow_event_ids()` (one batched, index-served watermark) lets an
unchanged Case skip its read+recompute; quota state computed once/tick not once/Case; reuse the
`list_open_cases` row instead of a redundant `get_flow_run`. Behaviour-preserving; 114 tests
green. **DEPLOY PENDING:** main checkout was on a concurrent agent's branch with uncommitted WIP
— `git checkout main && pm2 restart ai-team-gateway` when clean. Remaining structural work →
"DB-contention optimization backlog" below.

**2026-09-18 — Stale/wedged open Cases diagnosed (OPT-3).** The Wake-Dispatcher saw 14 "open"
Cases; ~10 were orphans (Manager session `closed` but `flow_runs.status` NULL — Case lifecycle
is not tied to session lifecycle; `close_case` never called automatically) and **4 are WEDGED**:
each carries a **pending `case_manager_respawn` approval** (raised when the Manager died, then
ignored). `close_case` hard-blocks on `_case_has_unresolved_approval` (`db.py:2985`), so those
Cases can't be closed — even manually ("Close failed: case has an unresolved required approval")
— AND the respawn never fired. Approvals have `expires_at` but **nothing enforces it**, so an
ignored proposal wedges forever. Immediate relief (no code): `POST /api/approvals/{id}/resolve`
`decision=reject` clears the pending approval → close unblocks. Proper fix = OPT-3.

**2026-09-15 — "Slow / lost messages / dishonest state" cascade: root-caused to DB write
contention + telemetry-on-hot-path; 4 PRs merged & deployed.**
Symptoms (operator): sessions slow to start, waits on connect, messages not visible, a turn
reported "running" that had actually errored. Evidence from live logs (09-14/15): `/telemetry/batches`
was the #1 slow task-server path (160 `task_server_request_slow`), a `/nodes/heartbeat` measured at
**178 s**, and `sqlite3.OperationalError: database is locked` on the telemetry write path. The old
`httpx.ConnectError`×3634 flood is historical noise (2026-08-28), ruled out.

**The cascade (one shared bottleneck).** Every task-server endpoint is a sync `def` running in the
anyio threadpool, and they all funnel through `MeshDB`'s single process-wide `_write_lock`. Telemetry
ingestion ran the CPU-bound turn projection (`rebuild_turn`) + session-growth refresh **synchronously
inside the request**, holding that lock while node heartbeats, task claims, and result submissions
queued behind it — so the worker's lifeline stalled and sessions couldn't start. A locked write
*aborted* (the exception dropped it), which is the "said running but errored, not captured" dishonesty.

**Fixes (all merged to main, gateway restarted; #138 is worker-side, awaits worker restart):**
- **PR #135** — finished the WIP branch (transcript projection now renders a dispatched-but-unanswered
  prompt instead of a blank turn = "message not visible" fix) + the CI break (`submit_result` needed a
  `background_tasks=None` default) + dropped a committed ctags `tags` file.
- **PR #136** — `submit_telemetry_batch` now inserts raw events fast (`rebuild=False`) and defers the
  projection to a coalescing background flusher (task-server lifespan) via `asyncio.to_thread`. A turn
  getting 20 activity events/sec is projected once, not 20×. Removes the #1 slow path.
- **PR #137** — `_begin_immediate` retries transient `database is locked` on the BEGIN (idempotent) with
  bounded backoff so a control-plane write is never silently dropped; `busy_timeout` 5s→15s; a 60s WAL
  checkpoint on the flusher tick. **Verified live: WAL 12.6 MB → 33 KB after one tick.**
- **PR #138 (root cause, worker-side)** — a co-located worker with `MESH_SHADOW_WRITE=true` (default)
  was writing telemetry BOTH over HTTP to the gateway AND directly into the **same** `state/mesh.db`
  via a second OS process (`DatabaseTelemetrySink` mirror) — pure cross-process `BEGIN IMMEDIATE`
  contention for zero benefit (HTTP already lands in that file). Now the local mirror is dropped when
  the HTTP target is on this host (loopback / own tailscale_ip); remote workers keep their ledger.
  **Activates on next worker restart** — until then the running worker still double-writes.

**Layer boundaries (as dissected — the map for the next refactor).**
- *Gateway orchestrator* (`orchestrator.py`, in-process): writes session/task/flow state via the shared
  `MeshDB` + `_write_lock`.
- *Embedded task server* (`task_server.py`, own event loop): sync handlers in the threadpool, same shared
  `MeshDB`/lock. Hot control-plane paths (heartbeat/pending/claim/result) and bulk telemetry share ONE
  write mutex — the coupling behind the cascade.
- *Worker daemon* (`worker_main.py` → `src/worker/agent.py`, separate OS process): talks to the gateway
  over HTTP for control, but its telemetry sink ALSO opened `mesh.db` directly (the #138 defect).
- *Telemetry tables* (`llm_events` 82k rows / `llm_turns`) live in the **same** `mesh.db` (186 MB) as
  control-plane state — append-heavy, eventually-consistent data sharing the control-plane write lock.

**Concrete next refactor (NOT yet done — the clean separation).** Move telemetry to its **own SQLite
file** (`state/telemetry.db`, own connection + own write lock) so append-heavy telemetry writes can
NEVER contend with control-plane writes. Blast radius is contained: `telemetry_store.py`,
`telemetry_sink.py`, `session_timeline.py`, `db.py` (DDL), `orchestrator.py` (readers). Steps:
(1) add a second `MeshDB`-style handle bound to `telemetry.db`; (2) move the `_LLM_TELEMETRY_SCHEMA_SQL`
DDL there; (3) one-time copy-migrate the existing `llm_events`/`llm_turns` via `ATTACH`; (4) repoint
`TelemetryStore` + the 4 readers; (5) keep cost/timeline reads working during transition. This is the
last structural coupling; #136/#137/#138 already removed the acute pain, so schedule it as a deliberate,
migration-tested PR rather than a rushed cutover.

**2026-09-15 (cont.) — Slow requests persisted after the first batch; root cause refined + 2 more PRs.**
Live logs showed `/telemetry/batches` at **25–45 s** under load (07:45Z) and still **5 s** + a burst of
heartbeat/`/jobs`/`/tasks/pending` stalling at an identical ~1960 ms **at LOW worker load** — proving the
stalls are I/O + write-lock sharing, not query cost (a standalone `SELECT ... LIMIT 20` on the 186 MB
mesh.db hit 2.5 s cold vs ~100 ms warm; worker telemetry write rate was 2 rows/20 s).
- **PR #139** — SQLite I/O tuning on every MeshDB conn: `synchronous=NORMAL` (drops an fsync/commit),
  `mmap_size=256MB` + `cache_size=8MB` (cold reads hit the page cache), and periodic checkpoint
  `TRUNCATE`→`PASSIVE` (never blocks the hot path). **WAL verified 12.6 MB → 33 KB live.**
- **PR #140** — the projection flusher still shared mesh.db's write lock and, under a backlog, projected
  ALL dirty turns in one tight loop, starving control-plane. Now bounded (`_drain_projection(max_turns)`,
  25/tick) + chunked (5) with an async pause that hands the lock back. Telemetry is droppable; a stalled
  heartbeat is not.
- **PARKED: telemetry → own DB file.** Built it, then found it UNSAFE to ship: control-plane methods
  `get_session`/`get_job`/`recent_cache_write` (quota-resume cache-health)/`cost_case_rows` read the
  `llm_*` tables from the mesh.db connection, some via **cross-table JOINs with `sessions`**. A separate
  file breaks those joins (SQLite can't join across files without `ATTACH`, and `ATTACH` re-shares the
  write lock — defeating the split). The correct split must first rewrite those 4 readers to app-level
  joins (two queries + merge) or move them onto `TelemetryStore`. Do NOT ship a naive file split — it
  silently degrades cache-health/orphan/cost reads.
- **HONEST BOUNDARY — needs a worker restart.** The heavy-load stalls also come from the co-located
  worker writing telemetry DIRECTLY to mesh.db cross-process (#138). No gateway-side change removes that;
  it stops only when the worker restarts on #138's merged code. Deferred by operator ("too much work
  happening"). Until then, expect residual stalls under heavy worker load.
The gap: a Manager turn refused by an Anthropic server-side overload classifies as
`error_class=upstream_error` (`api_error_status>=500`, PR #81) and its in-process burst retries are
spent in seconds — so a multi-minute overload went terminal → session `ERROR` → the Case stalled
(exactly the pre-PR#97 quota hole, but for transient errors, which never self-healed). Fix mirrors the
proven quota-pause seam, timed off a short **escalating fixed backoff** instead of a `resetsAt` a 529
does not carry: `TRANSIENT_PROVIDER_RESUME_ENABLED` (default **OFF**, registry-writable). When ON,
`_session_status_after_result` keeps the session `AWAITING_INPUT`; `_record_transient_pause` writes a
durable `flow.transient_paused` (Manager-only, one open pause) with `retry_at = now + backoff`
(30/60/120/300s); `_handle_transient_paused_case` in the Wake-Dispatcher tick (checked before
satisfaction, right after the quota branch) holds while the backoff runs, then AUTO-retries the
**exact** failed turn verbatim — free, no approval, single-flight via the same `claim_task` lease.
**Bounded:** attempts counted over a 15-min rolling window (self-resets for an unrelated later 529
without a success hook); once the 4-step schedule is spent it escalates `flow.transient_pause_exhausted`
instead of looping. OFF ⇒ byte-identical (transient → `ERROR`). Touched only `orchestrator.py` +
`control/db.py`; 17 new targeted tests + the three adjacent suites (quota/continuation/respawn, whose
duck-typed fakes gained the new tick-branch delegation) green. **Known boundary (documented, observe
first):** an operator manually re-sending the failed instruction while a pause is open can still race
the one auto-retry — single-flight guards the dispatcher, not the operator. `feat/transient-provider-self-heal`.

**2026-08-19 — Quota windows: keep the rhythm ticking, and resume on the provider's own clock.**
Two halves of the same problem. **(1) The 5-hour window now gets kept alive.**
`SESSION_WINDOW_WARMING_SPEC.md` items 1-6 were built long ago; 7-10 (activation, classification,
auto-activate, scheduling) never were — nothing in the tree ever opened a window. New
`QuotaWindowPrewarmer` (`QUOTA_PREWARM_ENABLED`, default OFF, needs the coordinator): when telemetry
shows **no** open five-hour window (the live signal is a five_hour bucket with no `reset_at` — seen
on this host at 04:25Z) it spends ONE minimal `haiku` turn — no tools, no MCP, no settings sources,
empty temp cwd, `max_turns=1` + budget cap — then **re-observes to verify a window actually opened**.
An activation that opens nothing counts as a failure, and 3 consecutive failures open a circuit
rather than retrying: the anchored-window premise is checked every cycle, never assumed. Schedules
off the provider's own `reset_at`, so ≤ ~5 activations/day, bounded again by
`QUOTA_PREWARM_MAX_PER_DAY`/`MIN_INTERVAL_SEC`. **Deliberate spec deviation: no quiet hours** — the
value only exists before the operator starts work (see `ENV_FEATURE_FLAGS.md` §D). Status rides on
`GET /api/quota-windows` under `prewarm`.
**(2) The quota-resume gate keyed on stale telemetry.** PR #97's restore check only consulted the
429's own `resetsAt` when evidence was exactly `no_telemetry`. But this host's observer legitimately
sleeps up to 6h (`next_observe_delay_sec` backs off to the next known reset), so its last reading is
usually a *healthy* one — neither `exhausted` nor `no_telemetry` — and a paused Case was proposed for
resume on the next 30s tick, hours early; approving it bought another refused turn. Now the reset
instant the provider attached to its own refusal **is** the schedule (it is exact and needs no
observer), and only telemetry OBSERVED AFTER the pause can release it early (Anthropic does re-anchor
limits). A healthy window's `reset_at` is no longer recorded as a pause boundary at all.
**(3) Resume mode is now decided by the prompt cache, not by liveness**: `in_place` while the pause is
younger than the ~1h cache TTL (nothing to rewrite) **or** the session's recent cache writes are
under 100k; otherwise `fresh_manager` from the Case brief — an unmeasured session assumes the
expensive case. Previously a quota-killed Manager (which stays `AWAITING_INPUT`) always recommended
`in_place`, i.e. the 200-300k rewrite this seam exists to avoid. **(4) The spec is now closed out, and the proposal is notified on two channels.**
`SESSION_WINDOW_WARMING_SPEC.md` carries a **§19 Conformance Record**: items 1-7 + 9 built, item 8
(three-cycle classification) replaced by *continuous* falsification, item 10 (work horizons)
deliberately dropped. Three gates were added to close §7/§9A/§13 honestly: **anchor-drift detection**
(a `reset_at` that moves forward while the old boundary has not elapsed = sliding window ⇒ circuit
open, i.e. ambiguity disables automation), **cost measured as the provider's own `used_percent`
delta** across the activation (2 breaches ⇒ circuit), and a **principal-identity gate**. Cross-node
locking (§9A) is N/A by architecture — only the gateway constructs a prewarmer, never a worker.
The resume proposal now reaches the operator on **Web Push + Telegram**: push deep-links to
`/work/{case_id}`, Telegram is **notification-only by design** (no approve affordance — approving
spends real money, so the decision stays on the authenticated Web UI). AUTO resumes notify too,
framed as already-done. Both channels are best-effort and isolated: a dead bot never undoes a
proposal already on the ledger. 190 targeted tests green (`test_case_quota_resume` 50,
`test_quota_window_prewarmer` 21, push 26, coordinator, control API).
Still open from A78: the 429 is not yet written into the quota store as an event-derived snapshot.

**2026-08-17 — Quota-paused Cases now pause, propose, and resume on purpose (PR #97).**
A Manager turn killed by the account's quota window used to leave no durable trace, and the harness
had exactly ONE resume trigger: a satisfied wait-group. So a quota-killed Case either stalled
silently (no workers in flight) or came back at an unrelated random moment — and since there was no
operator-triggered continuation, an operator who started their own session ended up with TWO
Managers once the engine caught up. Now: a quota death writes `flow.quota_paused` on the Case
ledger (Manager sessions only; survives restart); the Wake-Dispatcher checks that BEFORE
satisfaction, so a paused Case is never woken into another refused turn; restore is a telemetry
fact (`quota_window_state` reads `limit_reached`/`used_percent`/`reset_at`, falling back to the
`resetsAt` the provider attaches to its own 429 when no observer is running); and restore does not
spend — it raises a `case_resume` approval carrying a cost estimate, because resuming a fat Manager
re-writes its whole prompt cache (200–300k tokens observed). `CASE_QUOTA_RESUME_AUTO` (default OFF,
+ `CASE_QUOTA_RESUME_AUTO_MAX_USD` env ceiling) skips the ask. Both resume modes stay on the SAME
Case — `in_place` (one turn into the live session) and `fresh_manager` (a Manager rebuilt from the
Case brief: the cheap path, and the only one when the session is dead) — never a fork. Auto,
approval and the new operator button (`POST /api/cases/{id}/resume`, Case detail panel + Work-screen
prompt) all funnel through ONE leased `resume_case`, which is what structurally kills the
two-Managers-on-one-Case failure. Also corrected: a quota turn keeps reporting FAILED (the previous
branch state flipped it to success via the salvage path) while still leaving the session
AWAITING_INPUT; 429 / `rate_limit_event` / limit wording all classify as ONE `usage_limit` class
with 0 immediate retries (a 5-hour window will not clear in 2 seconds); and
`QuotaWindowStore.status()`'s un-indexed correlated subquery — **>120 s** on this host at 53k
snapshot rows — is now 0.11 s. 262 targeted backend tests + 130 web tests green; full flow
rehearsed against COPIES of the live mesh/quota DBs (real Case rows, real telemetry, zero paid
calls). Remaining telemetry debt is dispatched as **A78**.

**2026-08-14 — SDK stream-json poisoning killed sessions on Horse (PR #92, live).**
`"Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"` — 8 incidents on
the Windows node since 2026-08-13, each killing the whole persistent session (Manager `a0d17eb4100f`
among them). NOT an oversized message: across 215 CLI transcripts / 7 days on that box the largest line
is 149 KB. The SDK transport only skips non-JSON stdout when its buffer is EMPTY, so one unparseable
frame mid-message makes the buffer un-parseable forever, swallowing every later message until the 1 MB
ceiling raises out of `receive_messages()`. `12bbed2` (classify) and `adc7610`/#91 (salvage) only made
that failure prettier. Fix: resyncing stdout reader (drop the provably-garbage prefix when the current
line parses standalone), `max_buffer_size` 16 MB, CLI stderr captured, and — separate real defect —
respawn after a dead stream now RESUMES the backend conversation instead of booting empty (a Manager
silently lost its whole Case memory). Deployed: gateway + Horse worker restarted on `9e197c9`,
`event=sdk_stream_resync_installed` confirmed in Horse's live log. **Open:** the poison bytes themselves
are still unidentified — the next occurrence logs them as `event=sdk_stream_poison` (head+tail repr).
Check for that event before theorising further. Falsified already: hook-stdout inheritance (probe hook
proved the CLI captures hook stdout out-of-band) and a CLI regression (workers run the SDK-*bundled*
claude 2.1.191 from June, not the box's 2.1.231 — `CLAUDE_SDK_CLI_PATH` now exists to change that).

**2026-08-10 — Stale open Case cleanup path + live cleanup.**
Root cause for "open Cases with no active sessions": Case lifecycle is intentionally separate from
session lifecycle. `SessionService.close_session()` closes only the runtime session; `close_case()` is
criteria-gated and is not called automatically, and the Wake-Dispatcher only reacts to satisfied wait
groups. Result: a Case can remain `flow_runs.status=NULL` after its Manager session is closed/error/
missing. Cleaned 10 live orphan candidates through the existing `/api/cases/{id}/interrupt` path
(`reason=manager_session_unavailable`), which marked them `blocked` and cancelled 0 in-flight workers;
the post-cleanup orphan query was empty. Added `TaskOrchestrator.sweep_orphaned_cases()` plus authenticated
`POST /api/cases/orphans/sweep` (`dry_run` first, bounded `limit<=500`) and a Work-screen maintenance
panel that scans before enabling "Block stale". This is deliberately manual/operator-driven, not an
automatic background closer; cleanup blocks/resumes, never marks work done. Verified: targeted backend
tests 43 passed, web typecheck clean, web production build clean.

**2026-08-11 — Stale cleanup regression fixed: active/recovered Manager Cases must not be blocked.**
The 2026-08-10 live cleanup over-blocked two Cases linked to Manager session `3fd71c35a853`: the
session was later `AWAITING_INPUT` and affiliated to Case `9f2893...`, but both linked Cases stayed
`blocked`, so `_continue_case_once` correctly short-circuited before wait-group satisfaction and the
Manager could not be woken. Restored `9f2893...` and `9ad6...` to open with `flow.unblocked` audit
events. Patch: `manager_session_unavailable` is now a guarded cleanup reason; `interrupt_case` refuses
it when the Manager session exists and is not terminal, and sweeps no longer treat `ERROR` as enough to
block because it can be stale/recoverable. Added operator Case state control:
`POST /api/cases/{id}/state` (`open`/`blocked`) plus Work detail `Block`/`Unblock`, so Case state is no
longer read-only. Verified: targeted backend tests 49 passed, web typecheck clean, web production build
clean.

**2026-08-07 — Typed error classification instead of backend_error catch-all (PR #81, merged, gateway restarted).**
Follow-up to PR #80. The Claude Agent SDK's terminal `ResultMessage` carries structured `subtype`
(e.g. `error_max_turns`) and `api_error_status` (HTTP status of the underlying API call — 429/5xx)
fields that were read but only ever used as free-text fallback for keyword matching — anything not
matching "rate limit"/"context window" wording collapsed into the generic `backend_error`/`fatal`
bucket with 0 retries, even when the SDK told us exactly what happened. `classify_error_text()`
(driver) and `_classify_error()` (orchestrator) now check the structured fields first: `error_max_turns`
→ new `max_turns` class (0 retries, points at `CLAUDE_SDK_MAX_TURNS`); `api_error_status==429` →
reuses `rate_limit`; `api_error_status>=500` → new `upstream_error` class (reuses `network`'s retry
numbers, own accurate suggest_actions text). Deliberately does NOT touch
`_is_salvaged_backend_finalization_error` (PR #80's session-recovery mechanism) — verified it only
ever matched the generic banner + literal `error_during_execution` marker, so this is orthogonal.
110/110 targeted tests pass (5 new). Gateway restarted on merged code; worker untouched.

**2026-08-07 — Salvaged-turn "false failed" badge + truncated reply fixed (PR #80, merged, gateway restarted).**
Root cause: a turn whose SDK terminal wrap-up errors out AFTER the agent produced a real, complete
reply (context overflow / usage limit / backend_error — the `SALVAGE_ERROR_BANNER` case) already put
the *session* in the correct `AWAITING_INPUT` state, but `TaskResult.success` stayed `False`. Every
other consumer (task status, turn telemetry, the `mesh_result` SSE event → frontend red "failed"
badge, `mesh_tasks.status` read by `task_state_truth`) kept surfacing it as failed even though nothing
about the outcome actually failed — plus `session.last_summary`/`task_history` showed only the terse
one-line failure reason instead of the salvaged full reply. Fix: `_reclassify_salvaged_turn_success()`
flips `success=True` on the FINAL result only (after retry-eligibility, so genuine `rate_limit`/
`usage_limit` turns without salvaged work still retry normally); `last_summary`/`task_history` now
prefer the salvaged text, mirroring `_mesh_complete_task`'s existing precedence. 26/26 targeted tests
pass (2 new). Gateway restarted on merged code; worker untouched.

**2026-08-05 — A67 follow-ups dispatched as four small jobs (A72–A75).**
Operator chose "several jobs / small PRs, gradually" over folding everything into A71. Dispatched:
A72 input caps (P2-3), A73 node `.env` deploy guard (P1-4 node-side), A74 proactive-turn ownership
(P2-6), A75 token-out-of-HTML (P2-4, sequenced after A71 design). A71 stays the per-node credential
migration. Executing A72 then A73 next (both provably non-breaking).

**2026-08-05 — Mesh security review shipped (PR #72, `AGENT_67`).**
Adversarial review of the mesh surfaces completed: private findings in `.security/` (git-ignored —
never commit/publish), public threat-model in `docs/MESH_SECURITY.md` (hcom-structured). A **P0 was
verified and fixed live**: the task-server `/files` staging upload used the client filename verbatim
as a path segment, so a `../../` name escaped the staging root (arbitrary file write on the gateway
host). PR #72 adds sanitize + containment; gateway restarted post-merge; worker untouched. `.env`
and `state/mesh.db` chmod `0600`. **Escalated to operator** (R2, not silently patched): per-node
credentials replacing the single shared `WORKER_TOKEN` (self-reported node identity), claim/result
identity binding, server-side dispatch bounds + rate limits, dashboard token out of served HTML.
Until the credential model lands, treat `WORKER_TOKEN`/`DASHBOARD_TOKEN` as full-admin — see
`docs/MESH_SECURITY.md`.

**2026-08-05 — Close-session race gate merged (PR #70, `AGENT_70`).**
The close-vs-turn race behind `task_ed5283f1` is fixed at the root: the worker now defers a
`close_session` control task until the session's in-flight turn posts its real outcome
(`_inflight_sessions` tracking in `src/worker/agent.py`). PR #68/#69 already fixed the aftermath
(full payload shipped, salvaged session status). **Worker-side code — lands only on the next
worker redeploy (Horse); gateway side unaffected, no restart needed.**

**2026-08-04 — Upload endpoint boundary deferral.**
`POST /api/sessions/{id}/upload` is an external-input path. It now routes remote mesh uploads through
worker staging instead of gateway-local path writes, but the pre-existing request-size/timeout posture
remains: upload bodies are read into memory and bounded only when `GATEWAY_UPLOAD_MAX_MB` is set; there
is no upload-specific timeout/semaphore yet. Size this before raising upload volume or allowing broad
untrusted use.

**2026-08-04 — A65 cost monitoring complete (PR #62).**
The manager-vs-workers cost job is live end-to-end: P0 truthfulness audit
(`docs/cost_monitoring_audit.md`), P1 cost read-model (`ac5aea2`, PR #61 — codex `includes_cache`
accounting fixed, `_PRICE_TABLE` extended for codex/gpt, `/api/cost/explorer|top|projects` +
`/api/cases/{id}/usage`, six-case report reproduced via API), P2 Cost tab (`1f04be5` — 24h/48h/7d/30d
range defaulting 7d + per-project filter, spend by project/model with honest coverage %, top-N by
USD, per-case manager-vs-workers drilldown). P3 (`8792f9f`, PR #62) adds the authenticated
`/api/cost/alerts` read surface and Cost-tab alert banner: daily/session/Case thresholds are
known billable USD only; alerting activates when a positive `COST_ALERT_*_USD` knob is configured;
the separate enforcement flag remains OFF and only surfaces the existing SDK governor seam. Targeted
Python tests and the full web test suite pass.

**2026-08-01 — Wake-dispatcher IDLE-gate bug fixed and proven live (PRs #51/#52/#53).**
A Manager on worker-node armed a wait-group; workers finished but the Manager was never woken despite
`CASE_CONTINUATION_ENABLED=1`. Root cause: `_continue_case_once` required strictly `IDLE` but
a Manager that ran a turn settles in `AWAITING_INPUT`. Fix: accept `AWAITING_INPUT` as the wake
target (PR #51). PR #52 refined: `IDLE` is explicitly NOT a wake condition (freshly-created,
never-ran session cannot own a satisfied group). PR #53 fixed a silent case-identity split where
a satisfied group resolved to a dead/closed manager session — now escalates with
`case.manager_unavailable` instead of returning 0 silently. Proven live on the original failing
case (<case-id>). Gateway restarted post-merge; worker/worker-node untouched.

**2026-07-31 — A61 quota coordinator commit landed directly on `main` (`cbbaa10`).**
Bypassed branch+PR policy. A63 is the independent audit job. Do not treat A61 as reviewed until
A63 closes.

**2026-07-30 — M3.4 Job 1 (arm_wait_group default) + M3.3 governor/kill path merged (PRs #49/#50).**
Manager role default is now `arm_wait_group` + return control. `sdk_max_turns`/`sdk_max_budget_usd`
knobs wired. `interrupt_case` kill path added. Both flag-gated OFF → byte-identical until activated.
Two adversarial review rounds each; round 1 on A53 caught a real cross-layer inert-kill bug (worker
role filter was wrong). PR #47 (quota coordinator salvage) also merged same session behind
`QUOTA_COORDINATOR_ENABLED` (default OFF). ⚠️ PR #47 is scaffolding only — adapters are
`Unsupported` placeholders; activation is blocked on A61/A63.

**2026-07-28 — Worker model-selection contract + turn-surfacing resilience merged.**
`dispatch_worker` now requires an explicit `model` arg (no silent expensive default). Usage-cap
turns classified as retry-eligible `rate_limit` (not `fatal`). `wait_for_worker` timeout capped
at 600 s (was 3600). Truncated salvaged replies now deliver full text. PR #44 merged + gateway
restarted.

**Live `.env` note:** `CLAUDE_DEFAULT_MODEL=opus` in the live `.env` overrides the catalog
(code now defaults sonnet). Whether to change this is an operator decision.

---

## What this project is

A gateway for local coding agents (Claude Code, Codex, OpenCode CLI, OpenCode server), controlled
from a Web UI or Telegram. Sessions open from either surface; follow-up messages route to that
session; each turn resumes the native backend session. State is DB-canonical with a file-backed
fallback.

**Not** a generic autonomous-agent framework. No opaque memory, no broad self-directed execution,
no PTY-persistence backbone. See `context/production_vision.md` for the strategic frame and
anti-goals.

Two surfaces over one gateway process:
- **Web UI** (`web/`, React 19 + Vite + Tailwind v4) — primary UI, mobile web app served
  in-process at `/` + `/api/*`.
- **Telegram** — secondary command surface over the same backend.

Manager invocation: `POST /api/manager` → role-boot → `open_case` → dispatch workers → review →
`close_case`. See `docs/harness/roles/manager.md` and `docs/M3_MANAGER_INVOCATION_SPEC.md`.

**Do NOT run `python main.py status`** — it acquires the gateway lock and kills the live PM2
process. Check liveness with `curl http://127.0.0.1:9003/health`.

**TEST COST GUARD:** tests can invoke the paid Claude CLI. Run `pytest` on touched modules only.
Never run the full e2e suite. Real e2e is opt-in only:
`AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`.

---

## Architecture — as it runs today

**One process** (`ai-team-gateway`, PM2). When `MESH_ENABLED=true` it also hosts the task server
embedded on its own event loop.

```
[Web UI] / [Telegram] → [Gateway process]
  ├── src/telegram/interface.py         secondary command surface
  ├── src/orchestrator.py               task queue, in-process workers, routing, recovery
  ├── src/core/session_service.py       transport-neutral session lifecycle — M1 inbound seam
  ├── src/services/session_store.py     DB-first reads, dual-write to JSON + DB
  ├── src/control/db.py                 SQLite mesh DB (WAL, busy_timeout=5000, migrations)
  ├── src/control/embedded_server.py    task server, embedded (mesh on)
  ├── src/control/{task_server,node_registry}.py  HTTP API + node registry
  ├── src/worker/agent.py               worker daemon — own process on worker nodes (e.g. worker-node)
  └── src/backends/                     claude_code, codex, opencode, opencode-server
```

**Mesh (live):** gateway + embedded task server on the gateway host; worker daemon on a separate worker node. `MESH_ENABLED=false` → gateway is byte-for-byte the old behavior.

**State layout:**
```
state/sessions/<id>.json              session records (dual-written, NEVER deleted)
state/mesh.db                         SQLite — canonical for conversation + artifacts
results/reconcile/<task_id>.json      DB-reconcile spool; replayed on next startup
logs/session_events/<id>.log          per-session NDJSON
logs/events.ndjson                    system-wide event log
```

**Config flags:** `MESH_ENABLED` (default `false`), `MESH_SHADOW_WRITE` (default `true`),
`WORKER_TOKEN`, `MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT`. Feature flags →
`docs/ENV_FEATURE_FLAGS.md`.

---

## Architecture rules (do not violate)

- DB is the canonical read source. `state/sessions/<id>.json` is the ultimate fallback and is
  **never deleted**. `results/task_*.json` are droppable — `mesh_tasks` holds full conversation
  + artifact data (migration 17).
- **Two task classes, two routing policies:**
  - **Unpinned** (`session.machine_id` empty): may run anywhere.
  - **Pinned** (`session.machine_id = <node>`): host-or-nothing. Never relocate to a substitute
    host — `backend_session_id` is machine-local. Fallback = wait / requeue / operator re-pin.
- `MESH_ENABLED=false` → byte-for-byte old behavior.
- No uncontrolled autonomous behavior. Per-turn audit data (full reply, files changed, usage) is
  mandatory — lives canonically in `mesh_tasks`.

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | runtime, task queue, workers, routing, recovery, mesh hooks |
| `src/core/session_service.py` | transport-neutral session lifecycle — M1 inbound seam |
| `src/core/task_state_truth.py` | honest task/job state read-model |
| `src/backends/registry.py` | backend declaration — M1 |
| `src/control/db.py` | SQLite mesh DB — canonical DB layer |
| `src/control/task_server.py` | FastAPI task server (embedded) |
| `src/worker/agent.py` | worker daemon (own process on worker nodes) |
| `scripts/mcp_manager.py` | Manager MCP tool surface (`dispatch_worker`, `open_case`, etc.) |
| `config/settings.py` | all config incl. `MeshConfig` |
| `docs/ENV_FEATURE_FLAGS.md` | feature-flag reference |
| `docs/CONTROL_CONTRACT.md` | M1 — event + inbound-command + backend + read-model contract |
| `docs/harness/roles/manager.md` | Manager role behavior + dispatch-envelope template |
| `docs/harness/roles/worker.md` | Worker role behavior |
| `docs/Task_Harness_v0.7_AUTOMATION.md` | active harness automation spec (M0–M4) |
| `docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md` | M3.4 design + §10 boundary decision |
| `docs/SPEC_COMPLETION_PLAN.md` | ordered backlog to exhaust v0.7 (T1–T6 + V1–V4) |
| `docs/archive/progress/_archive_PROGRESS_LOG.md` | completed-work history |
| `ecosystem.config.js` | PM2 supervisor config |

---

## Deferred — Web UI / Cockpit track

| # | Task | Notes |
|---|---|---|
| 22 | Token streaming (`message.delta`) | DROP — timeline shows per-turn summary |
| 23 | Diff hunks / file-content preview | no backend source |
| 24 | Terminal / raw stdout-stderr stream | out (security) |
| 25 | Approvals automation | durable gate exists but inert; belongs to a future workflow-automation track |
| 35 | Per-project "Current Focus" panel | reads CONTEXT.md as source of truth; defer until workflow settled |

## Deferred — A80 session cache heartbeat follow-ups

Adversarial review of `feat/session-cache-heartbeat` (2026-08-29) confirmed 9 findings; 8 are now
fixed. Pre-merge (6): wait-group re-arm losing heartbeat coverage after Manager respawn, orphaned
owner rows on stop, `cache_below_threshold` permanently killing a heartbeat instead of retrying,
`notify_agent=false` silently dropping an explicit `cache_heartbeat="on"`, and the
`list_cache_heartbeats` N+1. Post-merge (2, ahead of turning `CACHE_HEARTBEAT_ACTIVE` on for
real): `_finalize_cache_heartbeat`'s 180s poll no longer misclassifies a still-running heartbeat
turn as `heartbeat_failed` — a timeout now closes out the lease without touching controller state
and naturally retries on the next `slot_epoch` (regression test:
`test_finalize_timeout_does_not_stop_the_controller`); and `watch_job`'s `cache_heartbeat="auto"`
now skips arming when `expected_runtime_sec` is known and shorter than
`CACHE_HEARTBEAT_INTERVAL_SEC` (job can't outlive one interval, so a heartbeat is pointless) —
`cache_heartbeat="on"` still always arms explicitly (regression test:
`test_auto_policy_skips_arming_for_jobs_shorter_than_interval`).

Still deferred (1, low severity — edge case, not a blocker for turning `CACHE_HEARTBEAT_ACTIVE`
on at normal scale):
- `_cache_heartbeat_owner_live`'s `case_wait_group` branch (`src/orchestrator.py:1263`) scans
  `list_flow_events` (N+1 per owner, 500-row window) with resolution logic that can diverge from
  `compute_continuation_tick`'s sticky/monotonic resolution on the same event stream — only bites
  Cases with 500+ flow events or a wait group re-armed after resolving. Needs a shared helper.
`ensure_cache_heartbeat_owner`'s bare-except (masks genuine DB errors as "flag off") was reviewed
and left as-is — it fails safe (no heartbeat instead of a crash), and a distinguishable error
surface is cosmetic, not a correctness or cost risk.

## Deferred — DB-contention optimization backlog (2026-09-18) — "on the wall"

Context: PRs #135–147 kept fighting the same slowness (SQLite contention / polling on
one 190MB `mesh.db` shared by control-plane + telemetry). The **proven** acute cause was
the Wake-Dispatcher running synchronous DB per-Case on the event loop — fixed by **#145**
(offload to `asyncio.to_thread`) + **#147** (event-driven skip via `max_flow_event_ids`
watermark + per-tick quota-state cache + reuse of the `list_open_cases` row). These are
the *remaining* structural jobs, ranked by win/effort. Each is a small logic change, not a
rewrite. **Verify against code before starting — do not trust this prose over the tree.**

- **OPT-1 — Telemetry store separation (the recurring root; do NOT ship naively).**
  `llm_events` (~82k rows) / `llm_turns` are append-heavy and live in the SAME `mesh.db`
  as control-plane state, sharing its single `_write_lock`. Parked once already (#140)
  because a **naive file split breaks live JOINs**: `get_session`/`get_job`/
  `recent_cache_write`/`cost_case_rows` read `llm_*` joined to `sessions` on the mesh.db
  connection, and SQLite can't join across files without `ATTACH` — and `ATTACH`
  re-shares the write lock, defeating the split. **Correct order:** (1) rewrite those 4
  readers to app-level joins (two queries + in-memory merge) or move them onto
  `TelemetryStore`; (2) THEN move `llm_*` to `state/telemetry.db` (own connection + own
  write lock); (3) one-time `ATTACH` copy-migrate; (4) repoint `TelemetryStore` + readers.
  Migration-tested PR, not a rushed cutover. This is the last structural coupling.

- **OPT-2 — Server-side pagination for read endpoints (approved).** `/api/sessions`
  (`list_all` → `list_sessions` full-scans ~1594 rows), `list_session_case_links`
  (`/api/work/affiliations`), and `api_session_timeline` all scan and contend (seen in the
  faulthandler dumps). The web `200` limit is **client-side only**. Add server-side
  `limit`/`offset` (default newest ~100) + a `load-more` affordance that fetches the next N
  beyond the first N. Operator works mainly with the latest sessions, so newest-first +
  bounded is the natural read shape.

- **OPT-3 — Case lifecycle: stop the stale/wedged-Case pileup (see shift note below).**
  Approval expiry enforcement + operator-close/interrupt cancels a Case's pending
  approvals + orphan sweep able to *close* (not only *block*) Manager-terminal Cases.

## Deferred — runtime / lower priority

- Backend lifecycle hooks (session-ID detection, PreToolUse security, PostToolUse quality gates) — `docs/TBD/BACKEND_HOOKS_STRATEGY.md`.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
- **M-Mesh** (distributed event bus, shared state store, leader election) — "DO NOT build until the app is operable."
- **ACP / A2A bridges**, **Supervisor agents & workflow engine**, **Transport/role/prompt/tool registries**, **Native mobile** — all deferred from the cockpit spec; no consuming surface yet.
