---
job: A101
note: O1 findings — how the CURRENT Work/Case resume decision behaves (so we replicate it faithfully)
author: A101 worker
created: 2026-10-07T22:40Z
scope: investigation only (prerequisite for the surfacing move); grounded in code (file:line)
---

# A101 · O1 — Current resume/Case decision flow (faithful baseline)

This documents, before any change, exactly what the Work/Case resume decision asks, the
options it offers, how the choice is applied, and how reliable it is. The A101 move replicates
this **verbatim** — same options, same effects — only relocating it into the session window and
adding a push. Nothing in this flow changes.

## 1. What raises the decision

A quota stop writes `flow.quota_paused` for the Case (`_handle_quota_paused_case`,
`orchestrator.py:3120`). When the quota window later reopens and `CASE_QUOTA_RESUME_AUTO` is
**OFF** (the default — resuming spends real money), the orchestrator does **not** auto-resume: it
raises a `case_resume` **approval** (`orchestrator.py:3174-3192`, `action=CASE_RESUME_APPROVAL_ACTION`,
`risk=medium`, `reversible=true`) and emits `case_resume_proposed`. The approval payload carries the
whole decision: `case_id, paused_task_id, session_id, mode` (the recommended mode), `cause`,
`paused_at`, `reset_at`, `quota_evidence`, `estimate_usd`, `estimate_known`, `objective_excerpt`.
A push already fires here (see §5).

If the operator never decides and quota is *not* restored, the queued message stays held by
`_MANAGED_RETRY_GATE_SQL` (`db.py:915`, quota branch `db.py:920-944`) — the exact gate that hid the
incident message.

## 2. Where it surfaces today (the Work/mesh view only)

- **`PausedCaseInbox`** (`web/src/screens/WorkScreen.tsx:129`) — a top-of-Work prompt listing
  `GET /api/approvals?status=pending` filtered to `action==="case_resume"`
  (`useCaseResumeApprovals`, `useWork.ts:264`). Per case it shows the objective excerpt + a cost
  label ("≈ $X.XX to resume" or "resume cost not measurable"). Options here are only
  **`Later`** (`POST /api/approvals/{id}/resolve {decision:"rejected"}`) or **`Choose how →`**
  (navigate to `/work/{case_id}`). It is a router, not the decision itself.
- **`CaseResumePanel`** (`web/src/screens/WorkDetailScreen.tsx:261`, component
  `web/src/components/work/CaseResumePanel.tsx`) — the actual decision surface. Data from
  `GET /api/cases/{id}/resume-state` (`useCaseResumeState`). It renders: paused-on-quota state,
  when the window reopens (local time), the cost estimate (or an honest "not measurable"), a
  "decision pending" badge, and the buttons.

## 3. The EXACT options offered (this is what we replicate)

`CaseResumePanel` shows one of two button sets, both staying on the **same Case**:

**When an approval is pending (quota restored):** prompt "Quota is back. Resume this Case — you
choose how:" with three options —
| Button | Effect |
|---|---|
| **Decline** | `resolveApproval({approvalId, decision:"rejected"})` → `POST /api/approvals/{id}/resolve`. Backend writes `flow.quota_pause_declined`; Case stays open, operator can still resume manually. |
| **In place** | `resumeCase({mode:"in_place"})` → `POST /api/cases/{id}/resume`, then (if pending) resolve approval `approved`. One turn into the existing Manager session (full history; pays the prompt-cache rewrite). Disabled if the Manager session is gone. |
| **Fresh Manager** | `resumeCase({mode:"fresh_manager"})` → same route; new Manager rebuilt from the Case ledger (cheap; the only option when the old session is gone). |

The **recommended** mode (`data.recommended_mode`, computed by `_recommended_resume_mode`,
`orchestrator.py:2913` — in_place if cache is warm/<1h and ≤100k cache tokens, else fresh_manager)
is rendered as the `primary` button; the other is `outline`. Both are always available — the
operator may override the recommendation.

**When no approval is pending (Manager gone, or direct resume):** prompt "Continue this Case (same
objective, not a fork)" with **In place** / **Fresh Manager** only (no Decline). If the window is
still closed (`blockedByQuota`), the panel shows "Waiting for the window to reopen. You will be
asked before anything is spent" and offers no buttons yet.

## 4. How the choice is applied (backend)

`resume_case` (`orchestrator.py:3525`) is the single entry. `mode` defaults to the recommended mode;
an `in_place` on a CLOSED/CANCELLED session is auto-upgraded to `fresh_manager`. It is **single-flight**
server-side (atomic `claim_task` on `quota_resume_task_id(case, paused_task_id)`): a second concurrent
attempt (e.g. the approval callback racing the direct button) loses with `resume_in_flight` (409,
harmless no-op). `fresh_manager` → `_do_respawn_manager_for_case`; `in_place` on an enrolled Manager →
`_quota_resume_managed` (A82 Stage 4e, queues a managed `manager_quota_resume` turn whose durable
finalizer writes `flow.quota_resumed` at terminal commit). `flow.quota_resumed` flips the gate's
"latest quota event" off `quota_paused`, so the held head activates on the next scheduler pass (~3s).
Approving the `case_resume` approval fires `_on_case_approval_resolved` (`orchestrator.py:2446`) which
re-invokes `resume_case` with the stored `mode`/`paused_task_id` — the mode choice rides in the
approval payload, not the button. Declining writes `flow.quota_pause_declined` and stops owning the
pause on the next tick.

Reason codes (surfaced to the UI as stable machine strings, mapped to copy in
`CaseResumePanel.REASON_COPY`): `resume_in_flight`, `manager_busy`, `case_terminal`,
`no_manager_link`, `continuation_disabled`, `respawn_failed`, `deliver_failed`, `db_unavailable`.

## 5. The existing push (reused for O4)

`notify_case_resume_proposal` (`notification_service.py:233`) already fires at approval creation
(`orchestrator.py:3204-3211`) on BOTH channels, best-effort / never-raise: Web Push (deep link) +
Telegram (notification-only — no approve/decline affordance, because approving spends real money and
the decision surface must be the authenticated Web UI). Today the deep link is `/work/{case_id}`.
A101 keeps this exact mechanism and only re-points the deep link to the session window
(`/sessions/{session_id}`), since that is where the decision now lives.

## 6. Reliability

The gate is a **deliberate, money-spending** manual gate, not a stall bug (diagnosis §2 verdict):
resuming a fat Manager rewrites a 200–300k-token prompt cache. It has been reliable *as a gate* — the
incident was purely that the gate and its decision were **invisible in the session window** where the
operator was typing (diagnosis §3). Single-flight + durable approvals (replayed on gateway restart)
make the apply path robust. Flags: `CASE_QUOTA_RESUME_ENABLED` (ON, registry), `CASE_QUOTA_RESUME_AUTO`
(**OFF** — stays off per O5), `TRANSIENT_PROVIDER_RESUME_ENABLED` (ON, auto-resumes, no decision).

## 7. Faithfulness decision for the move

Because `CaseResumePanel` is a self-contained component (`caseId` in → fetches resume-state, renders
the exact 3-option decision, applies the exact effects, self-gates its own visibility), the most
byte-for-byte-faithful move is to **render the identical component in the session window** and remove
its Work-tab mounts — no change to its logic, options, or effects (O3). The session window learns
*when* to show it (and which Case) from a new read-only `blocked`/`pause_reason`/`resume_case_id`
projection on the turn-queue read-model, which purely mirrors the scheduler gates and changes no
scheduling behavior. The in-session banner appears when the session has a **held (queued) managed
turn** on a quota pause — exactly the incident condition — and the push alerts the operator at pause
time regardless of whether a message is queued yet.
