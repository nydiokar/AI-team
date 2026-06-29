# Frontend ↔ Backend Gap Analysis

Status: v1 — reconciliation of the frontend UI spec against backend reality
Date: 2026-06-22
Owner: Nyd
Frontend spec: `.ai/context/mobile_coding_gateway_product_ui_spec_v0.2.md` (v0.2)
Backend spec: `docs/COCKPIT_REFACTOR_SPEC.md` (v3, M1 done)
Verified against: `src/core/interfaces.py`, `src/core/observability.py`,
  `src/control/dashboard.py`, `src/control/db.py`, `src/services/workflow_service.py`

---

## 0. Why this document exists

The frontend spec (v0.2) was written **after** the backend refactor spec, and the
two were never reconciled. The UI spec is a strong product north star, but it
assumes a data model the gateway **does not currently produce** — and in several
cases assumes exactly the things the backend spec **deliberately rejected** as
speculative (Task/Run/Review/Handoff domain tables; see `COCKPIT_REFACTOR_SPEC.md`
§1.3, §9 — "Rejected: new Task/Run/Review/Handoff tables").

The UI spec's own framing was *optimistic / realistic with no pessimistic lane*.
This doc is the pessimistic lane: for every UI concept, **what exists, what is
partial, what is missing, and who has to build it.** It is the bridge that makes
the UI spec buildable without writing the frontend against fiction.

**Rule going forward:** a frontend phase may only depend on a backend capability
marked ✅ PRESENT here, or one with a committed backend move ID. Anything depending
on a ❌ MISSING capability needs a backend move scheduled *first*.

---

## 1. Legend

| Mark | Meaning |
|---|---|
| ✅ PRESENT | Backend emits/exposes this today; frontend can bind now |
| 🟡 PARTIAL | Substrate exists but shape is wrong, write-only, or has no consumer |
| ❌ MISSING | No backend source, but the capability is wanted; requires a backend build |
| ⛔ DROP | Absent **by design** — fights the architecture or earns nothing at this scale. **Remove from the UI spec**, don't build it. |
| 🔵 MOCK-OK | Frontend may build against a fixture now; backend follows in a later phase |

**"Missing" vs "missing":** `❌ MISSING` = a real hole we intend to fill. `⛔ DROP` =
the backend doesn't have it *for a reason* (a CLI turn is atomic; a session is one
chat; we run tens of sessions, not millions). Treating a ⛔ as a backend gap would
have us build machinery to serve a UI concept that shouldn't exist. These get cut
from the frontend spec, not added to the backend.

---

## 2. Domain objects (UI spec §3) vs backend model

| UI object | UI spec | Backend reality | Source | Status |
|---|---|---|---|---|
| **Target** (machine) | §3.1 — online/degraded/offline/unknown | `nodes` table + `node_registry`; `/api/nodes` with derived `live` flag | `db.list_nodes`, `dashboard._annotate_node_liveness` | ✅ PRESENT (states differ: backend has `live` bool + heartbeat age, not 4-state enum) |
| **Workspace** | §3.2 — repo/dir scope on a target | `session.repo_path` (string only) | `interfaces.Session` | 🟡 PARTIAL (a path, not a first-class object; no browse/enumerate) |
| **Session** | §3.3 — open/closed/archived lifecycle + 6 operational states | `SessionStatus`: idle/busy/awaiting_input/error/cancelled/closed | `interfaces.SessionStatus` | 🟡 PARTIAL — see §3 below; **no `archived`, no `running` vs `waiting_for_approval` split** |
| **Task** | §3.4 — long-lived, 9-state lifecycle, child of session | `Task` = one request/response unit; `TaskStatus`: pending/processing/completed/failed | `interfaces.Task`, `TaskStatus` | ❌ MISSING the lifecycle model — see §4 below. **This is the single biggest gap.** |
| **Tool execution** | §3.5 — concrete backend op, nested under task | Not modeled. A backend turn is atomic to the gateway; it returns a result + `raw_stdout`, not a stream of instrumented tool calls | — | ⛔ DROP — instrumenting every internal CLI tool call fights the black-box backend boundary. The agent's own UI (Claude Code/Codex) owns tool granularity, not this gateway. |
| **Approval** | §3.6 — first-class decision object w/ risk, reversibility, expiry | `approval.requested/granted` events emitted, **no object, no consumer, no queue** | `workflow_service.py` (M4, write-only) | 🟡 PARTIAL — see §5 below |
| **Artifact** | §3.7 — durable output (patch/diff/report) | `results/<task_id>.json` written; `last_artifact_path` on session; `artifacts_written` event | `orchestrator.py:1378` | 🟡 PARTIAL (artifacts exist on disk; no listing API, no typed artifact object) |

---

## 3. Session state model (UI spec §3.3) — the conflation gap

UI spec requires lifecycle (`open/closed/archived`) **separate from** operational
state (`idle/running/waiting_for_input/waiting_for_approval/failed_attention/
connection_unknown`). Acceptance criterion #4: "lifecycle and operational state are
not conflated."

Backend has **one** flat `SessionStatus` enum that mixes both:

| UI needs | Backend `SessionStatus` | Gap |
|---|---|---|
| lifecycle `open` | (implicit: any non-closed) | derivable — `SessionView.is_active` already does this ✅ |
| lifecycle `closed` | `CLOSED` | ✅ |
| lifecycle `archived` | — | ❌ no archive concept |
| op `idle` | `IDLE` | ✅ |
| op `running` | `BUSY` | ✅ (rename in adapter) |
| op `waiting_for_input` | `AWAITING_INPUT` | ✅ (`needs_input` already derived) |
| op `waiting_for_approval` | — | ❌ (collapsed into nothing; approvals not wired) |
| op `failed_attention` | `ERROR` | 🟡 (no "needs review" distinction) |
| op `connection_unknown` | — | ⛔ DROP — connection liveness is a property of the **node/target** (heartbeat), not a session. A session doesn't independently lose its connection; show node `live`/`offline` at the target, not a per-session state. |

**Two of these are ⛔ DROP, not gaps:**
- **`archived`** — `closed` already means "ended, out of the working list, still
  readable/resumable." Telegram already hides bulk-closed sessions
  (`[[telegram-ux-human-friendly]]`). Archive is a second hiding tier that buys
  nothing at tens-of-sessions scale; it just adds a state + transition + UI.
- **`connection_unknown`** — see row above; belongs to the target, not the session.

**Verdict:** the lifecycle/operational split is a thin adapter on `SessionView`
(`view_models.py`) — it already carries `is_active` + `needs_input`. The only
*real* additions are `waiting_for_approval` (gated on approvals being wired, §5)
and a `failed_attention` vs plain-`error` distinction. `archived` and
`connection_unknown` are removed from the UI spec.

---

## 4. Task model (UI spec §3.4, §7.4, §7.5) — the biggest gap

This is where the two specs **directly contradict**.

- **UI spec** makes Task a first-class, long-lived, supervised object: 9-state
  lifecycle, parent→children tree, global Tasks inbox (§2.5), task detail route,
  progress, per-task deep links. Phases 2–3 of the UI depend on it.
- **Backend spec explicitly REJECTED this**: `COCKPIT_REFACTOR_SPEC.md` §1.3 and
  §9 — "new Task/Run/Review/Handoff tables: REJECT now — no current pain;
  `Task`+`Session`+events already cover live needs. Speculative schema."

Today's `Task` is a one-shot: `pending → processing → completed/failed`. The
session keeps only `last_task_id` + a thin `task_history` list of
`{task_id, timestamp, success, execution_time}`. The dashboard's `/api/tasks`
returns flat rows (`task_id/status/session_id`), **not** a tree.

| UI Task capability | Backend today | Status |
|---|---|---|
| 9-state lifecycle (queued/dispatching/…/cancelled/connection_unknown) | 4 states | ❌ MISSING 5 states |
| task is child of session (1:N tree) | session → `last_task_id` + flat history | 🟡 PARTIAL (relationship exists, not as live objects) |
| global cross-session Tasks inbox (§7.4) | `db.list_tasks(limit)` flat list | 🟡 PARTIAL (queryable, but no attention/running/queued sectioning) |
| task detail route w/ **child tool executions** (§7.5) | no child executions exist | ⛔ DROP the child-tool-execution part (see §2 Tool execution). Task detail w/o the tool tree (objective/state/times/artifact/logs) is still 🟡 PARTIAL-buildable. |
| **task.progress** events (§11.2) | none | ⛔ DROP — a turn is atomic to the gateway; there's no mid-turn progress to emit without re-architecting the backend boundary. Use state transitions + elapsed time instead. |
| per-task deep link to exact event (§6.2) | events carry `task_id`, so linkable | 🟡 PARTIAL (correlation exists; no event→UI anchor) |

**Decision required (operator):** the UI spec's task model is real product value
("what is running/blocked/failed/waiting anywhere?" — §7.4), but the backend spec
rejected the schema for it. These can't both stand. See §8 recommendation.

---

## 5. Approvals (UI spec §3.6, §7.7) — emitted but inert

| Capability | Backend | Status |
|---|---|---|
| approval event vocabulary | `EVENT_APPROVAL_REQUESTED/GRANTED` emitted by `WorkflowService` | 🟡 PARTIAL |
| approval object (risk/reversibility/affected files/expiry) | none — just event fields | ❌ MISSING |
| pending-approval queue | none | ❌ MISSING |
| approval **consumer** (anything reacts) | **none** — M4 is write-only ("no consumer reacts yet", `workflow_service.py` header) | ❌ MISSING |
| resolve endpoint (approve/reject over HTTP) | none — dashboard is read-only | ❌ MISSING |

UI Phase 3 (attention workflows) is the most blocked phase. Approvals need: an
object, a queue, a write endpoint, and an execution path that actually *waits* on
the decision. Today nothing pauses on `approval.requested`.

---

## 6. Event stream (UI spec §11.2) vs `events.ndjson`

UI spec defines ~25 dotted canonical `GatewayEvent` types. Backend emits ~25
**snake_case operational** events — and they barely overlap in meaning.

| UI canonical event | Nearest backend event | Status |
|---|---|---|
| `session.created/updated/closed` | (state in DB; no per-event) | 🟡 derive from `/api/sessions` diff |
| `message.created/completed` | none (whole-turn result) | ❌ MISSING |
| `message.delta` (token streaming) | none — CLI backends are blocking/one-shot | ❌ MISSING but **expensive & optional** — real chat-UI value, but net-new and against the blocking backend boundary. Ship whole-message first; streaming is a later, deliberate build, not a Phase-2 assumption. |
| `task.created` | `task_created` (`orchestrator.py:1132`) | ✅ (rename) |
| `task.state_changed` | `task_received`/`timeout`/`cancelled`/`retry` | 🟡 PARTIAL (scattered, not one typed transition) |
| `task.progress` | none | ⛔ DROP — atomic turn, no mid-turn progress (see §4) |
| `tool.requested/started/completed/failed` | none | ⛔ DROP — no tool-level events; black-box backend (see §2) |
| `approval.required/resolved` | `approval.requested/granted` | 🟡 PARTIAL (already dotted! emitted, no consumer) |
| `artifact.created` | `artifacts_written` (`orchestrator.py:1378`) | ✅ (rename) |
| `file.changed` | `TaskResult.files_modified` (not an event) | 🟡 PARTIAL |
| `run.cancelled` | `cancelled` (`:1292`, `:1617`) | ✅ (rename) |
| `connection.state_changed` | node heartbeat (derived) | 🟡 PARTIAL |

Backend events with **no UI home** (operational, keep for System/diagnostics):
`worker_pool_scaled`, `throttled`, `dropped_low_priority`, `mesh_dispatch`,
`mesh_result`, `mesh_routing_failed`, `security_violation`, `heartbeat`,
`task_claimed`, `summarized`, `validated`.

> **The "jobs" are the cheap presence signal.** Token streaming (`message.delta`)
> would show *where the agent is and what it's doing* — but these turn-level
> operational events already do that at coarse grain, for free, with no backend
> re-architecture: `task_received` → `mesh_dispatch` (which node) → `validated` /
> `summarized` / `retry` → `artifacts_written`. Render the meaningful ones as
> `SystemNotice` cards in the timeline and the operator sees live progress without
> streaming. Streaming becomes a *nice-to-have refinement*, not a Phase-2 blocker.

**Verdict:** the canonical event adapter the UI spec demands (§11.1) is **mandatory
and non-trivial** — it is a genuine translation layer (snake→dotted, scatter→typed
transitions), not a pass-through. The big holes are **streaming** (`message.delta`)
and **tool-level events**, neither of which the backend produces.

---

## 7. Transport & write paths (UI spec §9, §2.2)

| Capability | Backend | Status |
|---|---|---|
| read sessions/tasks/nodes | `/api/sessions|tasks|nodes` | ✅ PRESENT |
| live event tail | `/api/events?since=` (poll) | ✅ PRESENT (poll, not push) |
| **WS/SSE push** | none — dashboard polls every 3s | ❌ MISSING (deferred: backend Move F) |
| idempotency keys / command ack states | none | ❌ MISSING |
| send instruction (write) | `orchestrator.submit_instruction` exists in core, **not exposed over HTTP** | 🟡 PARTIAL (service method present; no endpoint) |
| create session (write) | `SessionService.create_session` (M1) exists, **not over HTTP** | 🟡 PARTIAL |
| stop / retry (write) | orchestrator methods exist; no endpoint | 🟡 PARTIAL |
| reconnect reconciliation | DB read-refresh is the path (no replay) | ✅ PRESENT (matches "refresh on gap") |

The dashboard is **read-only by design**. Everything that makes this a *gateway*
(send/stop/retry/approve/upload) needs a write surface. The service-layer methods
mostly exist (M1 gave us `SessionService`); what's missing is the HTTP/WS surface =
backend **Move F**, currently deferred.

---

## 8. What to do next — recommendation

**Do NOT rewrite either spec wholesale.** Instead:

### 8.1 Amend the backend spec (the real correction)
`COCKPIT_REFACTOR_SPEC.md` rejected the task/approval model as "speculative" —
that was correct *when there was no consumer*. **The frontend spec is now that
consumer.** Add a section to the backend spec that re-opens, as scheduled moves:

- **Move G′ (Task lifecycle):** extend `TaskStatus` to the UI's 9 states + make
  task a queryable object with session parentage. (Was rejected as "no pain"; the
  UI is the pain.)
- **Move H (Approval consumer):** an approval object + pending queue + a path that
  *waits* on the decision + resolve endpoint. Turns M4's write-only events live.
- **Move F (Write+WS surface):** promote from "deferred" to scheduled — HTTP write
  endpoints (`submit_instruction`, `SessionService`, stop/retry) + WS/SSE push.
- **Move I (Canonical event adapter):** the snake→dotted translation layer
  (rename + collapse scattered transitions into typed `task.state_changed`). Does
  **not** include `tool.*` or `task.progress` (⛔ dropped) and does **not** require
  `message.delta` (streaming is a separate, optional later build).

### 8.2 Cut the ⛔ DROP concepts from the frontend spec
These are absent by design; remove them so the UI doesn't promise what the
architecture won't serve:
- `archived` session lifecycle → `closed` + hide-bulk-closed already covers it.
- per-session `connection_unknown` → show node liveness at the target instead.
- tool-execution objects + `tool.*` events + `ToolExecutionCard` → black-box backend.
- `task.progress` events + progress bars → use state + elapsed time.
- (soften, don't cut) `message.delta` streaming → mark "optional, post-v1".

### 8.3 Annotate the frontend spec (don't rewrite the rest)
Add a "Backend dependency" line to each surviving UI phase (§14) pointing at the
move IDs above, plus a one-paragraph "Backend reality" note in §11 stating the event
adapter is a real translation layer (rename + collapse), not a pass-through.

### 8.4 Phase ordering that respects the gap
Buildable **now** against ✅/🔵 (no backend block):
- UI Phase 0 (contract) — write canonical TS types, but mark each event/object with
  its status mark from this doc.
- UI Phase 1 (mobile shell, Sessions screen, Nodes→System) — binds to
  `/api/sessions` + `/api/nodes`, both ✅. Timeline + Tasks = 🔵 MOCK-OK.

Blocked until backend moves land:
- UI Phase 2 → needs Move F (write+WS) + Move I (adapter). **Whole-message only**
  — `message.delta` token streaming is explicitly out of Phase 2 (post-v1).
- UI Phase 3 → needs Move H (approvals) + Move G′ (task lifecycle).
- UI Phase 4 (files/artifacts) → needs artifact listing API (extends §6's 🟡).
- UI Phase 5 (logs/terminal/health) → health is ✅; log streaming needs Move F.

---

## 9. One-line summary per UI phase

| UI phase (§14) | Backend blocker | Verdict |
|---|---|---|
| 0 — Domain & contract | none (annotate with this doc) | **GO** |
| 1 — Shell + Sessions + mocked timeline | none (mock tasks/timeline) | **GO** |
| 2 — Real session/task state, live transport | Move F + Move I | **BLOCKED** |
| 3 — Approvals, attention | Move H + Move G′ | **BLOCKED** |
| 4 — Files & artifacts | artifact listing API | **PARTIAL** |
| 5 — Operational depth | health ✅; logs need Move F | **PARTIAL** |
| 6 — Hardening | follows the rest | — |
