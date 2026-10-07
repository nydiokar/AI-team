# Worker-restart graceful reconcile — FROZEN expected behavior (A98)

**Status:** frozen 2026-10-07 (A98). Change this file only with the code that changes the behavior.
**Comparator:** `scripts/conformance/worker_restart_conformance.py` embeds this same chain as data and
checks a real incident against it. The script is the machine-readable source of truth; this doc is the
narrative. If they drift, the script wins — update the doc.

> **What this is for.** When a worker restarts while a Manager is waiting on it (the 2026-10-07
> incident shape), a reviewer — human or an automated judge — walks the actual trail against the
> steps below and says, per step: *"this was supposed to happen; this happened instead — is it working
> as expected, or garbage?"* and names the failure point: **flag off / half-built / missing carrier /
> missing action unit / operator-inaction**.

## Scenario

Manager session (`case_role=manager`, `driver_type=sdk`) has armed a wait-group on a worker task.
The worker daemon on node **N** restarts (OOM / crash / deploy). Its **in-memory SDK drivers die**;
the backend conversations survive on disk (`backend_session_id`). The system must detect it, page the
operator **once**, and re-establish the Manager and worker on the **same Case** — never dead-end on
resume-into-a-corpse.

**Preconditions (flags, all live/ON by default):** `HARNESS_FLOW_DRIVE=1`, `CASE_CONTINUATION_ENABLED=1`,
`RESTART_LOST_SESSION_FORK_DISABLED=0` (O1), `RESPAWN_ON_RESTART_ERROR_DISABLED=0` (O2),
`RESTART_NOTIFY_DISABLED=0` (O5), `WORKER_MEMORY_WATCHDOG_DISABLED=0` (O7). Gateway on A98 image;
workers restarted onto A98 code (O7 is worker-side).

## The frozen causal chain

| Step | Trigger / message | Action unit | Effect (DB/state) | Who's paged | Final / continuation |
|---|---|---|---|---|---|
| **0** | Manager armed a wait-group | Manager role loop | `worker.wait_pending` open; Manager `AWAITING_INPUT`/`driver_live`; node N @ incarnation I0 | — | baseline |
| **1** | worker daemon exits (OOM/crash/deploy) | (external) OS/PM2/Docker | in-memory SDK drivers on N destroyed; backend convo persists on disk | **O7**: `event=worker_memory_pressure` on heartbeat *before* death (if memory-driven) | — |
| **2** | worker re-registers, new incarnation I1 | `NodeRegistry.register` → `db.mark_driver_sessions_lost_for_node` | `nodes.incarnation_id I0→I1`; N's idle/awaiting SDK sessions `driver_status live→lost`; claims released | — | `event=driver_sessions_marked_lost`, `event=orphaned_claims_released` |
| **3** | reconcile tick sees I0→I1 | `TaskOrchestrator._detect_node_restarts_once` → `NotificationService.notify_restart` | durable warning + best-effort fanout | **OPERATOR: Web Push + Telegram** "Worker restarted — N, K sessions lost" | `event=node_restart_detected`, `event=node_restart_notification` |
| **4** | wait resolved / wake tick on the lost Manager (`AWAITING_INPUT`+`driver_lost`) | `_continue_case_once` → `_mesh_dispatch_payload` (**action=`create_session`**) + `_maybe_inject_restart_recovery_context` | fresh subprocess (role re-boot + A54 boot-reconcile + `<prior_context>`); `driver_status lost→live`; SAME Case | — | `event=restart_context_injected`; mesh_task `action=create_session` |
| **5** | wake on a satisfied Case whose Manager is **ERROR+`driver_lost`** (fork failed / died at idle) | `_is_restart_dead_session` → `_handle_dead_manager_session` (A55) | auto-respawn OR `case_manager_respawn` approval; NEW Manager on SAME Case via `get_case_brief`; waits re-armed | **OPERATOR: `case_manager_respawn` approval** (if `CASE_RESPAWN_REQUIRES_APPROVAL`, default ON) | flow `case.manager_respawned` (or escalate `case.manager_unavailable`) |
| **6** | live Manager (or pending re-run) dispatches the worker's next turn | same O1 routing for the worker session | worker recovers via `create_session`; committed work NOT redone; wait resolved/re-armed | — | worker `action=create_session` success; `worker.wait_resolved` / `review.*` |
| **7** | — | — | Case open & progressing; Manager live on same Case; worker live; operator paged once (+approval if gated) | — | **conformant final state** |

**Note:** Step 4 is the common path (Manager was waiting). Step 5 is the safety net for when the
Manager landed in ERROR (O1's fork failed, or it died at an idle point with no pending wait, or a
terminal `session_lost` turn errored it first). Steps 4 and 5 are mutually exclusive per wake.

## Failure-point decision tree (pinpoint)

Walk steps top-down; the **first** ❌ is the failure point. Map it to a cause bucket:

- **Step 2 absent** (no mark-lost): **MISSING CARRIER** — worker sent no `incarnation_id` (old worker
  code), OR sessions were BUSY at restart (mark-lost excludes BUSY — known gap), OR the controller
  never saw the re-register.
- **Step 3 absent** (no detect/page): **FLAG** `RESTART_NOTIFY_DISABLED` on, OR reconcile loop not
  running (MESH off / `session_reconcile_interval_sec=0`), OR **HALF-BUILT/not-deployed** (gateway on
  pre-A98 image). Event present but no Push/Telegram ⇒ notifier/bot unconfigured (best-effort seam).
- **Step 4 = `resume_session`→`session_lost`** (no `create_session`): **FLAG**
  `RESTART_LOST_SESSION_FORK_DISABLED` on, OR session turn-queue-enrolled (A82, scoped out), OR
  **not-deployed** (gateway pre-A98). *This is the original incident behavior.*
- **Step 5 absent** (Manager ERROR+lost, no respawn/approval): **FLAG** `CASE_CONTINUATION_ENABLED`
  off or `RESPAWN_ON_RESTART_ERROR_DISABLED` on; approval raised but never actioned ⇒
  **OPERATOR-INACTION** (wedged Case); `event=respawn_failed` ⇒ **MISSING CARRIER** (no placement node
  / worker offline → `case.manager_unavailable`).
- **Step 6 repeated `resume_session`→`session_lost`**: same O1 cause as step 4; repeated cold resumes
  ⇒ **cost-guard regression**.
- **Step 1 OOM with no prior `worker_memory_pressure`**: **O7 off** or worker on old code.

## Reference cases

- **FAIL reference — the 2026-10-07 incident** (node `Horse`, Case `00c48658…`):
  `python scripts/conformance/worker_restart_conformance.py --node Horse --since 2026-10-07T01:40 --until 2026-10-07T08:30`
  → first divergence **step 3** (no O5 page, pre-A98 gateway), then **step 4** (`resume_session`→
  `session_lost` ×2, 0 forks), **step 5** (Manager ERROR+lost, no respawn), **step 6** (worker ×5),
  **step 7** (incident end state). This is the canonical "garbage" trail.
- **PASS reference — O1 A/B drill** (2026-10-07, session `0bb2b40a951c`): with O1 ON the lost session's
  next turn was `create_session`→completed; with O1 OFF it reverted to `resume_session`→`session_lost`
  fatal. The step-4 ✅/❌ pivot is exactly the fork flag.

## Usage

```bash
# Print the frozen contract (this chain, as data):
python scripts/conformance/worker_restart_conformance.py --print-spec

# Judge a real restart (reads live mesh.db read-only + controller logs):
python scripts/conformance/worker_restart_conformance.py --node <N> [--since ISO] [--until ISO] [--json]
```

Exit code `2` = a divergence was found (first failing step named with its pinpoint); `0` = conformant
or scenario not exercised. Read-only; safe against the live DB.
