```yaml
job_id: AGENT_104_UNIFIED_AGENT_INBOX
created_at: "2026-10-09T17:17:51.298555+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-09T17:17:51.298578+00:00"
```

# DISPATCH — 104 · One agent inbox: finish A84 properly, retire the competing "what is waiting" systems

**Level:** 3 (schema migration + live data migration, >5 files, crosses the gateway↔worker
boundary, changes the Manager's tool surface) · **Type:** code (+ one live data migration)
**Authored:** 2026-10-09 · **Status of this packet:** ready — commissioned by the operator
2026-10-09. **All decisions are made (see DECISIONS); there is nothing to ask the operator.**
Execute end to end. The only hard stop is the repo rule "never restart a worker", and this job
needs no worker restart.
**Depends on:** — (coordinate with **A102**: it is in flight in `src/backends/`, `src/worker/`
and parts of `src/orchestrator.py`. Rebase on `main` often and never carry its edits.)
**Branch:** `feat/unified-agent-inbox`. Ship it as a short chain of PRs, one per phase gate
below. Each PR must be self-contained and green, and self-merged per the repo branch policy.

> **Read this first — why this packet exists.** On 2026-10-09 the Manager session
> `5a23135eeb97` was never woken when its worker (`task_7b175284`, session `243299713009`)
> finished at 15:16 UTC. Instead, the gateway admitted and withdrew a wake turn about every 32 s
> for six hours. That left **1,248 "cancelled" telemetry turns** and **1,560 withdrawn
> `mesh_tasks` rows**, plus ~1,300 junk links and events per Case. The session's reason stayed
> `waiting_workers` permanently, and the operator's own 16:29 message and the Manager's reply
> disappeared from the chat. No paid tokens were burned, because withdrawn turns never ran, but
> **every** Case born in outbox mode since the flag went ON (2026-10-07 22:43 UTC, 5 Cases) hit
> the same loop. The cause is not a single bug. Three separate mechanisms each answer the same
> question, *"what is waiting for this agent, and has it seen it?"*, from different stores and
> in different ways, and they disagree. This job makes ONE store and ONE function the only
> answer. The design doc that produced A84 already said to do this; the implementation was cut
> back to an additive half.

## Why (intent)

The outcome: **a message system that just works.** When an agent asks another agent for work,
the reply lands in the asker's inbox in the same transaction that records the work as finished.
The asker is woken exactly once, the message is marked consumed when that wake turn completes,
and every surface (wake, "waiting" badge, Manager brief, close gate, chat, UI) reads that one
state. Nothing is re-derived from a truncated log. Nothing loops. Nothing is keyed by role:
roles may disappear, so addressing is **agent→agent** (session→session) and the Case id is
only provenance.

The literal request is "fix the wake loop". The real goal is coherence: no component may form
its own idea of pending/waiting/consumed state. A patch that makes the two readers agree while
leaving them as two readers is **not** acceptable. That is how this incident was created.

## CONTEXT (reuse verbatim — verified 2026-10-09 against `main` @ `c64a4a7` and the live DB)

### What runs today (four stores, seven readers, no single owner)

```
 SENDERS                        WRITES                                         STORES
 Manager dispatch_worker ──┐
 Operator message        ──┼─► task row + "task" link to the Case ──────────► [A] mesh_tasks
 Manager's own wake turn ──┘     task finishes (one txn):                     [B] flow_links / flow_events
                                   ├─► "task.finished" event ────────────────►     (append-only Case log)
                                   └─► completion_outbox row, if the Case was ► [C] completion_outbox
                                       born "outbox" AND the task is linked        (addressed to the CASE)
 Manager arm_wait_group ──────► "worker.wait_pending" event ─────────────────► [B]
 Manager record_review  ──────► "review.*" event ─────────────────────────────► [B]

 READERS — each decides "is something pending?" on its own
 (1) Wake-Dispatcher producer   orchestrator.py:1987 / :2243 / :2319   reads [C] (outbox) or [B] (legacy)
        → writes token cont:<case>:<N> [D] → admits wake turn cturn_* into [A] (+ link, event, telemetry row)
 (2) Activation obsolete check  orchestrator.py:12096 (tick at :12155)  reads [B] ONLY → withdraws "reviewed"
 (3) Finalizer                  orchestrator.py:2516, db.py:7981         reads [D] → withdrawn ⇒ token re-armed ⇒ (1) again
 (4) Session reason             core/session_reason.py:106               reads [B] (wait_pending w/o wait_resolved)
 (5) Brief / close / boot / relay / heartbeat — db.py:8943, :6917, :9100, :7432, :7473, :7654; orchestrator.py:1597
                                                                         read [B] via list_flow_events (OLDEST 500)
 (6) Chat transcript            db.py:6271 get_session_turns             reads [A] OLDEST 1,000 rows, then drops withdrawn
 (7) In-turn wait_for_worker    scripts/mcp_manager.py:534               long-polls /api/flows events
 + agent→agent send path        scripts/mcp_sender.py, orchestrator.py:7705/:11237 (sender_session_id) — a 4th
                                messaging path that worker dispatch does NOT use (sender_session_id is set on 1 of
                                all live mesh_tasks rows)
```

### The failure chain, step by step (each step is evidenced in the live DB and logs)

1. **Wrong addressing.** The outbox writer (`db.py:5576`, `_record_case_child_outbox`) emits a
   row for ANY task with a `flow_links` `entity_type='task'` link to the Case. Its docstring
   claims the Manager's own turns carry no such link, but they do: `orchestrator.py` `member()`
   (~:11548) links continuation-attach and `find_open_case_for_session` turns with
   `created_by='system'`. So the Manager's boot turn (`task_a6896384`), its own wake turns
   (`cturn_1756b4b…`, `cturn_9a02de18…`) and the operator's messages (`task_637e22b0`,
   `task_f5327548`) all became "completions to review". Only `task_7b175284` was real.
   There is no sender/recipient concept anywhere.
2. **Two truths.** Producer (1) reads the outbox and admits a wake. Activation check (2) still
   reads the legacy wait-group ledger, which never contains outbox items, so it sees nothing
   pending and withdraws the wake with `reason=reviewed`. All 1,553 logged withdrawals carry
   that reason.
3. **No bound.** Finalizer (3) treats "withdrawn" as "nothing consumed" and re-arms the token,
   so (1) re-admits it. The token `cont:312ef564…:1` reached `attempt` 659. There is no
   attempt cap, dead-letter or alert.
4. **Noise displaces facts.** Every never-run wake writes a `mesh_tasks` row, a `flow_links`
   row, a `task.attached` event and an `llm_turns` row. `list_flow_events` (`db.py:7084`) is
   `ORDER BY id ASC LIMIT 500`. By 15:17, Case `83d10aec…` had 507 events, so the
   `gpu-enable` wait group and the real worker's `task.finished` were past the window. Replayed
   on a DB copy, the legacy tick at 15:17 returns `presented=[]`. The real completion was
   withdrawn along with the junk. The same oldest-N pattern hides the latest chat turn
   (`get_session_turns`, oldest 1,000).
5. **States that never resolve.** In outbox mode the legacy `retire_only` drain never runs
   (`_compute_outbox_tick` returns `retire_only_groups: []`), so the wait groups `preflight` and
   `instrument` never get `wait_resolved`. Reader (4) therefore shows `waiting_workers` forever.

### How it got here (design history — `docs/TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md`, `AGENT_84_WORKER_COMPLETION_OUTBOX.md`)
- **The problem A84 was meant to solve** (design :19-35): completions finished "into a void"
  when no wait group was armed, because "completion is not a signal the system delivers; it is
  a fact the Manager must arrange to re-derive". That diagnosis was right.
- **The design's end state** (design :208-211):
  - `arm_wait_group` → **DELETE**
  - `compute_continuation_tick` → **REPLACE with outbox SELECT**
  - `reconcile_worker_waits` / `boot_reconcile_case` → **DELETE**
- **The packet narrowed it** (A84 :26, :248): "DO NOT delete the wait-group primitive. The
  outbox is ADDITIVE". Only reader (1) was switched. Readers (2) and (5) were never named. The
  switch of reader (4) was deferred to A87 (`AGENT_87…:110`) and never done. T9 (seeding the
  outbox from the ledger) was never built.
- **Review and proof:** PR #202 had 0 reviews and 0 comments. The "live proof"
  (`A84_OUTBOX_E2E.md`) faked the Manager store. The flag went ON in production the same night
  (`DISPATCH_LOG.md:52`). No rollback was documented.

### Live population this job must not hurt (2026-10-09)
- `flow_runs`:
  - legacy: 439 closed, 96 cancelled, **10 open**, **1 blocked**;
  - outbox: 1 closed, **3 open**, **1 blocked**.
- 2 Cases are already past 500 events.
- `CASE_COMPLETION_OUTBOX_ENABLED`: `true`, source `registry`, `effect_scope=birth`.
  `CASE_CONTINUATION_ENABLED`: env `1`.
- Worker-side callers of gateway state that must keep working with an **un-redeployed** worker:
  - `src/backends/claude_driver.py:1950` calls `remote.boot_reconcile_case` →
    `src/control/task_server.py:905`;
  - Manager tool allowlist at `src/backends/claude_role_adapter.py:29-36` and
    `claude_driver.py:584`.

## Target design (the invariants — every task below serves these)

- **I1 One store.** A per-agent **inbox** table, evolved from `completion_outbox`. Do not add a
  fifth store; the outbox already gets the hard part right (atomic with the terminal write).
  Each row holds:
  - `message_id`, `recipient_session_id`, `sender_session_id` (NULL = human/system),
    `about_task_id`, `case_id` (provenance only), `kind`;
  - `state` ∈ {`pending`, `delivered`, `acked`, `dead`}, `attempts`, `last_error`, timestamps.
- **I2 Agent addressing, role-free.** The recipient is whoever **requested** the work, recorded
  on the child task at dispatch time (`sender_session_id`). Reuse the existing agent-send
  identity path (`orchestrator.py:7705`, `:11237`; `mesh_sender_capabilities`). Do not invent a
  new one. Rules:
  - A human requester gets no inbox row; push notifications stay as they are today.
  - A session never addresses itself.
  - No code path may branch on `case_role` to decide addressing.
- **I3 One read function.** `pending_for(recipient_session_id, *, case_id=None)` is the **only**
  way any component learns what is waiting. That covers the wake producer, the activation
  check, the session reason, the brief, the close gate, boot reconcile, heartbeat liveness,
  `wait_for_worker`/`reconcile_waits` (if kept) and the web views. The producer and the
  activation check call the same function, so they cannot disagree.
- **I4 One state machine, written transactionally.**
  - `pending` is set in the terminal txn.
  - `delivered` is set when a wake turn carrying the message ids is admitted.
  - `acked` is set in the wake turn's completion txn, or by a tagged `review.*` of
    `about_task_id`.
  - `dead` comes with a reason (Case closed, recipient gone with no successor, attempts
    exhausted).
  - Consumption is the message's state. There is no separate watermark or token arithmetic.
- **I5 Bounded delivery.** A wake that is withdrawn, refused or fails returns its messages to
  `pending` with `attempts+1` and backoff. After N attempts (D3: N=5) the message goes `dead`, an
  operator-visible alert is raised, and the push seam is reused. Nothing re-admits forever.
- **I6 Never-run work leaves no trace in fact stores.** A withdrawn or never-activated turn
  writes no `flow_links`, no `flow_events` and no `llm_turns` row. If telemetry must record it,
  it does so in a distinct status that no "turns" count includes. `flow_events` stays as an
  **audit log**; no state is derived from it.
- **I7 No state from truncated windows.** No reader derives state from an oldest-N slice of an
  append-only log. Fix `list_flow_events` callers and `get_session_turns` (newest window,
  never-run rows excluded in SQL, index-served).
- **I8 Wait conditions are filters, not ledgers.** If "wake me only when ALL of {a,b,c} are in"
  survives (D2: it does), it is a delivery condition stored with the inbox and evaluated over inbox
  rows. It is not a second ledger in `flow_events`.

## TASK (phases; each ends at a gate — do not start the next phase until the gate's proof is in the packet)

**Phase 0 — Containment (decided, D1).** Verify `scripts/ops_flag.sh get
CASE_COMPLETION_OUTBOX_ENABLED` reads `false` (the operator switches it off on 2026-10-09). If it
is still `true`, record that in TRAIL and continue; the fix does not depend on it. Touch no Case.

**Phase 1 — Full consumer inventory (read-only, written into this packet before any code).**
1. Enumerate every **writer** and **reader** of pending/waiting/consumed state, in a table:
   `symbol | file:line | store read/written | role in the flow | fate (keep / repoint to
   pending_for / delete / shim) | who consumes its output`. Cover at minimum:
   - **Readers already named:** every symbol named in CONTEXT, all 18 `list_flow_events`
     callers, all `compute_continuation_tick` / `continuation_watermark` callers.
   - **Background tasks and paths:** the finalizer, the reaper (PR #203), crash-respawn (A55),
     `resume_case` / `interrupt_case`, `_escalate_headless_case`, round-cap escalation, and
     A101 blocking-state surfacing.
   - **Tool surface and APIs:** the MCP tools in `scripts/mcp_manager.py` (including the reply
     text at :424-482 that instructs `arm_wait_group`), `scripts/mcp_sender.py`, and the
     `/api/flows`, `/api/work/*` and `/api/cases/*` routes.
   - **Web UI hooks:** session reason, Work timeline/detail, info tab, queue card.
   - **Worker-side callers:** the `src/worker/*` and `src/backends/*` references.
   - **Tests** that pin current behaviour.
2. For each item marked delete/shim, name its downstream consumers and how they keep working.
3. Classify the live data: for each open or blocked Case (10 + 1 legacy, 3 + 1 outbox), list
   what is genuinely pending (finished dispatched children not yet reviewed) versus junk.
- **Gate 1:** the inventory table is in this packet, and every row has a fate and a consumer
  plan. Non-paid check: `rg` commands reproduced in TRAIL.

**Phase 2 — Inbox schema + addressing (additive, behind no new flag; the migration is the switch).**
1. Write a migration that evolves `completion_outbox` into the I1 shape. Keep existing rows and
   the `idx_*_pending` index pattern (index on `recipient_session_id`, `state`).
2. Record the requester at dispatch: `dispatch_worker` (and any agent-originated work) sets the
   child task's `sender_session_id` through the existing identity path. Write the inbox row in
   the existing terminal txn (`_record_case_child_outbox` → rename/generalise), addressed to
   `sender_session_id`, with no row for a human sender or for self.
3. Write `pending_for()` as a pure, index-served, bounded read, plus the state-transition
   helpers. All transitions are conditional updates, so they are idempotent.
- **Gate 2:**
  - Unit tests, written RED-first, prove:
    - the 2026-10-09 shape: the requester gets exactly one row for a real worker; the Manager's
      own turns, operator messages and wake turns produce zero rows;
    - worker→worker dispatch addresses the requesting worker (proves role-free);
    - a human-dispatched task produces none;
    - a terminal-write rollback leaves no row.
  - Plain `pytest` on the touched modules only.

**Phase 3 — Repoint every reader to `pending_for` (I3) and the delivery state machine (I4/I5).**
1. Wake producer, activation check, finalizer and session reason move first, as one PR, so
   they cannot diverge even transiently.
2. Then brief, close gate, boot reconcile (keep the **server endpoint signature** stable for
   un-redeployed workers), heartbeat liveness, the web views and the MCP tools (D5).
3. Add the attempt cap, backoff, dead-letter and alert (I5). Withdrawal must call the state
   machine and must never re-arm silently.
4. Stop never-run turns from writing links, events and telemetry (I6).
- **Gate 3:**
  - A fake-carrier scenario matrix passes (no paid CLI; extend the `test_case_continuation` /
    `test_completion_outbox_drain` harnesses). Every scenario asserts both
    `pending_for` and the number of wakes admitted:
    - worker finishes while the recipient is idle, busy (an operator turn in flight), dead and
      respawned, or the gateway restarts mid-delivery;
    - two workers under an ALL condition (D2);
    - an out-of-band tagged review acks without a wake;
    - an operator message interleaves before the wake;
    - the Case is closed with pending messages → `dead(case_closed)`;
    - the recipient is rebound or replaced → D4 behaviour;
    - a wake withdrawn N times → exactly N admissions, then `dead` + alert;
    - a Case with >500 events and >1,000 session rows still wakes and renders correctly.
  - One test asserts the producer and activation predicates are the **same function**: the
    activation check calls `pending_for`, enforced by a call-graph or monkeypatch assertion.

**Phase 4 — Live data migration (pre-approved, D8).**
1. Write the migration script with `--dry-run` (default) and `--apply`. It does three things:
   - **Seed pending rows** for every open or blocked Case, legacy included, from the
     ledger: dispatched children that finished and were not reviewed or consumed. This is the
     never-built design T9. It also rescues "void" completions on legacy Cases.
   - **Mark junk outbox rows `dead(superseded)`**: Manager-own turns, operator messages and
     wake turns.
   - **Discharge the stuck continuation tokens** (`cont:312ef564…:1`, `cont:83d10aec…:3`, and
     any others found) so no producer resumes them.
2. Dry-run on a **copy** of the prod DB (`sqlite3 … ".backup"` into scratch). Emit a per-Case
   diff report covering pending before/after, rows killed and tokens discharged. Every genuine
   completion must end up `pending` or `acked`; none may be lost.
3. Apply only after (a) the dry-run report shows **zero genuine completions lost** and (b) a fresh
   DB backup (`deploying-the-gateway` skill: DB backup when migrations apply). Record the backup
   path. If (a) fails, stop and fix the script; do not apply a lossy migration.
- **Gate 4:** the dry-run report and the backup path are in TRAIL.

**Phase 5 — Remove what the inbox superseded (only rows whose Gate 1 consumer plan is satisfied).**
1. Delete or stub the following, each tied to its Gate 1 row:
   - the legacy satisfaction and watermark logic (`compute_continuation_tick` satisfaction,
     `continuation_watermark` consumption, `retire_only`, the outbox/legacy mode branch);
   - the wait-group fold readers;
   - the `CASE_COMPLETION_OUTBOX_ENABLED` birth flag and the `continuation_mode` routing.
2. Keep `flow_events` writes as audit only.
3. Keep compatibility shims for anything an un-redeployed worker calls (D7). Do not remove them
   in this job, and never restart a worker.
4. Fix I7 for every remaining `list_flow_events` caller and for `get_session_turns`.
- **Gate 5:**
  - `rg` proves zero remaining readers of the removed symbols outside the shims, and every test
    that referenced them is updated or deleted, with the reason given.
  - The targeted test modules for every touched file pass (list the modules in TRAIL).

**Phase 6 — Deploy + live proof.**
1. Merge, then deploy the gateway (`deploying-the-gateway` skill; gateway restarts are
   delegated, worker restarts are not).
2. Run the live checks below and record their output.
- **Gate 6 = ACCEPTANCE below.**

## ACCEPTANCE (proof, not vibes)
1. **One answer.** `pending_for` is the sole reader of pending/waiting state. `rg` output in
   TRAIL shows every caller, and each is on the Gate 1 list.
2. **Incident reproduction is green.** A regression test reproduces 2026-10-09 end to end on a
   real file-backed `MeshDB` and produces exactly ONE wake presenting only the worker task, zero
   withdrawals and zero self-addressed rows. The setup is: a Manager-own turn, operator
   messages, a worker finish, more than 500 Case events and more than 1,000 session rows.
3. **Scenario matrix (Gate 3) green.** List the modules and pass counts.
4. **Migration is lossless.** The dry-run report on the prod copy shows zero genuine
   completions lost. After `--apply`, no stuck token exists. Live query:
   `select count(*) from mesh_tasks where id like 'cont:%' and status in ('pending','claimed')`
   is 0 or fully explained.
5. **Live, after deploy:**
   1. One real dispatch → worker finish → wake → review cycle on prod produces exactly 1 wake
      and the message reaches `acked`.
   2. No `turn_withdrawn_obsolete` loop: count withdrawals per token in the gateway log over
      24 h; it is ≤ the attempt cap for every token.
   3. The session reason clears when nothing is pending.
   4. The latest reply shows in the chat for `5a23135eeb97`.
   5. The info tab shows no never-run turns as "cancelled".
   6. `curl http://127.0.0.1:9003/health` is OK, and the checking-live-state snapshot is
      clean.
6. **Nothing upstream or downstream broke.**
   - Closed legacy Cases still render in the Work timeline/detail (spot-check 3: one closed,
     one cancelled, one with wait groups) with the same content as before.
   - An un-redeployed worker still boots a Manager (`boot_reconcile_case` endpoint
     compatibility test).
   - The Manager's tool allowlist and the `dispatch_worker` reply text are consistent with the
     tools that exist.
7. **No role addressing.** `rg -n "case_role" ` over the new inbox code finds no addressing
   decision based on role, and a worker→worker test proves it.

## DECISIONS (made by the operator's delegate, 2026-10-09 — do not reopen)
- **D1 — Containment.** `CASE_COMPLETION_OUTBOX_ENABLED` goes OFF now, so new Cases are born on
  the old wait-group path. That path is self-consistent and handled 545 Cases. No Case is
  closed. The two looping Cases (`312ef564…`, `83d10aec…`) keep writing harmless junk (no paid
  tokens) until Phase 4 discharges their tokens.
- **D2 — "Wake when ALL of these are in" is kept**, as an I8 delivery filter stored with the inbox.
  It is not a ledger in `flow_events`. Managers use it today (e.g. `preflight` ALL) to avoid one
  wake per parallel worker.
- **D3 — Delivery bound.** At most 5 attempts per message, with exponential backoff from 30 s.
  Then `dead(attempts_exhausted)` and an operator-visible alert through the existing push seam.
- **D4 — Replaced recipient.** Follow the recorded lineage (`continued_from` / the rebind
  record) to the successor session. This is role-free. With no successor, the message goes
  `dead(recipient_gone)` + alert.
- **D5 — Manager tools.** `arm_wait_group`, `reconcile_waits` and `wait_for_worker` stay as thin
  shims over the inbox: `arm_wait_group` sets the D2 filter, and the other two read
  `pending_for`. Update their texts and the `dispatch_worker` reply text to match. Removing them
  is out of scope.
- **D6 — Junk data.** Delete nothing. The withdrawn rows and junk links/events stay in the DB;
  I6/I7 make every read ignore them.
- **D7 — Workers.** No worker redeploy or restart in this job. Every gateway endpoint a worker
  calls keeps its signature and behaviour (compatibility shims). Shim removal is a follow-up
  for whenever workers are next redeployed for other reasons.
- **D8 — Migration apply is pre-approved** once the dry-run proves zero genuine completions lost
  and a fresh DB backup exists.
- **D9 — PR chain.** One PR per gate (Gates 2, 3, 5 carry code; Gate 4 carries the migration
  script). Self-merge each when green. Deploy the gateway after Gate 4's apply and again after
  Gate 5.

## SCOPE OUT
- A102 backend unification (create/resume/compact/cancel bodies, TurnControl). Do not touch
  `src/backends/*` beyond what Gate 1 proves is required for compatibility.
- New UI features. UI changes are limited to pointing existing views at the new state.
- Any paid or e2e test run. Use plain targeted `pytest` only (`running-targeted-tests` skill).
  Never run `--run-e2e` or the full suite.
- Message bodies or free-form agent chat beyond completion notifications. The schema must not
  preclude them (I1 has `kind`), but this job ships completions only.

## TRAIL / EVIDENCE (fill at close)
- Gate 1 inventory table (inline above or linked section) + reproduction `rg` commands
- Gate 4 dry-run report path + DB backup path
- Test modules + pass counts per gate
- Deploy tag (`deploy/<stamp>-<sha>`) + live acceptance outputs

---
## Milestone (burndown)
- [ ] D1 containment verified (flag value recorded)
- [ ] Gate 1 — inventory complete, every row has a fate + consumer plan
- [ ] Gate 2 — inbox schema + agent addressing, RED→GREEN tests
- [ ] Gate 3 — all readers on `pending_for`, bounded delivery, never-run turns traceless; scenario matrix green
- [ ] Gate 4 — migration dry-run lossless; operator go; applied with backup
- [ ] Gate 5 — superseded code removed, shims kept, I7 fixed, `rg` clean
- [ ] Gate 6 — deployed; live acceptance 1–7 recorded

## Closure (fill on completion)
