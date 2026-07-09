# AI-Team Gateway — Hot Context

**Last Updated:** 2026-07-10
**Active branch:** `main` — Work Control Substrate (M2, A25–A30) merged (`24dff9b`); `HARNESS_FLOW_DRIVE` **ON** in live env. **M3 Phase 3.0 CODE-COMPLETE** (branch `feat/m3-phase30-mcp-manager`, NOT merged; stack A31→A34): A31 `scripts/mcp_manager.py` tool surface; **A32** optional `parent_flow_run_id` on `POST /api/instructions` → child `flow_runs` lineage edge (flag-gated); **A33** `dispatch_worker` sends it + `wait_for_worker` retry tolerance; **A34** `claude_driver` grants the manager tools to a session, **double-gated** (`MANAGER_TOOLS_ENABLED` env + `manager` in `~/.claude.json`; `setup_mcp.py --with-manager`) ⇒ byte-identical until the operator opts in. **97 M3 tests green; zero live-gateway blast (not restarted).** **Remaining = the paid live proof only:** 🟡 **A35** (operator-gated runbook, `dispatch/AGENT_35_LIVE_F4_SPIKE.md`) — register server + flag + restart + spawn a manager session + verify parent→child lineage in `/api/flows` with no slot starvation (live box has `MAX_CONCURRENT_TASKS=4` + `Horse`/`kanebra-worker` online ⇒ low starvation risk). NOT run autonomously (cost scar #1 + PM2 restart + global-config edit). See `dispatch/AGENT_31_*`…`AGENT_35_*`.

> This is the **fast-orientation** doc: what the project is, how it's wired *right
> now*, the current priorities, and the constraints. It is intentionally short.
> - Dispatched-job state → [`.ai/dispatch/DISPATCH_LOG.md`](dispatch/DISPATCH_LOG.md) (the manual state machine).
> - Product intent → [`.ai/context/production_vision.md`](context/production_vision.md).
> - Completed-work history → `docs/archive/progress/_archive_PROGRESS_LOG.md`.

> ⚠️ **TEST COST GUARD — READ BEFORE RUNNING ANYTHING.** Tests can invoke the
> **live, paid Claude CLI** and previously burned millions of tokens. A guard now
> prevents it, but respect the rules:
> - Run tests with plain `pytest` only. Prefer cheap targeted checks (import smoke,
>   direct function calls, `--collect-only`, single skipped-test).
> - **NEVER** run the full e2e suite "to verify." Real e2e is OpenCode-only:
>   `AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`.
> - **Do NOT run `python main.py status`** — it acquires the gateway lock and KILLS
>   the live PM2 gateway. Check the running gateway with
>   `curl http://127.0.0.1:9003/health` (or a tailscale IP).

---

## Current Focus

*What's active right now.* For per-job status see
[`dispatch/DISPATCH_LOG.md`](dispatch/DISPATCH_LOG.md); for forward priorities see the
**Current Priorities** table below; for who-owns-what-doc see [`DOC_MAP.md`](DOC_MAP.md).

> **➡️ FORWARD POINTER (2026-07-08): the harness is now being AUTOMATED, and Work
> Control Substrate is the next dependency.** The active
> roadmap is [`docs/Task_Harness_v0.6_AUTOMATION.md`](../docs/Task_Harness_v0.6_AUTOMATION.md)
> (operator-authorized build spec). It promotes the proven manual kernel + the A19
> `flow_runs` record into a gateway-driven, queryable state machine (M0 reconcile → M1
> flow-state machine → M2 dispatch lineage → M3 invoked-Manager → M4 spec layer). The
> prototype-era `docs/harness/promotion_ladder.md` is **RETIRED** by it (§0.3) — do not cite
> its "Phase 2 = NO / deferred / drop" verdicts against v0.6 work. A20–A23 shipped M0/M1.
>
> **➡️ STATUS 2026-07-09: M2 (Work Control Substrate, A25–A30) DONE & merged to `main`
> (`24dff9b`).** `HARNESS_FLOW_DRIVE` is **ON in the live environment** (gateway restarted on the
> merged code) so the substrate now populates from real execution. **M3 is the next milestone and
> is UNBLOCKED** — spec + backend-readiness dossier authored at
> [`docs/M3_MANAGER_INVOCATION_SPEC.md`](../docs/M3_MANAGER_INVOCATION_SPEC.md) (F4 spike answered
> on paper: the dispatch/session/approval/read-model primitives + the MCP-tool-to-session pattern
> all exist; M3 = a new `mcp_manager` tool surface + Manager role wiring + loop guardrails, in 4
> reversible flag-gated phases). **Next unblocked build: M3 Phase 3.0 (F4 spike).**

- **v0.6 M0 + M1 SHIPPED on `main` (2026-07-07).** A20 (M0 base reconcile), then A21→A22→A23
  merged in order (`6fdf8f0` → `56e4180` → `d1ea2f7`): `flow_runs` now carries the full §11
  field set + lineage cols (A21, migration 22 — additive/NULLable/version-guarded idempotent),
  `current_stage` is written at each loop transition behind `HARNESS_FLOW_DRIVE` (**default OFF
  ⇒ byte-identical A19; SHADOW only — nothing reads stage to drive execution**), and the flow
  record is queryable via read-only `GET /api/flows` + `/api/flows/{id}` (A23, auth-guarded,
  loopback/tailnet, no mutation/public bind). 41/41 M1 tests green in-tree. **M2 Work Control
  Substrate (A25–A30) is now DONE & merged (`24dff9b`)** — `flow_links`, append-only `flow_events`,
  write wiring, read model, read-only mobile Work surface, and honest uncapped session affiliations.
  See [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).
  **Next: M3** (Manager-as-invoked-role) — spec at
  [`docs/M3_MANAGER_INVOCATION_SPEC.md`](../docs/M3_MANAGER_INVOCATION_SPEC.md); start with Phase 3.0
  (F4 spike: prove a gateway-spawned session can dispatch a child worker session via `mcp_manager`).
- **⚠️ M2 has TWO coordinated halves — reconciled 2026-07-08 (was an overlapping-lane risk).**
  M2 dispatch lineage = **(A26a)** the `flow_runs` column wiring — `parent_flow_run_id`/
  `dispatched_by`/`dispatch_file` stamped at the child-dispatch seam behind `HARNESS_FLOW_DRIVE`
  (default OFF ⇒ byte-identical), plus the `_stamp_child_dispatch_lineage` **supplier** +
  `list_child_flow_runs` — **now MERGED to `main` (`24dff9b`)** via the substrate branch, which was
  built byte-identically on top of A26a, 19/19 tests green — **and (A25–A30)** the authoritative
  `flow_links`/`flow_events` substrate, also merged.
  **Authority:** `flow_links(child_flow)` is authoritative; the `flow_runs` column is a convenience
  index. **A26 CONSUMES A26a's stamped edge — it must NOT add a second stamping hook** (avoids the
  milestone's F4 "duplicate ledger"). The old roadmap phrase "M2 is wiring-only" (which spawned the
  duplicate lane) is corrected in `Task_Harness_v0.6_AUTOMATION.md` §F3.
- **⚠️ CONCURRENCY (2026-07-08): A25 is being built by another agent** on
  `feat/work-control-substrate` (migration 23 + `flow_links`/`flow_events`). One worker per branch —
  do NOT open a second substrate lane.
- **⚠️ MERGE ORDER (avoid a double-apply): `feat/work-control-substrate` is built ON TOP of the exact
  A26a code** (byte-identical), i.e. it already contains A26a's `_stamp_child_dispatch_lineage` +
  `list_child_flow_runs`. So merging that branch lands A26a + A25+ together. **Do NOT also merge
  `feat/m2-dispatch-lineage-wiring` separately** — it would double-apply A26a and conflict. That
  standalone branch is now the packet/closure/reference home for A26a only.
- **A24 decomposer generator is deferred (2026-07-08).** It remains valid M4 prompt work, but
  decomposition before durable Work/Case linkage creates more loose artifacts. Resume A24 only
  after the Work Control Substrate can attach decomposed packets/tasks to cases.
- **Task harness is COMPLETE and on `main` (one branch).** A13/A14/A15 all merged
  2026-07-03/04. The loop now has: the `docs/harness/` templates + generators, the
  **config map** (`loop_config_map.md` — the knobs), the **doc-structure contract**
  (`DOC_MAP.md`, lean DISPATCH_LOG, one-dispatch-one-file), the **promotion ladder**
  (`promotion_ladder.md` — evidence-gated v0.4 roadmap; 3 of 6 elements are drop-candidates),
  and the **driver** (`manager_invocation.md` — paste this to fire a loop).
- **Proven on docs (A12/A13/A14/A15); NOT yet on a real code task.** The adversarial-review
  / checkpoint half still wants a real *code* diff to validate — the next loop should run
  on a real feature/fix.
- **How to start a loop:** paste `docs/harness/manager_invocation.md`, fill the spec slot.
- **Branch policy (anti-sprawl, 2026-07-06):** the driver no longer reflexively branches.
  **Docs-only loops commit straight to `main`** (no branch/PR/merge); **code loops** cut
  `feat/<loop>-<slug>` and **open a PR at close** (`gh pr create`) — never a dangling local
  branch. See `manager_invocation.md` "Branch policy" + CLOSE step.
- **Newcomer front door shipped (A18) — MERGED to `main`.** `docs/OVERVIEW.md`: a static
  "you are here" router. v0.4 §2.3 human-orientation need, not the deferred wiki renderer.
- **Branch cleanup done (2026-07-06):** merged A18 + A19 to `main`, deleted them + the stale
  `feat/task-harness` (already fully in `main`).
- **⚠️ OPERATOR FORK 1 — `phase1-quota-window-coordinator` (unmerged remote branch).**
  **State VERIFIED in git 2026-07-07:** it is **9 ahead / 2 behind `main`**, **+1773 / −0
  across 6 files** vs its merge-base (`eec87c4`) — a stale *additive* feature branch, NOT the
  "~293-file-deleting" branch an earlier read reported (the only "deletions" are files `main`
  added while it sat behind). It is **salvageable via rebase-to-current, not destructive.**
  **Recommendation:** rebase onto current `main` to salvage its quota-coordinator commits when
  the quota work is scheduled; until then keep it a **separate fork, never entangled with the
  v0.6 flow-machine (M1) work.** Do NOT merge/rebase/delete it now — operator's call.
  (v0.6 §M0 / §4 F1 carry the same verified state; the A20 DISPATCH_LOG row too.)
- **⚠️ OPERATOR FORK 2 — A17 orphan-code drift (`d1556ad`, `AGENT_17_WIP_MERGE_RECONCILE.md`).**
  The "WIP snapshot before main merge" commit landed the reviewed A16 admission-block scope
  (4 files, verified on main) **plus 9 files of undispatched, unreviewed orphan code** — see
  the "Known drift" bullet below for the four clusters. **Recommendation:** retro-dispatch the
  two clusters that fix real bugs (backend-usage peak-vs-sum — already done as A17b; mesh-fleet
  tz/count) through a Level-3 harness loop, and take an explicit keep-with-tests-vs-revert
  decision on `_ActivityForwarder` (live, zero tests) and the `sonnet→opus` default flip.
  Remediation touches worker/mesh code ⇒ **Level-3 fork needing operator approval** — surfaced
  here, not resolved.
- **Known drift (A17 audit, `d1556ad`):** the "WIP snapshot before main merge" commit landed
  the reviewed A16 admission-block scope (4 files, verified on main) **plus 9 files of
  undispatched, unreviewed orphan code** in 4 clusters — **activity-forwarder** (live
  `_ActivityForwarder` @ `src/worker/agent.py:896`, remote-worker path, zero tests),
  **backend-usage-ext** (Codex peak-vs-sum token fix — untested), **mesh-fleet-count**
  (`_count_fleet_nodes` + tz-aware `_mesh_load_stats` fix in `src/control/db.py` — untested),
  and **models-default** (`config/models.py` flips Claude default `sonnet`→`opus`). Well-formed
  and two clusters fix real bugs, but none went through the harness. Not cleanly shipped → not
  a Shipped-Ledger entry. **Remediation (keep+test / retro-dispatch / revert) is a Level-3
  fork needing operator approval** — see `dispatch/AGENT_17_WIP_MERGE_RECONCILE.md`.

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex,
OpenCode CLI, OpenCode server). You open a session from Telegram (or the Web UI),
follow-up messages route to that session, and each turn resumes the native backend
session. State is DB-canonical with a file-backed fallback. It is **not** a generic
autonomous-agent framework — see `production_vision.md` for the strategic frame and
the anti-goals (no opaque memory, no broad self-directed execution, no PTY-persistence
backbone).

Two surfaces over one gateway process:
1. **Telegram** — the original command surface.
2. **Web UI** (`web/`, React 19 + Vite + Tailwind v4) — a mobile web app served
   in-process by `python main.py` at `/` + `/api/*` on one tailnet-bound port. It
   consumes the M1 control contract; no separate core refactor.

---

## Current Priorities

Ranked. Pull the next unblocked item; a dispatcher agent should turn these into
job packets in `.ai/dispatch/` and log them in `DISPATCH_LOG.md`.

| Rank | Item | Why it matters | State |
|---|---|---|---|
| — | ~~Build Task Harness Workflow Kernel (v1)~~ | Prompt+artifact task-quality loop; addresses the #1 scar (false-success / burned tokens from ungrounded execution). | **merged** (A9H, PR #8) on `main` — see Shipped Ledger + `docs/harness/` |
| — | ~~WebUI-first surfacing of the Level-3 admission block~~ (A9H "Next") | A blocked Level-3 submit must read as "needs approval," not an opaque 500 / stuck session. | **built** (A16) on `feat/harness-block-surface` — awaiting operator merge |

**To run a task through the harness:** start at
[`docs/harness/dispatch_pipeline.md`](../docs/harness/dispatch_pipeline.md)
(pick the level with `docs/harness/level_rubric.md`).

Everything else in the recent dispatch set is **shipped and on `main`**: M1/M2 + M3
observability, Operator Signal (Web Push + Backend Usage), Compact-Context, the
is_error fix. Web Push VAPID setup is **done** (operator, 2026-07-03). See the
Shipped Ledger below and `dispatch/DISPATCH_LOG.md`.

Deferred-but-valid work lives in the two "Deferred" tables at the end of this file.

---

## Shipped Ledger (one line each)

Detailed implementation notes are in git history and the dispatch `*_BUILD_REVIEW.md`
files. This is the "don't rebuild it, it's done" list.

**Mesh / State Separation (on `main`):**
- **State Sep P0–P3** — mesh live across `kanebra` + `Horse`; real two-machine dispatch; gateway-restart reattach delivers real worker results (no fabricated "Task failed").
- **P4 / P4.1 / P4.2** — graceful degradation audited & closed; DB-reconcile spool (`results/reconcile/`) replays failed completions; transition-only `mesh_degraded`/`mesh_restored` events.
- **M5** — `mesh_health_samples` trend ledger (migration 19); `/metrics` + `/api/mesh/health` expose recent trends read-only.
- **T1** auto-deploy (`scripts/auto_deploy.sh`) · **T2** long Telegram output · **T3/T3.1** watched jobs (`jobs` table, `/jobs` API, process-identity probe) · **T4** worker-restart claim reclaim (release + stale-claim reaper).
- **Cockpit M1** — backend `registry.py`, `SessionService` (create/bind), `SessionOrigin` (migration 12), `docs/CONTROL_CONTRACT.md`. Telegram byte-identical.

**Conversation/artifacts DB-canonical (2026-06-30):**
- Migration 17 — `mesh_tasks` holds full untruncated reply + prompt + parsed_output + file_changes + usage. Chat + Files/Info tabs read DB-first; `results/*.json` are fallback/debug only and droppable. Backfill done (`scripts/backfill_conversation_turns.py --verify`). Audit: `docs/CONVERSATION_DATA_FLOW.md` §0.

**Session/task state truth (2026-07-01):**
- `src/core/task_state_truth.py` derives honest state from DB rows + worker live_state + node incarnation + telemetry + stale evidence. `/api/sessions/{id}/timeline` exposes the bounded durable sequence; Session Detail renders stale/unknown/detached/recovered explicitly.

**Web UI ladder (merged from `feat/webui-ui0`, PR #4):** every rung M1 → UI-6 shipped (PWA + service worker + manifest). Plus the operator UX fixes:
- **#34** Stop Task = run outcome, not lifecycle (cancelled sessions stay resumable) · **#36** Tasks page removed, jobs render in Session Detail · **#37** job/task history owned by durable timeline API · **#38** System tab is infra-focused · **#39** honest worker/session state reporting.

**Operator Signal (merged from `feat/operator-signal`, PR #5):**
- **#21** Web Push (migration 20, `push_service.py`, SW handlers; notification fan-out only, NOT approval-gated). **VAPID env configured 2026-07-03 — push is live.** · **#30/#33** Backend Account + Usage Visibility (`backend_usage.py`, `/api/backends/usage`; honesty-first — unknown limits return `null` + reason, never fabricated).

**Task Harness Workflow Kernel v1 (A9H + A12 — MERGED to `main` via PR #8, `fd90a46`):**
- Prompt-and-artifact task-quality loop under `docs/harness/` — templates (packet
  XML, milestone, level rubric, README), DRAFT/REVIEW/CLOSE generators, and the
  `dispatch_pipeline.md` runbook. **Zero new gateway state** (spec §0). **Level-3
  admission gate on the HOT path** (build-review B1 follow-up, Option 3):
  `_harness_level3_allows_autopickup` runs in `orchestrator._enqueue_task` — the
  choke point every ingestion lane shares (Telegram/Web `submit_instruction`,
  `.task.md`, internal). Blocked ⇒ raises `HarnessAdmissionBlocked` (no faked
  task_id / no side effect); control API → 409, Telegram → approval reply.
  Flag-guarded `HARNESS_LEVEL3_GUARD`, OFF by default → byte-identical legacy when
  the field/flag is absent. 35 harness/compact + 51 control-API + 24 telegram tests
  green. Spec `docs/Task_harness_workflow.md` §13 ticked.
- **Harness self-test (A12, `feat/task-harness`)** — ran one real task through the
  §14 loop by hand to prove the operating model works before any Phase-2 build.
  `dispatch_pipeline.md` now carries a two-lane scope banner + a copyable all-7-stage
  worked example (real packet/milestone/F-tags/closure). Friction report verdict:
  **Phase 2 NOT justified** — file/dispatch discipline held; see `AGENT_12_HARNESS_SELFTEST.md`.
- **⚠️ A19 FlowRun record — Phase-2 `flow_runs` shipped under an OPERATOR OVERRIDE —
  MERGED to `main` (`0b6b1ec`, 2026-07-05).** Migration 21 + a
  5-col `flow_runs` table + `create/update/list_flow_runs` + a best-effort orchestrator
  write hook in `_enqueue_task` + `tests/test_flow_runs.py` (39 passed w/ regressions).
  It is a **RECORD, not a stage machine** — nothing reads `current_stage`; existing task
  execution is untouched and it is trivially revertible. **The promotion-ladder Row 1
  trigger was NOT observed** — this is an operator-directed experiment, recorded as such
  in `docs/harness/promotion_ladder.md`. Do NOT treat the table's existence as a tripped
  gate. First real *code* loop driven through the harness (`AGENT_19_FLOW_RUNS_RECORD.md`).
- **A16 admission-block surfacing (built, `feat/harness-block-surface`, awaiting merge)** —
  the Web `/api/instructions` lane now catches `HarnessAdmissionBlocked` → **409**
  (`reason=harness_level3_needs_approval` + human `detail` + `task_id`) instead of an
  opaque 500; `session_service.mark_idle` reverts the optimistically-BUSY session; the
  Composer renders the approval-needed copy, not "tap send to retry". Gate predicate
  untouched; guard OFF ⇒ byte-identical. ZERO new gateway state. See
  `dispatch/AGENT_16_HARNESS_BLOCK_SURFACE.md`. Telegram surfacing + approve-from-UI
  remain out of scope (deferred).

**Compact-Context (merged from `feat/compact-context`, PR #6):**
- **#31/#32** `load_compact_context` wired via opt-in `continues: <task_id>` frontmatter → `process_task` prepends bounded, fence-hardened `<prior_context>` block. No new gateway state. Docs: `docs/Task_harness_workflow.md` §7/§14.

**LLM Turn Observability:**
- **M1/M2** — **SHIPPED** (2026-07-03). Local Codex smoke + controlled mesh smoke passed 2026-07-02; SQLite benchmarks passed (#8). Spec: `docs/LLM_TURN_OBSERVABILITY_SPEC.md`. **#9 gateway-routed mesh smoke — CLOSED (2026-07-03, run from kanebra):** `task_35655be9`, `gateway_node_id=kanebra`, `execution_node_id=Horse`, non-null and distinct; privacy scan zero-hit; no `affinity_unrouted`. Control API (`:9003`) is loopback-only on kanebra — run §T1-style checks from kanebra itself, not from a worker box. Detail in `dispatch/AGENT_10_M3_CLAUDE_TELEMETRY.md` (T1 log) + `DISPATCH_LOG.md`.
- **M3** — Claude stream-json telemetry adapter **merged on `main`** (commit `c168028`, A10) and **verified live** (2026-07-03) on the canonical worker-agent/SDK-driver path (`task_bfe8c90b`, `task_f89edffb`) — real `model.request.usage` + coverage events land in `llm_events` for real Claude turns, not just fixtures. Unit test suite was previously vacuously green (missing fixtures, assertions on a nonexistent `send_batch` method) — fixed, 18/18 now genuinely pass. **M4** (OpenCode) deferred.
- **Fix (merged a3f734b)** — SDK `is_error` result no longer stored as a successful "Prompt is too long" reply; salvaged work + honest failure delivered instead. Memory `claude-iserror-prompt-too-long`.

---

## Architecture — as it runs today

**One process** (`ai-team-gateway`, PM2). When `MESH_ENABLED=true` it also hosts
the task server embedded on its own event loop.

```
[Telegram] / [Web UI] → [Gateway process]
  ├── src/telegram/interface.py     command surface (/status, /nodes, pickers…)
  ├── src/orchestrator.py           task queue, in-process workers, routing, recovery
  ├── src/core/session_service.py   transport-neutral session lifecycle — M1 inbound seam
  ├── src/services/session_store.py DB-first reads, dual-write to JSON + DB
  ├── src/control/db.py             SQLite mesh DB (WAL, busy_timeout=5000, migrations)
  ├── src/control/embedded_server.py task server, embedded (mesh on)
  ├── src/control/{task_server,node_registry}.py  HTTP API + node registry
  ├── src/worker/agent.py           worker daemon — own process on worker nodes (e.g. Horse)
  └── src/backends/                 claude_code, codex, opencode, opencode-server (declared in registry.py — M1)
```

**Control contract (M1):** the inbound/outbound boundary is `docs/CONTROL_CONTRACT.md`
— event envelope + catalog, the **two** inbound entry points
(`SessionService.create_session/bind_active`; `orchestrator.submit_instruction`),
`SessionOrigin`, backend extension via `registry.py`, and the `db.list_*` read model.

**Mesh (LIVE since 2026-06-11):** gateway + embedded task server on **Pi5 (`kanebra`)**;
worker daemon on **`Horse`**. Tasks dispatch machine-to-machine and survive a gateway
restart end-to-end. `MESH_ENABLED=false` ⇒ gateway is byte-for-byte the old behavior.

State layout:
```
state/sessions/<id>.json              session records (legacy-authoritative, dual-written, NEVER deleted)
state/telegram/active_bindings.json   chat_id → session_id
state/summaries/<id>.md               per-session summary
state/mesh.db                         SQLite — read-first; CANONICAL for conversation + artifacts (migration 17); mesh_health_samples (migration 19); push_subscriptions (migration 20)
results/<task_id>.json                task artifact — FALLBACK/debug only (DB-canonical since 2026-06-30); droppable
results/reconcile/<task_id>.json      DB-reconcile spool; replayed on startup / next DB-available completion
results/raw/<task_id>.ndjson.gz       gzipped raw_stdout debug stream (when system.slim_artifacts=on)
logs/session_events/<id>.log          per-session NDJSON
logs/events.ndjson                    system-wide event log
```

**Config flags that matter:** `MESH_ENABLED` (default `false`), `MESH_SHADOW_WRITE`
(default `true`), `WORKER_TOKEN`, `MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT` (9002).

---

## Architecture rules (do not violate)

- DB is the canonical **read** source. `state/sessions/<id>.json` dual-write is the
  ultimate session fallback and is **never deleted**. `results/task_*.json` are NO
  LONGER a source — `mesh_tasks` holds the full conversation + artifact data (migration
  17); the fat files are droppable (see `docs/RUNBOOK_db_self_sufficient.md`).
- The gateway host keeps its **own embedded worker capacity** (configurable pool,
  default ≥1 — **not** capped at 1) that runs tasks when no remote node is available.
  Prefer remote nodes when online. **This applies to UNPINNED work only** — see the
  two-class rule below.
- **Two task classes, two routing policies (A11/A18 — do not conflate):**
  - **Unpinned** (`session.machine_id` empty / `machine_id IS NULL`): may run
    anywhere. Local embedded capacity is a legitimate fallback when no remote node
    is online. *Unchanged.*
  - **Pinned** (`session.machine_id = <node>`): **host-or-nothing.** The turn runs on
    its pinned host or does not run. Fallback for a pinned turn means **wait / requeue /
    operator re-pin — never relocate** to a substitute host (that silently forks the
    conversation; `backend_session_id` is machine-local). The embedded worker pool must
    NEVER claim a pinned task (enforced by the mesh claim filter in `db.py` + a
    defense-in-depth assert at dispatch in `_process_task_remote`).
- Session affinity is a hard correctness requirement: a session pinned to a machine
  must execute on that machine. `backend_session_id` is machine-local.
- **Pinned-node-offline handling (A18):** when a pinned node is offline at dispatch,
  the gateway holds the session in `PAUSED_PINNED_NODE_OFFLINE` and polls liveness for
  up to `MESH_AFFINITY_OFFLINE_GRACE_SEC` (default **0 = disabled** ⇒ legacy immediate
  ERROR). If the node returns within the window the turn dispatches normally; if the
  window expires the session ends in the honest, resumable `PINNED_NODE_OFFLINE` state
  (retry or operator re-pin) — never a bare ERROR, never an off-host run.
- `MESH_ENABLED=false` ⇒ gateway is byte-for-byte the old behavior.
- No uncontrolled autonomous behavior. Ollama is optional/helper-only. Per-turn audit
  data (full reply, files changed, usage) is **mandatory** — it lives canonically in
  `mesh_tasks`.

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | runtime, task queue, workers, routing, recovery, mesh hooks |
| `src/core/session_service.py` | transport-neutral session lifecycle (create/bind) — M1 inbound seam |
| `src/core/task_state_truth.py` | honest task/job state read-model |
| `src/backends/registry.py` | single declaration site for the backend set — M1 |
| `src/services/session_store.py` | DB-first session reads + JSON/DB dual-write |
| `src/control/db.py` | SQLite mesh DB — canonical DB layer |
| `src/control/task_server.py` | FastAPI task server (embedded); `/metrics.history.recent` = M5 health samples |
| `src/worker/agent.py` | worker daemon (own process on worker nodes) |
| `config/settings.py` | all config incl. `MeshConfig` |
| `docs/ENV_FEATURE_FLAGS.md` | feature-flag reference — default-OFF gates that must be enabled separately (incl. M1 `HARNESS_FLOW_DRIVE`) |
| `docs/CONTROL_CONTRACT.md` | **M1** — event + inbound-command + backend + read-model contract |
| `docs/CONVERSATION_DATA_FLOW.md` | conversation+artifact data-flow audit (§0 = DB-canonical, migration 17) |
| `docs/RUNBOOK_db_self_sufficient.md` | backfill `mesh_tasks` + drop fat `results/*.json` |
| `docs/LLM_TURN_OBSERVABILITY_SPEC.md` | turn-observability spec (M1–M4) |
| `docs/Task_harness_workflow.md` | task-quality loop spec (v0.5) — A9H |
| `docs/Task_Harness_v0.6_AUTOMATION.md` | automation roadmap (M0–M4) — the active harness spec |
| `docs/PRIOR_ART_MAX_REUSE.md` | salvage map of the retired MAX orchestrator — idea-only inputs to harness M3/M4 (decomposer prompt + `SubTask` DAG shape); mirror & warning, not a platform |
| `docs/harness/` | task-harness v1: templates, generators, `dispatch_pipeline.md` runbook |
| `docs/RUNBOOKS/PHASE_4_RUNBOOK.md` | VPS cutover runbook (= State Sep end-state) |
| `docs/archive/progress/_archive_PROGRESS_LOG.md` | completed-work history |
| `ecosystem.config.js` | PM2 supervisor config |

---

## Deferred — Web UI / Cockpit track (`docs/DEFERRED.md`)

| # | Task | Notes |
|---|---|---|
| 22 | Token streaming (`message.delta`) | ⛔ DROP — timeline shows per-turn summary |
| 23 | Diff hunks / file-content preview | no backend source |
| 24 | Terminal / raw stdout-stderr stream | out (security) |
| 25 | Approvals automation | durable gate exists but inert; auto-emit belongs to a future workflow-automation track ("needs to be thought out better" — operator 2026-06-24). **Do not extend the H/UI-3 gate.** |
| 35 | Per-project "Current Focus" panel | reads CONTEXT.md as source of truth | DEFER until workflow settled |

## Deferred — runtime / lower priority

- Backend lifecycle hooks (session-ID detection, PreToolUse security, PostToolUse quality gates) — `docs/TBD/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
- **M-Mesh** (distributed event bus, shared state store, leader election) — "DO NOT build until the app is operable."
- **ACP / A2A bridges**, **Supervisor agents & workflow engine**, **Transport/role/prompt/tool registries**, **Native mobile** — all deferred from the cockpit spec; no consuming surface yet.
