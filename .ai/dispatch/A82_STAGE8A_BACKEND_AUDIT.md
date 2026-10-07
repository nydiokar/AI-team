# A82 Stage-8a — Backend structural audit (single-admission-authority)

- **Date:** 2026-10-07
- **Scope:** Read-only structural audit of the MERGED A82 Stage-8a implementation on `main`.
- **HEAD:** `d835d4d` (A82 feature commits `a51b832…`, merge PR #185 `60d5747`, cutover `999f95d` — all present on `main`).
- **Question (operator):** Did Stage-8a route the session turn-queue through ONE canonical
  admission authority, reusing the existing API/admission layer — or did it bolt on a parallel
  pathway / second execution queue / premature session-state mutation?
- **Nature:** Structural confirmation, NOT runtime re-proof (live FIFO proof already in
  `A82_STAGE8A_VALIDATION.md`). No code changed.

> **NOTE on layout drift.** The design packet (`AGENT_82_SESSION_TURN_QUEUE.md`) names
> `control_api.py` / flat `orchestrator.py` line numbers from an earlier tree. The merged code is
> refactored: HTTP routes now live under `src/control/routes/*.py`, shared helpers in
> `src/control/control_api.py`, admission service in `src/control/turn_admission.py`, the canonical
> commit in `src/control/db.py`. Every claim below is grounded in the code actually on `main`, not
> the packet prose (per the reality constraint + invariant #18).

---

## VERDICT: **HONORED** — single admission authority, no parallel pathway, no premature mutation.

The two admission front-doors converge on ONE commit function; acceptance is durable-before-ack and
writes no BUSY/active-task state; `/api/turns` + `useSessionTurns` remain telemetry.

---

## 1. Both admission entry points converge on ONE authority (invariants #1, #11)

**Convergence point: `MeshDB.enqueue_turn` — `src/control/db.py:2868`** (docstring: *"Admit a
MANAGED (protocol-1) turn in ONE bounded transaction"*). This is the single canonical `mesh_tasks`
protocol-1 commit.

The call chain, both entry points:

| Hop | New route (`turn-requests`) | Legacy route (`POST /api/instructions`) |
|---|---|---|
| HTTP handler | `api_create_turn_request` — `routes/turn_requests.py:117` | `api_instructions` — `routes/sessions.py:242` |
| branch | operator → `core._submit_managed_instruction`; agent → `core._submit_agent_instruction` (`turn_requests.py:146/148`) | enrolled → `core._submit_managed_instruction` (`sessions.py:271`) |
| shared submit | `_submit_managed_instruction` `control_api.py:200` / `_submit_agent_instruction` `control_api.py:250` | `_submit_managed_instruction` `control_api.py:200` |
| orchestrator | both call `orchestrator.submit_instruction(…, turn_queue_enrolled=True)` (`control_api.py:210`, `:258`) | same |
| choke point | `submit_instruction` `orchestrator.py:7673` → `_enqueue_task` `orchestrator.py:6206` (*"the choke point every ingestion lane passes through"*) | same |
| managed admit | enrolled ⇒ `_admit_managed_session_turn` `orchestrator.py:11187` (gate at `orchestrator.py:6268`) | same |
| admission service | `admit_turn_async` → `admit_turn` — `turn_admission.py:245/194` | same |
| **canonical commit** | **`db.enqueue_turn` — `turn_admission.py:211` → `db.py:2868`** | same |

`src/control/turn_admission.py` docstring (lines 1-7) is explicit: *"One entry for every producer
that targets an ENROLLED session … around the strict `MeshDB.enqueue_turn` transaction (which itself
enforces idempotency, enrollment, count/byte caps and commit-before-acknowledge)."*

**No second prompt/execution queue for enrolled sessions.** `mesh_tasks` (protocol-1) is the sole
ledger. `list_turn_requests` (`db.py:4659`) and the `GET /api/turn-requests` resources
(`turn_requests.py:219/236`) are READ views over that same ledger — not a second queue. The legacy
in-memory `SessionTaskQueue` is bypassed for enrolled sessions (`orchestrator.py:6265` comment:
*"no BUSY/last_task_id write, no in-memory queue"*) and the two admission sides share ONE fleet
allowance (`SharedWaitingAllowance`, `turn_admission.py` docstring) so they cannot double-admit.

**Invariant #1 — one execution ledger: HONORED.**
**Invariant #11 — turn-requests is the admission route, converges with legacy: HONORED.**

---

## 2. Acceptance does NOT prematurely mutate session state (invariant #5)

The Stage-0 offender (packet ~line 731: old `control_api.py:1789 mark_busy` wrote BUSY + clobbered
the active task id on acceptance) is **gone from the acceptance path**. In the merged tree:

- `_admit_managed_session_turn` docstring (`orchestrator.py:11196`): *"Does NOT touch BUSY /
  last_task_id / last_user_message / native id."* No such write exists anywhere in that function or
  in `_admit_managed_producer_turn`.
- `_enqueue_task` comment (`orchestrator.py:6264-6266`): managed path is *"commit-before-ack; no
  BUSY/last_task_id write, no in-memory queue."*
- **All three surviving `mark_busy` call sites are on NON-enrolled / refused paths:**
  - `sessions.py:288` — the legacy non-enrolled instruction branch, which Stage-8a now refuses
    BEFORE the write via `refuse_unenrolled_session_turn(..., enrolled=False)` (`sessions.py:285`,
    review F2). The enrolled branch (`sessions.py:271`) `return`s before ever reaching it.
  - `control_api.py:1227` and `:1311` — the web-upload path; an enrolled session is refused 422
    `managed_unsupported` by `_store_session_upload` before any write (Stage 4b rework note #4), so
    these are unreachable for enrolled sessions.
- `mark_busy` itself (`services/session_service.py:246`) is a plain BUSY+`last_user_message` write —
  it is simply never invoked on the enrolled accept path.

**Acceptance writes:** only the durable `mesh_tasks` row (queued/pending) via `enqueue_turn`, plus
Case/role lineage via `_write_managed_lineage` (`orchestrator.py:11297`) and post-commit telemetry.
**Activation/start writes** (separate, later): active ownership is read FROM the ledger, never from
session fields — `stop_managed_session_turn` (`orchestrator.py:11844`): *"cancel the turn that OWNS
the active slot, read from the ledger (never `last_task_id`, never a queued id)."*

**Invariant #5 — queued is not BUSY: HONORED.**

---

## 3. Canonical commit precedes acknowledgement (invariant #4)

In `_admit_managed_session_turn` the ordering is strict:

1. `admit_turn_async(db, request, …)` runs FIRST (`orchestrator.py:11284`) — the durable
   `enqueue_turn` transaction. Comment at `:11263`: *"Admit FIRST (no side effect before the
   durable decision): a refused admission leaves no Case lineage."*
2. ONLY after a committed, non-replay admission returns are side-effects emitted:
   `_write_managed_lineage` (`:11297`), then `turn.accepted` / `turn.queued` telemetry
   (`:11304/:11308`), flow stage (`:11313`), and `notify_turn_queue_changed()` (`:11317`).
3. A `withdrawn` lineage outcome returns a `withdrawn` TurnAdmission with NO accepted-telemetry
   emitted (`:11298-11301`). A replay without lineage waits/recovers before returning (`:11293`).
4. A failed admission raises a typed `TurnQueueError` / `HarnessAdmissionBlocked`, converted to HTTP
   at the submit helpers (`control_api.py:226/228`, `:270/272`) — no 202, no SSE, no backend call.
5. The HTTP receipt is built only after the commit and reads the COMMITTED row
   (`turn_requests.py:156-172`, `db.get_turn_request`), then returns **202**
   (`turn_requests.py:172`). The legacy route returns its existing `{"ok": true, "task_id": …}`
   envelope after the same commit (`sessions.py:275-278`).

`enqueue_turn`'s own docstring names *"commit-before-acknowledge"* as a transaction property.

**Invariant #4 — canonical commit precedes ack; no ack/SSE/backend on failed commit: HONORED.**

---

## 4. `/api/turns` + `useSessionTurns` remain telemetry, not admission (invariant #11)

- `/api/turns`, `/api/turns/{id}`, `/diagnostics`, `/graph`, `/events` are ALL `@router.get` in
  `routes/monitoring.py:159-205` — read-only, no POST admission verb.
- `useSessionTurns` (`web/src/hooks/useLiveData.ts:155`) is a `useQuery` polling hook
  (`refetchInterval`) over `GET /api/turns?session_id=` (`web/src/transport/apiClient.ts:381`,
  rawApi.ts:557 — *"LLM turn observability"*). Comment at `apiClient.ts:154`: the admission queue is
  *"distinct from the telemetry `/api/turns` DTOs."*
- ALL web admission/mutation POSTs target the new `turn-requests` names:
  `/api/sessions/{id}/turn-requests/{pause,resume}`, `/api/turn-requests/{id}/{withdraw,
  resolve-recovery}` (`apiClient.ts:261-282`).

Telemetry plane and admission plane are cleanly separated; `/api/turns` was NOT repurposed into an
admission path.

**Invariant #11 — `/api/turns` + `useSessionTurns` stay telemetry: HONORED.**

---

## 5. Acceptance-vs-activation write split (summary)

| | ACCEPTANCE (admission) | ACTIVATION / START (later, by scheduler/carrier) |
|---|---|---|
| Writes | durable `mesh_tasks` row (queued/pending) via `enqueue_turn`; Case/role lineage; post-commit `turn.accepted`/`turn.queued` telemetry | the turn reaches the head, becomes active; session BUSY and native backend id are set by the activation/carrier path, not acceptance |
| Does NOT write | BUSY, `last_task_id`, `last_user_message`, native/backend id (`orchestrator.py:11196`) | — |
| Active-slot source | n/a (queued ≠ active) | read FROM the ledger, never `last_task_id`/queued id (`orchestrator.py:11844`) |

---

## 6. Observations (NOT defects — no decision required)

1. **Acknowledged dual front-door, single authority.** Both `POST /api/instructions` (enrolled
   branch) and `POST /api/sessions/{id}/turn-requests` accept enrolled-session turns. This is an
   explicit, documented overlap with a planned fold at Stage 8b — see the REVISIT note at
   `turn_requests.py:115`: *"overlaps POST /api/instructions for enrolled sessions — fold plan at
   that route."* It is NOT a parallel pathway: both converge on `_submit_managed_instruction` →
   `enqueue_turn` (§1). Two HTTP surfaces onto ONE admission authority is exactly what invariant #11
   permits (legacy keeps its envelope; turn-requests returns 202). No second queue, no second
   commit site. Flagged for awareness, not as a violation.
2. Packet-named contracts verified to EXIST as described (invariant #18): `enqueue_turn`
   (`db.py:2868`), the `turn-requests` route family (`routes/turn_requests.py`), `turn_admission`
   service (`turn_admission.py`). The only deviation from the packet is file location (refactor),
   not behavior.

---

## Conclusion

The Stage-8a backend was done **correctly** per the design: it reuses the existing admission layer
(`submit_instruction` → `_enqueue_task` → `_admit_managed_session_turn` → `admit_turn` →
`enqueue_turn`) as the SINGLE canonical authority for both the new `turn-requests` route and the
legacy `/api/instructions` route. It did **not** reintroduce the original A82 bug: no parallel
pathway, no second execution/prompt queue for enrolled sessions, no premature BUSY/active-task
mutation on acceptance, and commit strictly precedes acknowledgement.

**Single-admission-authority invariant: HONORED.**
**Convergence point: `MeshDB.enqueue_turn` — `src/control/db.py:2868`.**
**No defect found — no code change made; no reserved decision raised.**
