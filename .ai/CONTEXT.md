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

## Deferred — runtime / lower priority

- Backend lifecycle hooks (session-ID detection, PreToolUse security, PostToolUse quality gates) — `docs/TBD/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
- **M-Mesh** (distributed event bus, shared state store, leader election) — "DO NOT build until the app is operable."
- **ACP / A2A bridges**, **Supervisor agents & workflow engine**, **Transport/role/prompt/tool registries**, **Native mobile** — all deferred from the cockpit spec; no consuming surface yet.
