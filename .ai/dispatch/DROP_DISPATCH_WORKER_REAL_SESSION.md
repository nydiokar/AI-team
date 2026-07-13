# DROP — Automation sessions must be observable AND survivable (dispatch_worker → real session)

**Raised:** 2026-07-13 (operator, live incident debrief)
**Priority:** HIGH — this is a foundational invariant the operator considered always-true but
which was never actually built. It undermines trust in the whole automation surface.
**Level:** 3 (architectural; touches session lifecycle, mcp_manager, mesh routing, Web UI) —
needs a scoped plan + operator approval before implementation.
**Owner:** unassigned
**Status:** 🟡 **CORE SLICE BUILT 2026-07-13 — PR (feat/observable-worker-sessions).** Finding-1
(workers = real observable sessions) implemented + tested; Finding-2 role-boot half already
fixed by PR #18 (DROP-1). Node-default routing, worker-session close-on-Case-close, and the Web
UI Case→worker linkage are DEFERRED with written traces (see "Resolution" at bottom).

---

## Operator invariant (verbatim intent)

> When there is an automation process, I must **always be able to peek into the session** and
> see what happened — what the manager and the worker talked about, what passed between them —
> **exactly as if it were my own session, but driven by an agent.** If we open a manager
> session we must see it. When the manager opens a worker to do something, that worker is a
> **new session** we can open and read. This was an invariant from the beginning; we just never
> built it correctly.

## Finding 1 — workers are NOT sessions (verified in DB, 2026-07-13)

Manager case `d536af369743475bb2b26ad6c7751962` (the A42/F1 live loop). Its two "workers":

- `task_835909d9`, `task_f076ba59`: **`action=run_oneoff`, `session_id=NULL`.**
- One-shot tasks: prompt in → a single `reply_text` blob out → done.
- **No `sessions` row, no per-turn transcript, nothing to open in the UI.**
- Across the whole DB: **50 sessionless `run_oneoff` worker tasks; 0 worker sessions ever.**
- Only 2 sessions ever carried a `case_role`, both `manager` (`3c05d7cdba3b`, `6cae2407a5ee`).

So the manager↔worker "conversation" that exists is only: the dispatch prompt (manager→worker)
+ one reply (worker→manager) + the manager's `review.*` verdict. There is **no observable
worker session** because `dispatch_worker` (`scripts/mcp_manager.py` → `run_oneoff`) never
creates one. The manager did NOT spawn a rogue OS subprocess — work went through the tracked
task system and is auditable at the *task* level — but it is invisible at the *session* level.

## Finding 2 — automation sessions are bolted to the gateway, so they die on restart

`invoke_manager` (`src/orchestrator.py:2092`) defaults `node_id="__local__"` → the manager
session pins to the **gateway host embedded worker** (`machine_id=kanebra`). Its Claude CLI is a
**child of the gateway process**. Consequences observed 2026-07-13:

- Gateway restarted at **11:26:33 UTC** (clean `pm2` restart; log shows `Loaded environment
  from .env`, no crash trace).
- The restart killed the in-process Claude subprocesses of BOTH the manager session
  (`6cae2407a5ee` → `driver_status=lost`) and an unrelated user session (`60fb97b9c163`) whose
  in-flight turn then raised `CLIConnectionError: Cannot write to terminated process (exit 0)`.
- **The session row still shows `awaiting_input`/open, but the driver is `lost`** — the operator
  correctly flagged this as dishonest: the UI shows an openable session that cannot actually be
  resumed because nothing re-attaches a manager session after a gateway restart.

### ⚠️ VERIFIED 2026-07-13: the manager-role-ON-a-node path is BROKEN (test run, `dfa521bfb2df`)

Invoked `POST /api/manager` with `node_id="kanebra-worker"`. Session pinned correctly
(`machine_id=kanebra-worker`, driver live) — BUT the boot turn came up as a **generic Claude
session**: reply was *"I'm ready to help. What would you like me to work on?"*, citing the plain
CLAUDE.md workflow, NOT the Manager role. It received **no role prompt, no objective/assignment,
and no manager MCP tools** (dispatch_worker/review/close_case). Finished in 9s, 102 output tokens,
went `awaiting_input` without dispatching.

**Conclusion:** the Manager role boot (`_role_boot` in `claude_driver` — role prompt + scoped
tools) and the first-assignment delivery (`render_first_assignment`) happen only in the
**in-gateway SDK driver path** (`node_id=__local__`). When the turn executes on a remote node
worker daemon, none of that wiring is present — you get a bare, role-less, tool-less session.

So node-routing buys survivability but currently **loses the entire manager machinery.** Making
automation sessions run on a node (Finding 2 goal) REQUIRES porting the role-boot + assignment +
MCP-tool wiring onto the node-worker execution path. This is the crux of the drop-2 build, not a
config toggle. The proven live manager path remains in-gateway (`__local__`, A41/A42).

Operator's architectural point (still correct): an automation session should run on a **legit node
worker (its own process, e.g. `Horse`/`kanebra-worker`)**, not on the gateway's embedded worker. A node worker
maintains the subprocess independently of gateway lifecycle, so a gateway restart does not kill
the automation. `node_id="Horse"` is already wired through `/api/manager` →
`create_session(machine_id="Horse")` — but the **manager-role-ON-a-node path has never been run**
(A41/A42 both ran in-gateway). Reachability of the manager MCP tools from a node is unverified.

## Work to scope (Level-3 plan required before building)

1. **`dispatch_worker` opens a real worker session**, not a `run_oneoff` task:
   - Create a `sessions` row with `case_role="worker"`, joined to the manager's Case
     (`flow_links` role `worker`; `membership:worker`, no child Case per the M3 design).
   - The worker turn(s) execute as normal session turns → full transcript, openable in the UI,
     resumable — "as if it were my own session, but driven by the agent."
   - Manager↔worker exchange (assignment, worker replies, manager review) is a readable thread.
2. **Automation sessions run on a node by default**, not the gateway embedded worker:
   - Decide the default `node_id` for `invoke_manager` (prefer an online node over `__local__`).
   - Verify the manager MCP tool surface (`dispatch_worker`, `wait_for_worker`, `record_review`,
     `close_case`) is reachable when the manager session runs on the node (MCP config on the node
     + control-API reachability over tailnet). THIS IS THE KEY UNKNOWN — verify before relying on it.
3. **Honest resume after restart:** a manager/worker session whose driver is `lost` must either
   auto-reattach (if on a node that kept running) or render as "needs re-invoke," never as a
   silently-open session that cannot resume.
4. **Web UI:** surface manager + worker sessions in the Work/Case view, each openable, showing
   the live transcript and their relationship (manager → worker(s) → verdicts).

## Acceptance criteria (for the eventual build)

- [ ] A manager run produces a `sessions` row per worker, `case_role=worker`, case-linked.
- [ ] Operator can open each worker session in the UI and read the full manager↔worker exchange.
- [ ] Automation sessions survive a gateway restart (run on a node) OR degrade honestly.
- [ ] No sessionless `run_oneoff` worker on the manager path.

## Cross-refs

- `scripts/mcp_manager.py` (dispatch_worker → run_oneoff — the thing to change)
- `src/orchestrator.py:2092` `invoke_manager` (node routing default)
- `src/control/control_api.py` `ManagerInvokeBody` (exposes `node_id`)
- `docs/M3_MANAGER_INVOCATION_SPEC.md` (scope this against it)
- CONTEXT.md M3.3 "durable relay" note (`wait_for_worker` is in-process — related fragility)

---

## Resolution (2026-07-13) — core slice + scoped plan

### What was built (PR `feat/observable-worker-sessions`)
**Finding 1 fixed at the smallest correct seam.** `dispatch_worker` (`scripts/mcp_manager.py`)
no longer defaults to a sessionless `run_oneoff`: when it is called without a `session_id` but
with a `cwd`, it first opens a **real worker session** via the existing, tested
`POST /api/sessions` (rooted at `cwd`, optionally pinned to a `node_id`), then submits the
objective INTO that session joined to the Manager's Case (`case_id`). The join stamps
`case_role="worker"` and the session is openable/resumable exactly like an operator's own session.
- Reused the PROVEN join/wait seams — `case_id` → `case_role=worker` membership (no child Case) and
  `wait_for_worker(task_id, flow_run_id=case_id)` off `task.finished` are unchanged (already
  covered by `test_session_based_worker_joins_case_as_worker`). No change to the hot
  `/api/instructions` endpoint or the proven loop's control flow.
- Honest fallback: no `cwd` and no `session_id` ⇒ legacy one-off, and the reply says so (never a
  silent regression). Added `node_id` arg so a worker session can be pinned to a node worker
  (survivability); combined with PR #18 (DROP-1) a node-carried worker now also boots correctly.
- Tests: `tests/test_mcp_manager.py` +4 (opens session when cwd/no-session; reuses given session
  without creating; one-off fallback without cwd; existing payload/lineage tests unchanged). 27 pass.

### Acceptance criteria status
- [x] A manager run produces a `sessions` row per worker (`case_role=worker`, case-joined) — when
      the Manager passes `cwd` (the tool description now instructs it to; falls back honestly otherwise).
- [x] Each worker session is openable/resumable via the existing `/api/sessions/{id}` +
      `/timeline` endpoints (they are ordinary sessions). *A dedicated Case→worker Web UI linkage
      is deferred — see below.*
- [~] Automation sessions survive a gateway restart: role-boot on a node is fixed (PR #18) and
      `node_id` is now dispatchable; making a node the DEFAULT carrier for `invoke_manager`/workers
      is deferred (item below).
- [x] No sessionless `run_oneoff` worker on the manager path — provided `cwd` is supplied.

### Deferred with written trace (Level-3, operator-gated)
1. **Worker-session lifecycle / close (§7 resource boundary — MUST do next).** A worker session now
   PERSISTS (`awaiting_input`, a live pooled SDK process) after its turn, so the operator can read
   it. Nothing closes it yet. In the proven loop workers are sequential (1–2 per Case) so live-session
   count is bounded in practice, but over a long/large Case this accumulates live Claude processes.
   **Follow-up:** `close_case` should dispatch `close_session` to its member worker sessions (reuse
   the existing `action=close_session` path), and/or an idle-timeout reaper for `case_role=worker`
   sessions. Until then, worker sessions are closed manually / by the existing session reaper.
2. **Node-default routing for `invoke_manager` + workers.** `invoke_manager` still defaults
   `node_id="__local__"` (gateway host). Choosing an online node as the default (so a gateway restart
   can't kill automation) needs the MCP-reachability-from-node verification (drop item 2) and a
   routing-policy decision — operator-gated. The plumbing (`node_id` on `/api/manager` and now on
   `dispatch_worker`) is in place.
3. **Web UI Case→worker surfacing.** Worker sessions are openable individually today; rendering the
   manager→worker(s)→verdict relationship in the Work/Case view is a Web-track follow-up.
4. **Live re-validation (paid, operator-supervised).** The next live manager run will exercise
   session-based workers instead of run_oneoff. Bounded + supervised per the cost guardrails.
