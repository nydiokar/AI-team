# v0.7 Automation — Spec Completion Plan (the remaining, ordered backlog)

**Authored:** 2026-07-30. **Purpose:** the single, dependency-ordered list of the work still
required to **exhaust `docs/Task_Harness_v0.7_AUTOMATION.md`** and reach the desired end-state.
A Manager told only "continue the project" should pull the **lowest-numbered UNBLOCKED** task
here and drive it to a merged PR. Each task is scoped so a pickup is self-contained — no
half-implemented, parallel, or duplicated machinery.

> This is a *plan* doc (DOC_MAP: planning plane). The milestone contract is v0.7 §M3.3/§M3.4/§M4;
> the M3.4 design detail is `AUTONOMOUS_CASE_CONTINUATION_DESIGN.md`. Where they disagree on
> mechanics, the design doc wins; this doc only sequences and scopes.

---

## 0. The ultimate, specific goal ("desired state" — when the spec is DONE)

**One operator invocation of a Manager drives one bounded objective to verified closure,
hands-off.** Concretely, the spec is finished when a Manager — invoked once with an intent —
will, without further human poking:

1. orient → expand the intent into a scoped, not-overstated objective + plan;
2. (feature-sized only) author a spec and pass a rubric-scored adversarial review before decomposing;
3. open **ONE** Case, dispatch worker(s) **into it**, and record the lineage;
4. **arm a wait condition and return control** — never block-poll — talking to the operator freely
   while workers run;
5. be **woken on each completion** (coalesced), review the committed diff adversarially, record a
   `review.*` verdict, and dispatch the next task / wait / close;
6. **survive** context resets and process restarts — resume the same Case from the DB alone;
7. be **bounded** — round/turn/cost caps and a kill path escalate instead of running away;
8. **close the Case itself** as the sole authoritative closer when `completion_criteria` are met.

…with every stage/dispatch/verdict a queryable row, **every flag OFF ⇒ byte-identical legacy**,
and **never inventing or starting an unrelated Case** (`production_vision.md` §6 / v0.7 §0.2
anti-goal). "Done" test: a Manager given "continue" runs a full bounded Case end-to-end across at
least one crash + one context reset with zero operator turns beyond the initial intent.

**Merged so far:** M0, M1, M2, M2.5, M3.1, M3.2, M3.3 (durable-relay half), **M3.4 Job 1**.
**Remaining:** the tasks below.

---

## 1. Ordered backlog

Dependency-ordered. `⛔ blocked-by` names the hard prerequisite; items with the same rank and no
cross-dependency may proceed in parallel **only by different loops** (never split one task's
machinery across loops).

### T1 — M3.4 Job 1 **adoption + activation** (make the engine actually used)  ·  ⛔ none (Job 1 merged)
**Why:** the Wake-Dispatcher exists but is inert — the Manager role still block-polls and the flag
is OFF, so the "free-for-convo, woken-on-completion" benefit is not realized. This is the task that
*delivers* the wait_for_worker fix.
**Scope (in):**
- Rewrite the "Waiting on a batch" guidance in `docs/harness/roles/manager.md` + the `Next:` hints
  in `scripts/mcp_manager.py::_dispatch_worker`: after dispatching a batch, **`arm_wait_group` and
  return control**; the harness re-enters you on completion. Demote `wait_for_worker` to a
  last-resort single synchronous wait, not the default.
- Thread a `round_cap` argument through the `open_case` MCP tool → control route → `db.open_case`
  (encode as the `completion_criteria` JSON `{"round_cap": N}` the engine already reads), so a
  Manager can set the cap without hand-crafting JSON.
- Operator activation runbook: set `CASE_CONTINUATION_ENABLED=1` (+ siblings) and
  `pm2 restart ai-team-gateway`.
**Scope (out):** no engine changes (Job 1 is done); no reconstruction/respawn.
**Acceptance:** unit — `open_case(round_cap=2)` round-trips to `case_round_cap==2`; role-doc/hint
tests updated. **Live (operator-gated):** a Manager dispatches N workers, arms an ANY group, returns,
converses with the operator, and is re-entered with a coalesced review turn per completion — no
blocking poll, no interruption of a live operator turn.
**Files:** `docs/harness/roles/manager.md`, `scripts/mcp_manager.py`, `src/control/control_api.py`,
`src/control/db.py` (open_case round_cap only), tests.

### T2 — M3.3 completion: **per-invocation turn/cost governor + kill path**  ·  ⛔ none
**Why:** M3.3's milestone named "round/turn/cost caps; kill path → `flow.interrupted`" but only the
durable relay shipped. `ClaudeAgentOptions` passes **no `max_turns`**; no cost ceiling or kill path
runs on any Manager/worker. A bounded *autonomous* loop is unsafe to run long without this — it is a
prerequisite for trusting T3's longer unattended runs.
**Scope (in):**
- Pass `max_turns` (from config / `completion_criteria`) into `ClaudeAgentOptions`
  (`claude_driver.py:537`) for Manager + worker sessions; surface the effective cap in health.
- A kill path: an operator/programmatic stop → `flow.interrupted` → Case `status=blocked` (resumable),
  workers cancelled, escalation emitted. Reuse the existing cancel/`stop` plumbing; do not invent a
  second one.
- Enforce a turn/cost cap distinct from M3.4's *round* cap (rounds = continuation generations; turns =
  SDK turns within a session). Exhaustion → `flow.interrupted` + escalation, not silent stall.
**Scope (out):** the round cap (already in M3.4 Job 1); no new budget columns (reuse
`completion_criteria` / config, per design §6 AVOID).
**Acceptance:** e2e (fake backend) — a session exceeding its turn cap halts with `flow.interrupted`
+ escalation; a kill request blocks (not closes) the Case and leaves it resumable; flag/config OFF ⇒
byte-identical.
**Files:** `src/backends/claude_driver.py`, `src/orchestrator.py`, `src/control/db.py`, tests.

### T3 — M3.4 Job 2: **durable Case reconstruction (`get_case_brief`) + auto-reconcile at boot**  ·  ⛔ T1 activation
**Why:** design §7 job 2 — the prerequisite that makes crash-respawn *safe*. Today `get_case`
returns objective+criteria+stage only.
**Scope (in):**
- `get_case_brief(case_id)`: one DB-only call returning objective + `completion_criteria` +
  round/turn caps + rounds-used + dispatched workers (via `flow_links`) + latest `review.*` verdict
  per worker + open/ready waits + **armed wait-groups** (from the M3.4 markers).
- At Manager role-boot, auto-run `reconcile_waits` **and re-arm live wait-groups** so a resumed
  Manager wakes with its full obligation set, not lost memory.
**Scope (out):** no respawn of a dead session (that is T4); no new tables.
**Acceptance:** e2e (fake backend) — boot a Manager on an existing open Case with in-flight workers
+ armed groups; assert full state reconstructed from the DB alone and waits/groups re-armed.
**Files:** `src/control/db.py`, `scripts/mcp_manager.py` (get_case_brief tool + boot hook),
`src/orchestrator.py`, tests.

### T4 — M3.4 Job 3: **crash-respawn dispatcher path**  ·  ⛔ T3
**Why:** design §7 job 3 — until this, a Wake-Dispatcher tick that finds the bound Manager session
**dead** does nothing (the current, intended Job-1 behavior). This closes the "survive a process
restart" clause of the goal.
**Scope (in):**
- When a tick finds the Case's Manager session dead, reconstruct via `get_case_brief` (T3), re-arm
  waits/groups, and resume **one** active invocation (reuse the `mesh_tasks` incarnation/reaper for
  single-flight — no second lock model).
- Guard the §0.2 anti-goal: respawn only continues the SAME bounded Case; it never starts new work.
**Scope (out):** M4 task-graph; subagents.
**Acceptance:** e2e (fake backend) — kill the Manager session mid-Case; assert exactly one role-full
Manager is respawned, resumes the same Case, and drives it to close; no duplicate respawn under a
racing tick.
**Files:** `src/orchestrator.py`, `src/control/db.py`, tests.

### T5 — M4: **feature-spec authoring + scored review + decomposer-as-task-DAG**  ·  ⛔ none for generators; wiring ⛔ T1
**Why:** v0.7 §M4 — the front-end for a *feature-sized* intent (goal step 2). Currently absent (no
`spec_authoring` stage, no `publish_artifact`, no decomposer).
**⚠️ Not greenfield — an abandoned capability, read before building:** the v0.5 harness already had a
proven intent-expansion contract (`docs/harness/generators/draft_packet.md`'s literal-vs-interpreted-
vs-real-objective split + forced assumptions/drift-risks) and a stalled attempt to extend it to
feature-sized intents (`AGENT_24_DECOMPOSER_GENERATOR.md`, deferred 2026-07-08, never resumed even
though its blocker — M2.5 — has been live since mid-July). The live Manager today gets a flat
`objective: str` with none of that structure. Full trail + an open "should this be the Manager or a
separate role" fork: see the addendum in `AGENT_56_M4_SPEC_AUTHORING_DECOMPOSER.md`.
**Scope (in):**
- `spec_authoring` stage before LOOP 0: Manager authors a spec + runs a **rubric-scored adversarial
  review** (a cheap/cross-model plan-reviewer seat, per M3.2) before decomposing.
- `publish_artifact` → `artifact` `flow_links`+events.
- Decomposer: an expanded objective → `open_case()` + N `task_attached` links forming a **task-DAG
  inside ONE Case** (edges on `flow_links.metadata_json`) — **not** N orphan flow_runs.
**Scope (out):** the intra-task parallel *executor* — that is the parked hybrid spike (T6). Design
the DAG as data only; do not build a bespoke executor.
**Acceptance:** e2e (fake backend) — a feature intent produces a reviewed spec artifact + one Case
holding a task-DAG (dependency edges present); a low-scored spec blocks decomposition.
**Files:** `src/orchestrator.py`, `docs/harness/` generators, `scripts/mcp_manager.py`, `src/control/db.py`, tests.

### T6 — M4 **hybrid-executor spike** (invoke, don't build)  ·  ⛔ T4  ·  SPIKE, gated
**Why:** design §7 "parked" — decide whether the intra-task parallel executor is SDK Dynamic
Workflows / subagents rather than a hand-rolled DAG executor.
**Scope (in):** confirm the **Python** `claude_agent_sdk` exposes `Workflow`/`Task` (docs cite TS
only); the account workflow toggle; a `PreToolUse` hook + git-worktree to contain subagent
auto-approval. If the Workflow tool is absent, fall back to SDK **subagents** (`agents=`/
`AgentDefinition`). **Output: a go/no-go + chosen mechanism, not a full build.**
**Scope (out):** shipping the executor (a follow-on task decided by the spike).
**Acceptance:** a written verdict with the confirmed mechanism + the containment design; a minimal
proof that one subagent runs contained in a worktree.

---

## 2. Live validations (operator-gated / paid) — required to DECLARE the spec achieved

These are not build tasks; they are the proof that the merged machinery works end-to-end. The spec
is not "achieved" on green unit tests alone (a green layer can be inert — see the repo's own scars).

- **V1 — M3.4 Job 1 live proof:** flag on + restart; a real Manager arms a group, returns, is woken.
- **V2 — M3.3 durable-relay live e2e:** flag-on marker→crash→reconcile through the running gateway (still un-run).
- **V3 — Node re-run of A43:** a role-full, tool-full Manager booted on a real node (not the gateway host).
- **V4 — Whole-loop hands-off acceptance (the goal test):** one real feature intent → traceable
  dispatch → reviewed diff → autonomous continuation across ≥1 crash + ≥1 context reset → authoritative
  close, all queryable, zero operator turns beyond the intent.

---

## 3. Out of scope of THIS spec (do not conflate)

- **A51 dispatch-state-kit migration** is a **separate plane** (plan/authoring/audit; design §10). It
  neither advances nor blocks the automation spec. Track it on its own; do not fold it into the
  continuation substrate or DB.
- Always-on / self-igniting / multi-project autonomous swarms — **anti-goal** (v0.7 §0.2). Nothing
  above crosses the one-operator-invocation bound.

---

## 4. "Just continue" quick-map

Lowest UNBLOCKED wins: **T1 → T2 → T3 → T4 → T5 (generators can precede) → T6.** T2 has no hard
dependency and may run alongside T1 (different loop). Validations V1–V4 are pulled as their build
tasks land and the operator opens the gate. When T1–T6 are merged and V1–V4 pass, v0.7 is exhausted
and the desired end-state (§0) is reached.
