```yaml
job_id: AGENT_11_MESH_AFFINITY_ROUTING
created_at: "2026-07-03T17:18:44+03:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md             # -> DISPATCH_LOG.md section with the verdict prose
evidence: .ai/dispatch/AGENT_11_MESH_AFFINITY_ROUTING.md                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T13:21:01.724336+00:00"
```

# AGENT 11 — Mesh Affinity Routing: session pin ignored at execution (silent local fallback)

**Dispatch created:** 2026-07-03
**Owner:** build agent (worker-node) in relay-cooperation with the gateway agent (gateway-host).
**Branch to cut:** `fix/mesh-affinity-routing` off `main`
**Theme:** A session created with `machine_id: worker-node` had its turn executed **locally on
gateway-host** instead of being dispatched to worker-node over the mesh. The pin is honored at the
API/session level but dropped at the execution routing decision — a silent local fallback
that violates the "affinity is required, no local fallback" contract.

> **Test cost guard (READ FIRST).** Diagnosis is DB reads + a config check + log grep on
> gateway-host — **NO paid CLI turn** is required to find the root cause. Only the final
> re-validation submits ONE Codex turn (reuse the A10 §T1 procedure). Never loop it, never
> run the full e2e suite, never run `python main.py status` (kills the live gateway).

---

## Evidence (from the A10 §T1 gateway-routed smoke, 2026-07-03)

Submitted through the production control API on gateway-host (`POST :9003/api/sessions` with
`node_id: worker-node`, then `POST :9003/api/instructions`). Turn ran and returned `success`.
Authoritative record — **gateway-host's** `state/mesh.db` (`/home/cifran/dev/AI-team/state/mesh.db`):

- `llm_turns`: `<task-id> | gateway_node_id=gateway-host | execution_node_id=<empty> | success`
- `llm_invocations.node_id = gateway-host`
- F4 close response shows the session **did** carry `"machine_id":"worker-node"`.

So: **pin stored (worker-node), execution happened on gateway-host (local).** `gateway_node_id` and
`execution_node_id` are therefore not distinct → the A10 §T1 gate stays **FAILED / not
passed**. (Note: the worker-node box has its own separate `state/mesh.db` with unrelated test
rows — it is NOT the gateway DB. Only gateway-host's DB is authoritative for this smoke.)

---

## Root-cause hypotheses (code-grounded)

The remote-dispatch decision is `src/orchestrator.py:2241` inside `process_task`:

```python
route_remote = bool(
    config.mesh.enabled
    and session
    and session.machine_id
    and session.machine_id != socket.gethostname()
)
```

`create_session` (`src/services/session_service.py:86`) correctly sets
`s.machine_id = node_id` for `node_id not in ("", "__local__")` and saves. The control API
(`src/control/control_api.py:788`) passes `node_id=body.node_id or "__local__"`. So the
create path is sound and the F4 echo proves the pin persisted **on the session object**.

Execution still went local, so **exactly one** of these was false at submit time on gateway-host:

- **[H1] `config.mesh.enabled` is False in the gateway/control-API process.** `MESH_ENABLED`
  is read per-process from env (`config/settings.py:552`). If the PM2 gateway process on
  gateway-host was started without `MESH_ENABLED=true` in its env, `route_remote` is always False
  and every pinned session silently runs local. (The *worker* and *task-server* being up does
  NOT imply the orchestrator's `config.mesh.enabled` is True — different concern.)
- **[H2] `session.machine_id` was empty when `process_task` re-fetched it.**
  `process_task` re-`get()`s the session from `self.session_store` (`orchestrator.py:2235`).
  If `create_session`'s save wrote `machine_id` to the in-memory/JSON object the API echoed
  but NOT durably to the shared DB row the orchestrator reads, the round-trip loses the pin →
  `route_remote` False. This is the more serious bug (create echoes a pin it didn't persist).
- **[H3] hostname mismatch** (lower likelihood): `socket.gethostname()` on the gateway box
  returns something equal to `session.machine_id`. Not applicable here (machine_id=worker-node,
  gateway=gateway-host), but confirm the gateway hostname is `gateway-host` and not e.g.
  `DESKTOP-3PGTBMF` (that value appears in older `llm_turns` rows and would indicate a
  host-identity inconsistency worth noting).

There is NO `mesh_routing_failed` signal in the evidence, which points at **H1 or H2**
(routing was never *attempted*), not at a routing failure after the attempt.

---

## T1 — Diagnose (gateway-host, no paid turn)

Run on the gateway box, against gateway-host's `state/mesh.db` and gateway logs:

1. **Did the gateway even have mesh enabled?**
   - Confirm the gateway PM2 process env: `MESH_ENABLED=true` present? (`pm2 env <id>` or the
     ecosystem/config it was launched with). Record true/false.
2. **What machine_id did the DB actually store for the smoke session?**
   ```
   sqlite3 state/mesh.db "SELECT id, session_id, machine_id, status, claimed_by FROM mesh_tasks WHERE id='<task-id>'"
   # and the sessions table (name per schema — sessions / mesh_sessions):
   sqlite3 state/mesh.db "SELECT session_id, machine_id FROM <sessions_table> WHERE session_id='<session-id>'"
   ```
   - If `machine_id` is **empty / __local__** here → **H2 confirmed** (persistence gap).
   - If `machine_id='worker-node'` here but it still ran local → **H1 confirmed** (mesh disabled).
3. **Confirm the gateway hostname** (`hostname`) is `gateway-host` (rules out H3 / flags host-identity drift).
4. **Grep the gateway log for the task**: any `mesh_routing_failed`, `route_remote`,
   `_process_task_remote`, dispatch/local lines for `<task-id>`.

Report the four answers back to the build agent. That pins H1 vs H2 with zero cost.

---

## T2 — Fix (build agent, driven by the T1 answer)

- **If H1 (mesh disabled in gateway env):** the code is correct; the fix is
  operational + a guardrail. (a) Ensure the gateway process launches with
  `MESH_ENABLED=true`. (b) Add a **loud startup assertion / log line**: when the control API
  accepts a session with a non-local `node_id` while `config.mesh.enabled` is False, log
  `event=affinity_pin_ignored_mesh_disabled` at WARNING and — decision for operator — either
  reject the create (409, honest) or accept-but-flag. Silent local fallback on an explicit
  pin is the actual defect; make it non-silent.
- **If H2 (persistence gap):** fix `create_session` / the session store so `machine_id` is
  written to the **same DB row** `process_task` re-reads (not only the echoed object). Add a
  test that: create-session with `node_id=X` → re-`get()` from the store → asserts
  `machine_id == X`. This is the real code bug.
- **Either way — close the silent-fallback hole at the routing decision.** When a session
  has a non-local `machine_id` but `route_remote` evaluates False, emit an explicit event
  (`event=affinity_unrouted machine_id=... mesh_enabled=... host=...`) instead of quietly
  running local. The contract is "affinity required, no local fallback" (see
  `_process_task_remote` docstring, `orchestrator.py:2743`) — the decision site must honor it.

**Scope guard:** routing/persistence + observability only. Do NOT touch the telemetry
adapters, the turn schema, or backend code. Keep local (non-pinned) sessions byte-identical.

---

## T3 — Re-validate (the A10 §T1 gate, ONE paid turn)

Re-run the A10 §T1 procedure through `:9003` on gateway-host (create session pinned to worker-node →
one instruction with a fresh sentinel → poll). Gate passes iff, in **gateway-host's** `llm_turns`:
`gateway_node_id` = gateway-host AND `execution_node_id` = worker-node, **non-null and distinct**, and
`llm_invocations.node_id = worker-node`. Privacy scan (fresh sentinel) = 0 hits in `llm_%` tables /
`telemetry_spool` / turn APIs. Close the session. Then — and only then — mark A10 §T1 PASSED
and update `.ai/CONTEXT.md`, `DISPATCH_LOG.md`, and the A10 packet.

---

## Implementation log

### T1 diagnosis (gateway-host, 2026-07-03) — H1 and H2 both RULED OUT; real cause is double-dispatch / affinity not enforced in the LOCAL worker pool

gateway-host `=== A11 T1 DIAG ===` returned:
- `MESH_ENABLED` in gateway proc: **true** → **H1 ruled out.**
- `mesh_tasks` row: `<task-id> | <session-id> | machine_id=worker-node | completed | claimed_by=worker-node`.
- `sessions.machine_id` in DB (session `<session-id>`): **worker-node** → **H2 ruled out** (pin persisted to the canonical DB).
- gateway hostname: `gateway-host` (rules out H3 host-identity drift).
- log: `event=codex_started worker=worker-0` … `node=gateway-host` … `codex_finished status=SUCCESS duration_s=10.78`, `validated valid_llama=True`. No `mesh_routing_failed`.

**Interpretation.** The affinity metadata is written correctly everywhere
(`sessions.machine_id=worker-node`, `mesh_tasks.machine_id=worker-node`, and worker-node even *claimed* the
mesh row → `claimed_by=worker-node`). Yet the **local in-process worker pool on gateway-host
(`worker-0`) also executed the task locally** and finished it in 10.78s. So the turn was
effectively double-dispatched: onto the local `task_queue` (via `_enqueue_task`, `orchestrator.py:1611`,
which queues EVERY task unconditionally) AND onto the mesh pending table.

**Code trace.** `_task_worker` (`orchestrator.py:1930`) pulls from `task_queue`, logs
`codex_started` at 1984, calls `_mesh_enqueue_task` at 1987, then `process_task` at 1990.
`process_task`'s remote gate is `route_remote` (`orchestrator.py:2241`): mesh enabled AND
`session.machine_id` AND `machine_id != socket.gethostname()`. With DB machine_id=worker-node this
should be True and dispatch remote-only — but the local worker demonstrably ran codex, so in
that call `route_remote` evaluated **False**. The remaining variable is the session object
`process_task` re-fetched at 2235: `SessionStore.get()` is DB-first (`session_store.py:64`)
but falls back to the JSON file (`session_store.py:76`); `SessionStore.create()` stamps
`machine_id=socket.gethostname()` (=gateway-host) at `session_store.py:49` and only later does
`session_service.create_session` set it to worker-node and re-save. A race / stale-JSON read there
yields machine_id=gateway-host → `route_remote` False → local execution.

**Fix target (T2):** enforce affinity at the LOCAL worker pool, not only inside
`process_task`. Before `process_task` (or at the top of the local retry loop), re-read the
session's `machine_id` from the **canonical DB** and, if it names a remote node, refuse local
execution — route remote or emit an explicit `event=affinity_unrouted` (never silently run
local). Also fix `SessionStore.create()` so it does not stamp the local hostname when a pin is
about to be applied (accept an optional `machine_id` in `create()` so create+pin is atomic and
the JSON never briefly says gateway-host). Kill the silent fallback so a pinned task can never be
claimed by both the local pool and the remote node.

### T1b (gateway-host) — narrowed the cause

`=== A11 T1b ===`: sessions JSON `machine_id=worker-node`, `mesh_tasks` rows for task = **1**,
remote-dispatch log hits (`route_remote|_process_task_remote|mesh_dispatch|_dispatch_to_node`)
= **0**. So: no JSON/DB disagreement, no duplicate row, and **the remote-dispatch code path
was never entered** — the gateway's own in-process worker pool (`worker-0`) ran the task
locally and `process_task`'s `route_remote` evaluated False despite the DB saying worker-node. The
window is the create-then-pin gap in `SessionStore.create()` (stamps local hostname first)
combined with there being **no affinity guard in the local worker path** — so if the routing
flag is ever False at that call site, a remote-pinned task silently runs on the wrong host.

### T2 — FIX SHIPPED (2026-07-03, on `feat/task-harness` per operator — no new branch)

Two changes, defense-in-depth:

1. **Atomic pin at create (`src/services/session_store.py`).** `create()` now accepts an
   optional `machine_id`; the first written JSON+DB row already names the target node instead
   of transiently naming the local host. `src/services/session_service.py::create_session`
   passes the pin into `create()` (was: create-with-localhost, then set+save). Unpinned
   create still defaults to `socket.gethostname()` — byte-identical for local sessions.

2. **Hard affinity guard at the routing decision (`src/orchestrator.py::process_task`).**
   Computes `_pinned_elsewhere` (session names a node ≠ this host). If the session is pinned
   elsewhere but `route_remote` came out False, it now **refuses to run locally**: logs
   `event=affinity_unrouted` (with machine_id/host/mesh_enabled) and emits the event, then —
   if mesh is enabled — forces the remote path (which fails loudly when the node is offline,
   no local fallback); if mesh is disabled, returns an honest failure instead of executing on
   the wrong machine. The prior silent local-execution path (root cause of the #9 smoke
   failure) is closed.

**Tests (`tests/test_session_service.py`, no CLI):**
- `test_store_create_stamps_pin_atomically` — `create(machine_id="worker-node")` → returned object
  AND immediately-reloaded row both say worker-node, never the local hostname.
- `test_store_create_defaults_to_local_host_when_unpinned` — no pin → legacy default preserved.

**Verification:** `pytest tests/test_session_service.py tests/test_session_service_lifecycle.py
tests/test_control_api.py tests/test_mesh_dispatch_timeout.py -q` → **54 passed**. No paid CLI.
`orchestrator.py` parses clean.

**Deploy note:** the affinity fix lives in the **gateway** process (gateway-host). It requires a
gateway redeploy on gateway-host to take effect before T3 re-validation.

### T3 — re-validate (pending gateway-host redeploy + one paid turn)

Redeploy the gateway on gateway-host, then re-run the A10 §T1 smoke (create Codex session pinned
to worker-node → one instruction with a fresh sentinel → poll). Gate passes iff gateway-host's
`llm_turns` shows `gateway_node_id=gateway-host` AND `execution_node_id=worker-node`, non-null and
**distinct**, with `llm_invocations.node_id=worker-node`. Watch for `event=affinity_unrouted` in
the gateway log — if it appears, the task was still not routed and the node was likely
offline/misregistered (investigate rather than mark passed). On success: mark A10 §T1 PASSED
and update `.ai/CONTEXT.md`, `DISPATCH_LOG.md`, A10 packet.
