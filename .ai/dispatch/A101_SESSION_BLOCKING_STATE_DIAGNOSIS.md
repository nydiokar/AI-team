---
job: A101
title: Session window silently blocks a queued operator message on an invisible pause
type: research/diagnosis (read-only; no code changes)
author: Manager (session b1e91c10e1de, Case fc6661f439d74b9db10f213af2db119d)
created: 2026-10-07T18:55Z
status: done
scope: diagnosis + fix plan ONLY — no src/ or web/ edits, no flags, no fix implemented
---

# A101 — Why a quota-stopped session silently swallowed a queued operator message

## 0. Root cause in two sentences

The session window's turn-queue overlay models only the **session-column** operator pause/hold
(`sessions.turn_queue_paused` / `turn_queue_hold`), but the gate that actually withholds a queued
turn during a quota/transient/retry/manager-rebind pause lives in **Case-level `flow_events`** (and
per-row `blocked_reason`/`retry_pause_state`), which the overlay never reads or exposes — so the
session shows "N waiting" with no reason, while the only surface that renders the pause (a
`case_resume` approval) is the Work/mesh view. Because `CASE_QUOTA_RESUME_AUTO` is OFF by design
(resuming spends real money), the pause correctly required a manual operator action, but that action
and its prompt were structurally invisible in the window where the operator was typing.

---

## 1. Exact live state (grounded in the live gateway + mesh.db, read-only)

**Session `b1e91c10e1de`** (Manager, backend=claude, carrier=`kanebra`, model=opus, repo `AI-team`):

- `GET /api/sessions/b1e91c10e1de` (via list) → `status=awaiting_input`, `needs_input=true`,
  `reason.kind=waiting_workers`. Current `turn_queue`: `{queued:0, active_turn_id:null,
  active_status:null, paused:false, hold:null}` — i.e. the incident is **already resolved**; the
  queue is empty now. (`GET /api/sessions/b1e91c10e1de/turn-requests` → `enrolled:true, paused:false,
  hold:null, queued:0`.)
- DB confirms the session columns right now: `turn_queue_enrolled=1`, `turn_queue_paused=0`,
  `turn_queue_hold=NULL`.

**The Case:** `flow_run_id = fc6661f439d74b9db10f213af2db119d`.

**Reconstructed incident timeline** (from `flow_events` + `mesh_tasks`, all UTC 2026-10-07):

| Time | Event | Evidence |
|---|---|---|
| 17:45:58 | System **continuation** turn `cturn_3d25…` (queue seq 14, `turn_kind=continuation`, `turn_source=system`) **FAILED** on quota | `mesh_tasks.status=failed` |
| 17:46:23 | **`flow.quota_paused`** written (actor=system), entity=task `cturn_3d25…` | `flow_events` |
| 18:10:23 | **`approval.requested`** (the `case_resume` approval — the "pending resume" the operator saw in Work) | `flow_events` |
| 18:32:07 | Operator types **msg #1** → human turn **seq 15** `task_fb6ff2c4` (`turn_source=human`, action=`resume_session`) → enqueued. Shows as **"1 waiting"**. Never activated. | `activated_at=NULL`; `task.attached` event |
| 18:36:48 | seq 15 **withdrawn** (operator re-sent) | `mesh_tasks.status=withdrawn` |
| 18:36:55 | Operator **msg #2** → human turn **seq 16** `task_bcaac940` ("Continue quota stopped us") enqueued, still blocked | `task.attached`; `activated_at` later |
| 18:37:49 | Operator clicks **Resume** → recovery task `qresume:…:cturn_3d25…` (action=`manager_quota_resume`, machine_id=`__manager_continuation__`) completes **and** **`flow.quota_resumed`** (actor=system, entity=session) | `mesh_tasks` + `flow_events` |
| 18:37:55 | seq 16 **activates** (`activated_at`) → message finally delivered | `mesh_tasks.activated_at` |

So the session was **quota-paused for ~51 minutes (17:46 → 18:37)**. Throughout, the session's own
`turn_queue_paused`/`turn_queue_hold` stayed `0`/`NULL` — the pause was **never** a session-column
state. The "pending `resume_session`" the operator saw in the Work view was the **`case_resume`
approval** (`approval.requested` 18:10:23). "resume_session" is also, confusingly, the normal action
verb for *every* managed turn on an existing session (seq 2–17 are all `action=resume_session`) — so
the operator's own queued message is itself a `resume_session` row; the Work view additionally
surfaces the **Case-resume approval**, which is the thing the "Resume" click resolves.

Relevant flags (live `GET /api/flags`): `CASE_QUOTA_RESUME_ENABLED=True (registry)`,
**`CASE_QUOTA_RESUME_AUTO=False (default)`**, `TRANSIENT_PROVIDER_RESUME_ENABLED=True (registry)`,
`QUOTA_PREWARM_ENABLED=True (registry)`.

---

## 2. Why the queued turn did not activate until a manual resume

The scheduler head-selection and activation gates are in `src/control/db.py`:

- `select_eligible_turn_heads` (`db.py:3391`) and `activate_prepared_turn` (`db.py:3440`) both apply
  **`_MANAGED_RETRY_GATE_SQL`** (`db.py:915`).
- The **quota branch** of that gate (`db.py:920-944`) holds a queued turn whenever the **latest** of
  `flow.quota_paused | flow.quota_resumed | flow.quota_pause_declined | case.manager_respawned` for the
  session's manager-role Case is **`flow.quota_paused`**, *unless* a linked retry turn is already
  `claimed` with action `manager_quota_resume` for that exact pause event:

  ```sql
  AND pe.event_type = 'flow.quota_paused'
  AND NOT (t.turn_kind = 'retry' AND EXISTS (
      SELECT 1 FROM mesh_tasks tok
      WHERE tok.producer_turn_id = t.id AND tok.status = 'claimed'
        AND tok.action = 'manager_quota_resume' …))
  ```

  During the incident the latest quota event **was** `flow.quota_paused` (17:46:23) and there was **no**
  claimed `manager_quota_resume` turn, so **every** managed head on that session — including the
  operator's seq 15 / seq 16 — was held. This is the exact block.

- Releasing it is a deliberate, money-spending gate, **not** an accidental stall. Because
  `CASE_QUOTA_RESUME_AUTO=False`, `_handle_quota_paused_case` (`src/orchestrator.py:3026`) does **not**
  auto-resume; it raises the `case_resume` **approval** (seen at 18:10:23) and returns "pause owns the
  Case." Resume happens only via `resume_case` (`orchestrator.py:3525`) driven by the operator
  approving / clicking Resume. `resume_case` admits the `manager_quota_resume` recovery turn
  (`_quota_resume_managed` → `_admit_managed_recovery_turn`, `orchestrator.py:3706/3365`) and writes
  `flow.quota_resumed` — which flips the gate's "latest quota event" off `quota_paused`, and the queued
  head activates on the next scheduler pass (3 s). That is precisely the 18:37:49 → 18:37:55 sequence.

**Verdict:** the manual-resume requirement is a **correct, deliberate gate** (resuming a fat Manager
rewrites a 200–300k-token prompt cache and costs real money — the same reason Telegram is
notification-only). The stall was **not** a scheduler bug. The defect is purely that this gate was
**invisible** in the session window and had **no affordance co-located with the composer**.

---

## 3. Why it was surfaced only in the Work/mesh view, never in the session window

**Session window** (`web/src/screens/SessionDetailScreen.tsx`):
- Renders the queue only via `TurnQueuePanel` and only when `turnQueue?.enrolled`
  (`SessionDetailScreen.tsx:1335-1336`). Its data is the turn-queue overlay from
  `GET /api/sessions/{id}/turn-requests` → built by `list_turn_requests` (`db.py:4740-4751`) /
  `session_turn_queue_states` (`db.py:4822-4831`). That overlay exposes **only**:
  `queued, active_turn_id, active_status, enrolled, paused, hold` — where `paused` **is literally**
  `sessions.turn_queue_paused` and `hold` **is literally** `sessions.turn_queue_hold`.
- `TurnQueuePanel` computes `held = page.paused || page.hold != null` (`TurnQueuePanel.tsx:100`) and
  early-exits to **render nothing** unless `waitingCount>0 || held || needsAttention`
  (`TurnQueuePanel.tsx:260`). During a quota pause, `paused=false` and `hold=null`, so `held=false`;
  the only reason the panel shows at all is `waitingCount>0` → it prints **"N waiting"**
  (`TurnQueuePanel.tsx:283`) with **no reason**. The panel's own docstring admits the gap:
  *"resume never clears recovery/Case/quota holds (the server decides)"* (`TurnQueuePanel.tsx:22-23`).
- The composer is rendered unconditionally whenever the session is open — `<Composer sessionId={id}
  running={running} />` (`SessionDetailScreen.tsx:1356`); it is **not** disabled or annotated for a
  paused/awaiting-resume session, so the operator types and enqueues into a silent hold.

**Work/mesh view** reads a **different** data source: `useCaseResumeApprovals()` (`web/src/hooks/useWork.ts:264-269`)
= `GET /api/approvals?status=pending` filtered to `action === "case_resume"`, rendered by
`PausedCaseInbox` (`web/src/components/work/PausedCaseInbox.tsx`) and `CaseResumePanel`
(`web/src/components/work/CaseResumePanel.tsx` — "the operator's control over a Case that stopped on
quota"). The resume prompt and the **Resume** button are driven by that `case_resume` **approval** row
(cost estimate + reset time in its payload), which is a **Case** concept — it has no representation in
the session `turn_queue` overlay at all.

**The asymmetry in one line:** the "needs resume" signal rides on a `case_resume` **approval** +
Case-level `flow.quota_paused`; the session window queries neither — it only queries the session-column
`turn_queue` overlay.

---

## 4. Full taxonomy — every state that can hold a queued operator message

Each row: what holds it, where the gate is, whether the **session window** surfaces it today, the
operator action that clears it, and whether the composer blocks sending.

| # | Hold state | Gate (file:line) | Surfaced in SESSION window? | Clears via | Composer blocked? |
|---|---|---|---|---|---|
| 1 | **Operator queue pause** (`turn_queue_paused=1`) | `set_turn_queue_paused` `db.py:4835`; gate in head-select `db.py:3415` | **SURFACED** — `held` true → panel shows "Queue paused" (`TurnQueuePanel.tsx:100,254`) | Operator clicks Resume in panel (`set_turn_queue_paused(false)`) | No (still enqueues) |
| 2 | **Operator STOP hold** (`turn_queue_hold='operator_stop'`, `status='cancelled'`) | set `db.py:~3906`; released `db.py:~4860`; gate `activate_prepared_turn` `db.py:3493-3500` | **PARTIAL** — `hold != null` → `held` true, panel renders a hold label (`blockedReasonLabel`), but no first-class banner | Operator Resume (release hold) | No |
| 3 | **Quota pause** (latest Case event `flow.quota_paused`) — *the incident* | `_MANAGED_RETRY_GATE_SQL` quota branch `db.py:920-944`; approval in `orchestrator.py:3026` | **HIDDEN** — not in `turn_queue` overlay; `paused/hold` stay 0/NULL (proven live) | Approve `case_resume` / click Resume in **Work** → `resume_case` `orchestrator.py:3525` (or `CASE_QUOTA_RESUME_AUTO=1`) | No |
| 4 | **Transient provider pause** (latest Case event `flow.transient_paused`; `TRANSIENT_PROVIDER_RESUME_ENABLED=True` live) | `_MANAGED_RETRY_GATE_SQL` transient branch `db.py:946-968` | **HIDDEN** — same overlay gap | **Auto** — escalating backoff auto-retries (30/60/120/300s); no operator action; but message is held+invisible meanwhile | No |
| 5 | **Pending retry-pause** (`retry_pause_state='pending'` on a row) | `_MANAGED_RETRY_GATE_SQL` first clause `db.py:916-919` | **HIDDEN** | Auto (retry machinery) | No |
| 6 | **Manager rebind / lost-session fresh fork** (A98) or **crash respawn** (`case.manager_respawned`) — queued turn stuck on the *former* Manager session | `_MANAGED_CASE_BINDING_GATE_SQL`; `blocked_reason` forced to `'manager_rebound'` in `get_turn_request` `db.py:4762-4763` | **PARTIAL** — `blocked_reason='manager_rebound'` exists on the turn row but no banner; panel shows it only via `blockedReasonLabel` on an expanded row | New Manager owns the Case; stale turn never runs (by design) | No |
| 7 | **Carrier offline** (`blocked_reason='carrier_offline:<node>'`, `_QUEUE_TURNS_FOR_OFFLINE_CARRIER`) | admission/scheduler sets `blocked_reason`; head-select skips | **PARTIAL** — per-row `blocked_reason` only; no session banner | Carrier comes back online (operator restarts worker) | No |
| 8 | **Legacy work draining** (protocol-0 EXECUTION row still claimed/running) | `activate_prepared_turn` `db.py:3514-3535` (`legacy_work_draining`) | **HIDDEN** | Auto once legacy row terminal | No |
| 9 | **Backoff / not-before** (`blocked_until` / `not_before` in future) | head-select `db.py:~3395` | **HIDDEN** | Auto when time elapses | No |
| 10 | **Lineage not committed** (`lineage_state='pending'`) | head-select `db.py:~3399` | **HIDDEN** (momentary) | Auto (admission txn commits) | No |
| 11 | **`recovery_required` turn** occupying the active slot | counted as active in overlay | **PARTIAL** — `needsAttention` path (`TurnQueuePanel.tsx:104-116`) renders an attention state | Operator intervention on the recovery turn | No |
| 12 | **Session terminal `ERROR`/`driver_lost`** (not a pause — delivery impossible) | `select_eligible_turn_heads` excludes closed/cancelled; ERROR sessions do not advance | **PARTIAL** — session badge/status, but composer still open | Respawn / new session (A98 fork for lost sessions) | No |

**Summary count:** of the holds that can silently queue a message, **1 is fully SURFACED (operator
queue pause)**, **5 are PARTIAL (operator-stop, manager-rebind, carrier-offline, recovery_required,
terminal-ERROR — a field exists but no first-class banner)**, and **6 are fully HIDDEN (quota pause,
transient pause, retry-pause, legacy-draining, backoff, lineage)**. In **every** case the composer
accepts and enqueues, so the operator can always type into a silent hold.

---

## 5. Root cause + fix plan (recommend, do not implement)

### Root cause
Two layers disagree about where "this session can't deliver right now" lives:
- **Delivery gate (truth):** Case-level `flow_events` (`flow.quota_paused`/`flow.transient_paused`),
  per-row `retry_pause_state`/`blocked_reason`/`blocked_until`, and the manager-rebind binding gate —
  evaluated by `_MANAGED_RETRY_GATE_SQL` (`db.py:915`) and `activate_prepared_turn` (`db.py:3440`).
- **Session window's model:** only `turn_queue_paused` / `turn_queue_hold` session columns, projected
  through the `turn_queue` overlay (`db.py:4740-4751`, `4822-4831`).

The overlay is a strict subset of the gate, so the majority of holds are invisible in the exact place
the operator is typing. The composer never consults the gate either. The Work view happens to show one
specific hold (quota) only because it independently polls the `case_resume` **approval** table.

### (a) FRONTEND surfacing fixes — folds into A99 (the session-window pass)
1. **First-class pause banner co-located with the composer** in `SessionDetailScreen.tsx` (next to
   `<Composer>` at `:1356`): "This session is paused — <reason> — [Resume]" / "retrying automatically
   in Ns" / "awaiting carrier". It must cover quota, transient, retry, manager-rebind, carrier-offline,
   recovery_required — not just `paused`/`hold`.
2. **Composer should annotate, not silently enqueue:** when a hold is active, show "Your message will
   queue until this session resumes" (and optionally keep send enabled, since queuing is legitimate) so
   "1 waiting" is never unexplained.
3. **Wire the session-window Resume to the same action the Work view uses** (`resume_case` for a quota
   pause) so the operator never has to leave the session window. Reuse `CaseResumePanel`'s decision
   data (cost estimate / reset time) inline.
4. `TurnQueuePanel` should stop treating `held = paused || hold` as the whole truth
   (`TurnQueuePanel.tsx:100`) — it should consume the unified reason from (b).

### (b) BACKEND fix — expose the gate as a read-model (the enabling change)
The frontend cannot surface what the API does not return. Add a single **`blocked`/`pause_reason`**
field to the session detail + `turn-requests` payload that unifies, in priority order:
`turn_queue_hold` → `turn_queue_paused` → latest `flow.quota_paused`/`flow.transient_paused` (with the
pending `case_resume` approval id + cost/reset where present) → `retry_pause_state` →
`manager_rebound` → `carrier_offline`/`blocked_until`. This is a pure read-model mirroring the gate
(`_MANAGED_RETRY_GATE_SQL`); it changes **no** scheduling behavior.

### Behavior question the operator raised — should resume be automatic?
**Recommendation: keep the manual gate; do not auto-resume quota pauses by default.** Resuming a fat
Manager rewrites a 200–300k-token prompt cache (real money); the existing design intentionally gates
this behind an authenticated Web approval and makes Telegram notification-only. The incident was **not**
caused by the gate being manual — it was caused by the gate being **invisible** where the operator was
working. `CASE_QUOTA_RESUME_AUTO` already exists for operators who want hands-off resume. **Transient**
pauses (#4) already auto-resume on backoff, so they need **only** surfacing, not a behavior change.
No backend scheduling change is recommended beyond the read-model in (b).

---

## 6. Evidence index (how to reproduce every claim)
- Live: `GET /api/sessions` (record for `b1e91c10e1de`), `/api/sessions/b1e91c10e1de/turn-requests`,
  `/api/nodes`, `/api/flags`.
- DB (read-only `mode=ro&immutable=1` on `~/ai-team-data/controller/state/mesh.db`): `mesh_tasks`
  seq 1–17 for the session; `flow_events` for `flow_run_id=fc6661f439d74b9db10f213af2db119d`;
  `sessions` columns.
- Code: `src/control/db.py:915` (`_MANAGED_RETRY_GATE_SQL`), `db.py:3391/3440/3493`,
  `db.py:4740-4751` + `4822-4831` (overlay), `db.py:4762-4763` (`manager_rebound`);
  `src/orchestrator.py:3026` (`_handle_quota_paused_case`), `3525` (`resume_case`), `3365/3706`;
  `web/src/screens/SessionDetailScreen.tsx:1335,1356`; `web/src/components/timeline/TurnQueuePanel.tsx:22,100,260,283`;
  `web/src/hooks/useWork.ts:264`; `web/src/components/work/{PausedCaseInbox,CaseResumePanel}.tsx`.
