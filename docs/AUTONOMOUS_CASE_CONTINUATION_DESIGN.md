# Autonomous Case Continuation (M3.4) — Design

**Authored:** 2026-07-28 (closing design session `8d3dc68a35ec`, 2026-07-25).
**Status:** Reference doctrine (durable) — *implementation-ready design, not yet built*.
**Owner surface:** this doc holds the **M3.4 design detail** (state machine, delta, acceptance
test). The ≤15-line milestone contract lives in `docs/Task_Harness_v0.7_AUTOMATION.md` §M3.4.
The prior-art / boundary doctrine that motivated it lives in
`docs/PERSISTENT_MANAGER_LOOP_ANALYSIS.md` (§0–§4 valid; its §5–§7 recommendations are
**superseded by this doc** — see that file's banner).

> **Provenance.** This is the converged output of a five-turn design session. The session
> *started* from the framing in `PERSISTENT_MANAGER_LOOP_ANALYSIS.md` and then materially
> revised three of its conclusions on evidence. Where the two disagree, **this document wins.**
> The revisions were: (1) a durable scheduler is **not** SDK duplication — the SDK agent loop is
> client-driven and non-durable, with no server-side session, so cross-turn/cross-crash
> continuation is genuinely ours to build; (2) M4's task-graph is **HYBRID, not de-scoped**
> (parked spike, §7); (3) the naïve `task.finished → inject Manager turn` callback is **replaced**
> by a condition-gated, coalesced, at-least-once, leased re-entry built on the existing
> `mesh_tasks` claim/incarnation mechanism.

---

## 0. Frozen product contract

> **One operator invocation starts one bounded Case. The harness then continues that Case
> autonomously across worker completions, Manager turns, context resets and process restarts
> until verified closure (`close_case`), explicit escalation (`flow.blocked`) or a configured
> limit (`flow.interrupted`). It does not independently invent or start unrelated Cases.**

This stays **inside** the `production_vision.md` §6 / v0.7 §0.2 anti-goal ("no standing
autonomous process"): continuation is *bounded by the one operator invocation*, never
self-igniting. Fully-unattended / self-igniting / always-on remains a separate operator fork,
out of scope here.

---

## 1. Ownership boundary (corrected)

The earlier framing — "any coded outer control duplicates the SDK" — was **wrong**. The SDK's
agent loop is client-driven and non-durable: session state is local JSONL under
`~/.claude/projects/`, resume replays local files (not server state), and a workflow in flight
during a crash restarts fresh. **There is no server-side durable session.** So a durable
scheduler is not duplication; the SDK cannot provide it. The line is **coarse vs. fine** and
**durable vs. ephemeral**:

| Layer | Owner | Why |
|---|---|---|
| Inner model↔tool loop — one turn: reason, call tools, verify, stop | **Claude Agent SDK** | `ClaudeSDKClient` persistent stream (`claude_driver.py:503-528`); ADR-0001. Do not rebuild. |
| Fine-grained execution graph inside one task — parallel subagents, branching, loop-until-done | **Dynamic Workflows / SDK subagents** (hybrid, §7) | First-class `Workflow`/`agent()`/`pipeline()` runtime, headless via SDK. But ephemeral: resumable only within the same live session; restarts fresh on crash. |
| **Durable coarse-grained Case graph** — objective, `completion_criteria`, dispatch lineage, review verdicts, budget, closure | **Our harness** | `flow_runs`/`flow_links`/`flow_events` (migrations 21–24) — the only crash-durable record. |
| **Autonomous continuation = durable scheduler + control loop** — persist a pending wake, dedupe, lease to one invocation, re-enter across turns/crashes, enforce budget, ack | **Our harness** (the SDK has no equivalent) | No server-side session store; wake intent (`worker.wait_pending`) is durable but nothing acts on it — `reconcile_waits` is pull-only, no background consumer exists. |
| Final closure authority / escalation / permissions / budget ceilings | **Our harness**, enforcing via SDK primitives (`max_turns`, `PreToolUse` deny hook) | Today no round/cost governor runs on any Manager/worker (`max_turns` echoed in health only, never passed to `ClaudeAgentOptions`). |

**Sharpening:** "autonomous continuation" is *not* a property that emerges from the Case graph —
it is a **distinct durable-scheduler component**, and it is the load-bearing gap. The SDK owns
the turn; **we own when the next turn happens and whether it is allowed to.**

---

## 2. Can `mesh_tasks` represent the continuation job? — Yes, no new table/columns

The continuation is a **scheduling token**, not a coding task. Every primitive already exists:

- **One row per (Case, generation), idempotent:** deterministic `id = "cont:{case_id}:{gen}"`.
  `enqueue_task` (`db.py:1008`) catches `UNIQUE constraint failed: mesh_tasks.id` and no-ops
  (`:1044-1047`) → concurrent enqueues collapse to exactly one row.
- **Structurally excluded from the worker claim scan (load-bearing):** the worker scan filters
  `WHERE status='pending' AND (machine_id IS NULL OR machine_id = ?)` (`db.py:1400-1406`). Pin
  `machine_id` to a reserved **sentinel** that equals no real `node_id` → invisible to every
  worker/embedded-pool claim regardless of backend filter. Set `action='manager_continuation'`
  as the Wake-Dispatcher's discriminator.
- **Atomic claim / retry / stale recovery — reused verbatim:** `claim_task(cont_id, gateway_node_id)`
  is `UPDATE … WHERE status='pending'` + `SELECT changes()>0` (`db.py:1103-1119`) → single winner.
  Gateway host is a registered node, so `claimer_incarnation` is stamped; on restart
  `release_node_claims` (`:1147`) + `list_stale_claims` (`:1246`) reap a claim held by a dead
  incarnation back to `pending`.
- **Consumed watermark — reused `result` column:** the harness writes
  `result = {generation, consumed_task_ids}` on completion. No new column, no new event type.

**Net new persistent state: zero tables, zero columns.** One reserved `machine_id`/`action`
sentinel pair, one enriched `worker.wait_pending` payload, one background loop.

---

## 3. Minimal durable state machine — four distinct states

Wait-group state is **derived** from the append-only `flow_events` ledger, not stored. The only
enriched write is the `worker.wait_pending` **payload** (`payload_json` is already unconstrained,
`db.py:138-139, 2078-2107`):

```
worker.wait_pending payload += { wait_group_id, condition: ANY|ALL|named[task_ids], member_task_ids[] }
```

Four states are kept strictly distinct (the T5 correction — do **not** overload
`worker.wait_resolved` as both semantic resolution and transport ack):

```
State 1  SATISFIED     Computed from flow_events, not stored. Per Case C, per ARMED group,
                       condition true over {task.finished(member)} MINUS the consumed watermark
                       (⋃ result.consumed_task_ids of completed cont rows). No row yet.
                         condition(G): ANY → ≥1 unconsumed member finished
                                       ALL → every member finished
                                       named → the named subset finished

State 2  SCHEDULED /   Dispatcher tick, on SATISFIED at generation N:
         CLAIMED         a. enqueue_task(id="cont:{C}:{N}", machine_id=SENTINEL,
                            action="manager_continuation",
                            payload={case_id:C, generation:N,
                                     presented_task_ids:[finished − consumed]})   ← idempotent (UNIQUE id)
                         b. claim_task("cont:{C}:{N}", gateway_node_id)           ← ATOMIC single winner
                            (changes()>0). A racing dispatcher gets False and stops. No delivery.

State 3  TURN STARTED  Winner delivers ONE coalesced proactive turn (all presented_task_ids,
                       across every group satisfied this tick) to the bound Manager session;
                       session goes BUSY. Row stays 'claimed'.

State 4  COMPLETED +   When the Manager-turn future returns, the HARNESS (orchestrator, not the
         CONSUMED       LLM) records consumption: row status='completed',
                        result={generation:N, consumed_task_ids:[...]}. This is the transport
                        ACK and advances the watermark. Round N is now counted.
```

**Coalescing.** `DELIVER` presents the FULL set of finished-unconsumed members (across every
group satisfied this tick) as ONE Case-level turn — not one turn per worker. Several completions
before the Manager runs collapse into a single wake. Coalescing many groups into one tick = one
row = **one round**.

**Lease.** The atomic lease is `claim_task` on the deterministic continuation row — **not** the
Case→Manager binding + IDLE check (that is ownership + observation, and two dispatchers can both
observe IDLE and both deliver — the hole T5 closed). At most one Manager turn in flight per Case.

**Round count.** Number of continuation rows for the Case (= highest generation), compared to a
cap carried in `completion_criteria`. **Count continuation turns, not `worker.wait_resolved`
events.** No counter column.

**Recovery (at-least-once, safe duplicates).** A crash between State 2b and State 4 leaves the
row `claimed` by a dead incarnation → reaped to `pending` → re-claimed next tick → **redelivered**.
Because consumption was never written, `presented_task_ids` recompute identically and the
Manager's idempotent effects (`record_review`/`dispatch` keyed by task; `close_case` guarded)
absorb the repeat. **Redelivery is impossible while the claim is live** (`status='claimed'` blocks
re-claim); it happens ONLY after ownership is released or its incarnation expires — never
concurrently.

**Termination.** `close_case` (criteria met) → `flow.closed` | escalate → `flow.blocked` |
rounds ≥ cap → `flow.interrupted` + operator escalation. All three already in `FLOW_EVENT_TYPES`
(`db.py:131-152`).

---

## 4. Watermark / generation — which completions a turn consumed

- **Generation `N`** — monotonic per Case, encoded in `cont:{case_id}:{N}`. Round count = highest
  generation. Coalescing many groups into one tick = one row = one round.
- **`payload.presented_task_ids`** — the finished-but-unconsumed members visible at claim time
  (what the turn is shown).
- **`result.consumed_task_ids`** (harness-written, State 4) — authoritative record of exactly what
  generation `N` consumed.
- **Watermark** = `⋃ result.consumed_task_ids` over all **completed** continuation rows of the
  Case. Next-satisfaction is checked over `{task.finished} − watermark`. An in-flight (claimed,
  not completed) row contributes nothing to the watermark — which is exactly why a crash
  redelivers rather than drops.

`worker.wait_resolved` is **removed from the transport role**; it stays purely semantic (a group's
obligation discharged, harness-written) — never the turn ack. The ack is State 4's `mesh_tasks`
completion.

---

## 5. Wait-condition semantics — ANY / ALL / named

- **ALL(members)** — one-shot, level-triggered. Satisfied only when *every* member has
  `task.finished`. Fires once; on consumption the harness writes `worker.wait_resolved(group)` and
  the group is done. No members remain attached.
- **named(task_ids)** — identical to ALL over an explicit subset (ALL == named-over-all-dispatched).
  One-shot; resolved on consumption.
- **ANY(members)** — **edge-triggered, repeating, NOT one-shot, members stay attached.** Satisfied
  as soon as ≥1 member has an *unconsumed* `task.finished`. The continuation coalesces and presents
  **all** currently-finished-unconsumed members, then consumes them (watermark advances). Unfinished
  members **remain attached**. A later completion is a new unconsumed `task.finished` → re-satisfies
  ANY → schedules generation `N+1` → another wake presenting just the newly-finished member(s). The
  group retires only when (a) all members are finished-and-consumed (converges to
  `worker.wait_resolved`), (b) the Manager cancels it, or (c) the Case closes. So ANY = "wake me on
  each new completion from this group, coalescing simultaneous ones, until drained or cancelled."

Round cap (from `completion_criteria`): when the next generation would exceed the cap, the
dispatcher emits `flow.interrupted` + operator escalation instead of scheduling — for ALL, named
and ANY alike.

---

## 6. Repository delta

| Disposition | Item | Evidence / rationale |
|---|---|---|
| **REUSE** | `flow_events` ledger + `worker.wait_pending`/`worker.wait_resolved` vocab | `db.py:138-139`; `record_worker_wait`/`reconcile_worker_waits` (`:2133`/`:2174`). Wait state is derivable. |
| **REUSE** | `flow.closed` / `flow.blocked` / `flow.interrupted` event types | Already in `FLOW_EVENT_TYPES` (`db.py:131-152`). |
| **REUSE** | `mesh_tasks` row as the continuation job + atomic claim/incarnation/reaper | `enqueue_task` (`:1008`), `claim_task` (`:1103`), `list_stale_claims` (`:1246`). The lease, dedupe and crash-recovery come free. |
| **REUSE** | `mesh_tasks.result` column as the consumed watermark | `result = {generation, consumed_task_ids}`. No new column. |
| **REUSE** | `completion_criteria` column as the round-cap home | Migration 24 JSON contract; a round/cost cap is a sibling termination criterion. No `flow_runs` budget columns. |
| **REUSE** | Interval reconcile-loop scaffolding | `_start_stale_busy_reconciler` (`orchestrator.py:620`) is the Wake-Dispatcher template. |
| **MODIFY** | `dispatch_worker` / `record_worker_wait` payload | Add `{wait_group_id, condition, member_task_ids}` to the existing `worker.wait_pending` write (`db.py:2133`). |
| **MODIFY** | Manager arming surface (`dispatch_worker` tool + role prompt) | Let the Manager declare a group condition (default ANY for a single worker). |
| **ADD** | Reserved `machine_id` sentinel + `action='manager_continuation'` discriminator | Keeps the continuation row invisible to the worker claim scan (`db.py:1400-1406`). |
| **ADD** | Wake-Dispatcher evaluator (the re-entry engine) | New loop: satisfy → coalesce → enqueue `cont:{C}:{N}` → atomic `claim_task` → deliver → harness-record consumption → charge round. Flag-gated, default OFF. |
| **ADD** | Round-count-vs-cap check + `flow.interrupted` emit on exhaustion | Count = highest continuation generation; cap from `completion_criteria`. |
| **AVOID** | New wake/queue table, new `flow_runs` budget/counter columns | Continuation = a `mesh_tasks` row; wait state derivable; cap in `completion_criteria`. No parallel state model. |
| **AVOID** | Cross-crash exactly-once lock | Contract is **at-least-once + idempotent ack**, not exactly-once. |
| **AVOID** | Per-worker turn injection / wake-on-every-`task.finished` | Re-entry is **condition-gated and coalesced**. |
| **AVOID** | `worker.wait_resolved` as the transport ack | Ack is the `mesh_tasks` State-4 completion; `wait_resolved` stays semantic. |

---

## 7. Implementation order (resolves the live-vs-crash dependency)

1. **Job 1 — Live-session re-entry.** Wait-condition + coalesced Case-level wake + atomic
   `mesh_tasks` continuation lease + harness-recorded consumption + round cap. Handles what the
   SDK can't: continuing a Case whose Manager session is **live and idle**. No reconstruction, no
   respawn. **(Full dispatch below.)**
2. **Job 2 — Durable Case reconstruction.** `get_case_brief` single-call state (objective +
   `completion_criteria` + budget/rounds + dispatched workers via `flow_links` + latest verdict
   per worker + open/ready waits) from the DB alone (today `get_case` returns
   objective+criteria+stage only, `mcp_manager.py:571`); auto-`reconcile_waits` at role-boot. The
   prerequisite that makes respawn *safe*.
3. **Job 3 — Crash-respawn.** Dispatcher path when the Manager session is dead: reconstruct via
   Job 2, re-arm waits, resume; reuse the `mesh_tasks` incarnation/reaper for one active
   invocation. **Depends on Job 2** — a crashed Manager must not be respawned until durable
   reconstruction exists. Until Job 3, a tick that finds the session dead does nothing.

**Parked (later integration spike, not in this sequence): M4 task-graph — HYBRID.** Keep the
durable coarse Case graph as owner of objective/state/budget/closure; **replace M4's hand-rolled
task-DAG *executor*** with Dynamic Workflows / SDK subagents for parallel decomposition inside a
selected task, whose only durable footprint is one `task_attached` node + a synthesized result +
one `review.*` verdict. **Gated on a prerequisite spike:** (a) confirm the **Python**
`claude_agent_sdk` exposes the `Workflow`/`Task` tool (docs cite TS only); the Manager's
`ClaudeAgentOptions` (`claude_driver.py:491-501`) passes no `Task`/`Workflow`/`agents=` today, and
its grant is Read/Edit/Bash + manager MCP only (`claude_role_adapter.py:24-36`) — enabling it is an
allowlist add; (b) the account's workflow `/config` toggle; (c) a `PreToolUse` hook + git-worktree
to contain workflow subagents' `acceptEdits` auto-approval (`AgentDefinition.tools` is
under-enforced — SDK #172/#189). If (a) fails, fall back to SDK **subagents** (`agents=`/
`AgentDefinition`), Python-confirmed. **Do not de-scope M4; re-scope it to "invoke, don't build,"
and gate on this spike.** Not started until Jobs 1–3 land.

---

## 8. Job 1 — implementation dispatch (ready to hand to a worker)

```
TASK        Add condition-gated, coalesced, at-least-once Case re-entry for a LIVE+IDLE Manager.
            When a Manager-owned wait condition (ANY|ALL|named) over a dispatch group becomes
            satisfied, schedule ONE deterministic mesh_tasks continuation row, atomically claim
            it, deliver ONE Case-level wake turn presenting all newly-finished-unconsumed members;
            the HARNESS records consumption on turn return; enforce a round cap.
TYPE        code (feat branch + PR) — flag-gated CASE_CONTINUATION_ENABLED, default OFF ⇒ byte-identical.
CONTEXT     Reuse only: flow_events (worker.wait_pending/resolved), completion_criteria as cap
            home, mesh_tasks row (id="cont:{case}:{gen}", machine_id=SENTINEL,
            action="manager_continuation") for the atomic lease via claim_task + result watermark,
            the _stale_busy_reconciler interval loop (orchestrator.py:620) as the dispatcher
            template. NO new table, NO new columns. worker.wait_resolved is semantic ONLY — the
            transport ack is the mesh_tasks completion.
CHANGES     (a) record_worker_wait payload += {wait_group_id, condition, member_task_ids}
                (db.py:2133); dispatch_worker declares the group (default ANY, single worker).
            (b) Wake-Dispatcher tick: for each open Case, for each ARMED group, if SATISFIED over
                {task.finished} − watermark → enqueue cont:{C}:{N} (idempotent) → claim_task
                (atomic single winner) → deliver ONE coalesced proactive turn (presented_task_ids
                across all groups satisfied this tick) to the bound LIVE+IDLE session; else skip.
            (c) On Manager-turn return, HARNESS sets the row completed with
                result={generation, consumed_task_ids} (advances watermark, counts round N).
            (d) rounds = highest continuation generation; cap from completion_criteria; on
                exhaustion emit flow.interrupted + operator escalation, do NOT schedule.
ACCEPTANCE  (single e2e, cheap — TestClient + real MeshDB + FAKE claude backend; gateway host node
            registered for incarnation; NEVER the paid CLI). Flags HARNESS_FLOW_DRIVE +
            MANAGER_ROLE_ENABLED + DURABLE_RELAY_ENABLED + CASE_CONTINUATION_ENABLED ON.
            open_case(round_cap=2); Manager dispatches t1,t2,t3 as one group, condition=ALL (gen 1).
            1. Not-yet-satisfied: finish t1,t2; tick → assert NO cont:{C}:1 row (ALL unsatisfied).
            2. Satisfy → one atomic claim: finish t3; tick → assert exactly one row cont:{C}:1
               pending→claimed (claimed_by=gateway node); a second claim_task("cont:{C}:1",…)
               returns False; exactly ONE coalesced turn, presented_task_ids=={t1,t2,t3}; round==1.
            3. No concurrent duplicate under live ownership: while the turn is in flight (row
               claimed, session BUSY), another tick → NO new row, no second delivery, re-claim False.
            4. Harness-recorded consumption (not the LLM): on turn return assert the HARNESS set
               cont:{C}:1 → completed, result.consumed_task_ids=={t1,t2,t3}; watermark covers all
               three; a later tick delivers nothing.
            5. Redelivery only after released ownership (crash): re-arm a fresh group, satisfy it
               (gen 2); after claim but BEFORE consumption, bump the gateway node incarnation; run
               the reaper → row returns to pending; next tick re-claims and REDELIVERS the same
               coalesced set (at-least-once). Assert redelivery strictly after ownership release,
               never while claimed.
            6. Round cap → escalation: the gen-2 attempt is at round_cap=2; a further satisfied
               condition (gen 3) → assert flow.interrupted + operator escalation instead of
               scheduling. Verify all via /api/work/{id}/timeline + mesh_tasks rows.
SCOPE OUT   No get_case_brief, no dead-session respawn, no reconstruction (Jobs 2/3). No subagents
            / Dynamic Workflows.
```

---

## 9. Cross-references

- Motivating boundary/prior-art doctrine (T1 framing; §5–7 superseded here):
  `docs/PERSISTENT_MANAGER_LOOP_ANALYSIS.md`.
- Milestone contract: `docs/Task_Harness_v0.7_AUTOMATION.md` §M3.4 (this doc is its detail).
- Adjacent milestone: v0.7 §M3.3 (durable, case-aware relay) — M3.4 is the condition-gated,
  coalesced, leased re-entry layer that consumes M3.3's durable relay.
- Manager tool surface: `scripts/mcp_manager.py`. Continuation substrate: `src/control/db.py`
  (`mesh_tasks` claim/reaper, `flow_events` wait markers). Dispatcher template:
  `src/orchestrator.py:620`.

> **Code anchors are file:line as of 2026-07-25 — verify before citing; line numbers drift.**

---

## 10. Adjacent plane: the git dispatch registry (synergy verdict, 2026-07-28)

A separate instrument — the portable **`dispatch-state-kit`** (`/home/cifran/dev/dispatch-state-kit/`,
being ported into this repo as **A51**) — makes dispatch **drops** machine-tracked: each
`.ai/dispatch/<job>.md` carries a ` ```yaml ` state block (`status`, canonical `created_at`,
`depends_on`, `evidence`, `results_ref`), rendered to generated views, gap/proof-audited, and kept
in **git**. Because it lives in git, it is also how the *same project on other machines* stays in
parity — GitHub mediates the drop set. The question this section settles: does that plane help,
obstruct, or need folding into the M3.4 continuation substrate designed above?

**Verdict: two distinct planes, complementary, kept separate — do NOT fold, do NOT DB-back.**

| | Dispatch registry (kit) | Case-continuation substrate (this doc) |
|---|---|---|
| **Plane** | Plan / authoring / audit | Runtime execution |
| **Truth** | `.ai/dispatch/*.md` yaml blocks — **git-canonical**, human/agent-authored | `flow_runs`/`flow_links`/`flow_events` + `mesh_tasks` — **DB-canonical**, gateway-written |
| **Tempo** | Authoring-time & audit-time; diffable, offline, durable across repos | Event-driven, in-process, per-tick |
| **Cross-machine parity** | GitHub (pull/merge the drop set) | Mesh dispatch (machine-to-machine); SQLite is single-writer-authoritative |
| **Lifecycle vocab** | `ready→active→blocked→done→dead` | `open → … → closed / blocked / interrupted` |
| **Answers** | "what work exists, its state, is 'done' proven" | "drive THIS bounded Case to closure now" |

**Why NOT port dispatch rows into the DB / into the continuation scheduler:**
1. **It would breach the frozen §0 contract.** A git-synced registry that the gateway *reads and
   auto-starts* becomes a standing work-puller that "independently invents or starts Cases" — the
   exact `production_vision.md` §6 / v0.7 §0.2 anti-goal. The kit must stay **authoring/audit only**;
   it must never be an ignition source.
2. **Two parity mechanisms fighting over one truth = the duplicate-ledger failure mode** the M2
   milestone already warned against (F4). Git-parity for *plans* and mesh/DB-authority for
   *execution* are orthogonal; unifying them recreates the drift, not less of it.
3. **The kit's whole value is its git-mediation** — durable, diffable, offline, cross-repo-portable
   (it was built to be injected into ANY project). DB-backing it throws that away for nothing M3.4
   needs.

**The seam (soft, one-directional):** a drop's `evidence:` / `results_ref:` **may point at** the
runtime artifact it produced (a Case id, a `flow_events` timeline, a merged PR/commit). That is a
reference, not a shared state machine. The registry records *that a drop was executed and proven*;
the substrate records *how a Case ran*. Neither reads the other's status to make a decision.

**Net effect on the M3.4 plan: none — it neither advances nor blocks it.** It is pure upside on its
own plane: it replaces the rotting prose `DISPATCH_LOG` (which had drifted ~16 PRs behind git) with
an honest, proof-gated board. A *reliable* dispatch board is, longer-term, a legitimate **input** to
a Manager choosing the next unblocked drop — but that selection stays an operator-invoked, bounded
act, never an autonomous pull. Keep the two planes separate; let them meet only at the evidence seam.
