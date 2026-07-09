# M3 — Manager-as-Invoked-Role: Specification & Backend-Readiness Dossier

**Status:** DRAFT spec (2026-07-09). Authored at "M3 spec-time" as
`Task_Harness_v0.6_AUTOMATION.md` §5.2 requires (guardrails + role separation are written
*here*, after the F4 capability audit — not before). Nothing in this doc is built yet; it
is the bounded plan the first M3 dispatch executes against.

**Reads with:** `docs/Task_Harness_v0.6_AUTOMATION.md` (§1 target, §3 M3, §4 F4, §5
guardrails) · `docs/harness/manager_invocation.md` (the role prompt this promotes to a
gateway role) · `docs/harness/FLOW_MAP.md` · `docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`
(the now-shipped substrate M3 hangs off).

---

## 0. Where we are (why this is the next logical step)

The v0.6 ladder is **M0 → M1 → M2 → M3 → M4**. As of 2026-07-09:

- **M0 (base reconcile)** — DONE.
- **M1 (flow-state machine + `/api/flows`)** — LIVE.
- **M2 (dispatch lineage + Work Control Substrate A25–A30)** — **DONE & merged to `main`
  (`24dff9b`)**. `flow_links`, append-only `flow_events`, the write-path seams, the read
  model, the mobile Work surface, and honest session affiliations all shipped.
  `HARNESS_FLOW_DRIVE` is **ON in the live environment** (gateway restarted on the merged
  code), so the substrate now populates from real execution — the observability M3 needs to
  see its own dispatches *already exists and is live*.
- **M3 (this doc)** — NOT YET. The roadmap's own ordering rule (§4 F3): never build a thing
  that *produces* dispatches autonomously before you can *observe* them. We can now observe
  them (M2 is live). **So M3 is unblocked and is the correct next milestone.**
- **M4 (spec authoring + scored review)** — its *generators* are docs and can land any time;
  its *stage-wiring* waits on M3.

---

## 1. Target (bounded — the anti-goal fence stays up)

One **operator intent** → one gateway-spawned **Manager session** that:

1. **Orients & grounds** — reads project context + the actual code/git before touching
   anything (the anti-duplication scar; `manager_invocation.md` rule 1).
2. **Scope-locks** — expands the intent into a professional, *not-overstated* objective-lock +
   plan; writes a `flow_runs` row (the case).
3. **Dispatches worker(s)** — as **separate gateway sessions** (NOT sub-agents — see §3),
   through the existing dispatch path, with parent→child lineage persisted (M2).
4. **Reviews from above** — verifies the worker's committed diff *adversarially and from a
   higher vantage than the worker* (§4), in git, against project memory/scars.
5. **Decides** iterate / close / derive; **updates the durable log + the case**; and
   **nudges the worker to the next task** — preferring to continue an existing session over
   spawning a new one when the token math favors it (§5).

**Load-bearing bound (v0.6 §0.2):** every capability is bounded by the *one operator
invocation*. No standing loop, no always-on swarm. The Level-3 admission gate stays on the
`_enqueue_task` choke point. If a proposed capability lets the system act without an
operator invocation bounding it, it is out of scope and belongs to the anti-goals.

---

## 2. Backend-readiness dossier (the F4 capability spike, answered on paper)

§4 F4 asks: *can a gateway-spawned session itself orchestrate sub-sessions with the current
backend surface?* Audited against `main` @ `24dff9b`. **Verdict: YES — the primitives exist;
M3 assembles them, it does not build a new orchestration engine.** The one genuinely new
piece is a **Manager tool surface** (an MCP server) + the role wiring + the loop guardrails.

### 2.1 What already exists ✅ (grounded in code)

| Capability M3 needs | Already in the backend | Where |
|---|---|---|
| Dispatch a worker task **with parent→child lineage** | `submit_instruction(..., parent_task=, dispatched_by=, dispatch_file=)` stamps lineage (flag-gated) then `_enqueue_task` | `src/orchestrator.py:2049`, `:2079` |
| HTTP surface to dispatch (callable by a session) | `POST /api/instructions` → `submit_instruction` (auth: tailnet/loopback token) | `src/control/control_api.py:883` |
| Session lifecycle (create/bind/stop/compact/close/model) | `POST /api/sessions` + `/{id}/bind|stop|compact|close|model` | `control_api.py:939–1026` |
| Approvals + the Level-3 admission bound | `POST /api/approvals`; `_harness_level3_allows_autopickup` on the choke point | `control_api.py:1061`; `orchestrator._enqueue_task` |
| **Observe** own dispatches (the case + lineage + events) | Work substrate: `GET /api/work`, `/api/work/{id}`, `/api/work/{id}/timeline`, `/api/work/affiliations/sessions`, `GET /api/flows[/{id}]` | `control_api.py:770–839`, `:736` |
| Write authoritative case state (links/events/stage/outcome) | `flow_links` / `flow_events` write seams + `update_flow_run` | A25–A30 (substrate) |
| **The MCP-tool-to-session pattern** (give a session a gateway tool) | `scripts/mcp_jobs.py` exposes `watch_job` over stdio; wired via `_mcp_jobs_configured()` → `mcp__jobs__watch_job` added to a session's allowed tools | `scripts/mcp_jobs.py`, `src/backends/claude_driver.py:310,392` |
| Long-poll / notify a session when async work finishes | Jobs `watch_job` (long-poll) + `POST /sessions/{id}/proactive-turn` reach-back + notification fan-out | `task_server.py:806–959` |
| Live aggregates for a manager agent | `GET /metrics` — *documented* "for the future project-manager agent … consumed by a task-distributing agent" | `task_server.py:354–414` |
| Orphan-worker reap (a guardrail primitive) | stale-claim reaper loop (lease + incarnation-id) | `task_server.py:699` |

### 2.2 What is missing 🔲 (this is the M3 build surface)

1. **A Manager tool surface (MCP server).** New `scripts/mcp_manager.py`, modeled *exactly*
   on `scripts/mcp_jobs.py` (loads `.env`, bearer token, urllib → gateway HTTP). Tools (each
   a thin wrapper over an endpoint that already exists):
   - `dispatch_worker(objective, files, cwd, parent_flow_run_id, skills?)` → `POST /api/instructions` with lineage.
   - `wait_for_worker(task_id|flow_run_id, timeout)` → long-poll `GET /api/work/{id}` (or `/api/flows/{id}`) until terminal status (reuse the `watch_job` long-poll shape).
   - `get_case(flow_run_id)` / `list_children(flow_run_id)` → Work read model.
   - `record_review(flow_run_id, verdict, findings)` → `flow_events` (append `review.*` — this is where the deliberately-deferred review vocab finally gets a real emitter; see §6).
   - `update_case(flow_run_id, status, closure_summary)` → `update_flow_run`.
   These are additive, read-mostly, and behind the Level-3 gate for anything that dispatches.
2. **Manager session type + role wiring.** A gateway-spawnable "manager" session that boots
   with (a) the `manager_invocation.md` role prompt, (b) the `mcp_manager` tools, (c)
   project-context grounding. Promote the *prompt* driver to an *invokable* role.
3. **The F4 orchestration-from-within-a-session proof.** Confirm live that a
   gateway-spawned Claude session, given `mcp_manager`, can dispatch a child worker session
   and receive its terminal result **without** deadlocking the gateway's own task loop
   (the Manager's `wait_for_worker` must not block a worker slot the child needs). This is
   Phase 3.0 — a spike, not a feature. If it can't, that becomes M3's first real job.
4. **Loop guardrails** (§5) — turn-cap, kill path, crash recovery, cost bound. None exist as
   *loop-level* controls yet.

### 2.3 Frontend readiness (secondary, per operator)

The Work surface (A28–A30) already renders cases, lineage (parent/children), the append-only
timeline, and session affiliations read-only — enough to **watch** a Manager run. The one
add worth scoping with M3: surface the **Manager→worker dispatch tree** and **review verdicts**
in the case detail (the `flow_events` `review.*` + lineage are already the data source). No
new backend needed; a read-only view addition. Kept out of the M3 critical path.

---

## 3. Worker model — separate sessions, NOT sub-agents (operator-directed)

The Manager dispatches each worker as an **independent gateway session**, not an in-process
sub-agent. Rationale (operator + roadmap-aligned):

- **Traceability.** A separate session is a first-class row (`sessions` + `flow_links` +
  `flow_events`) the operator can chase; a sub-agent is opaque in-process state — the exact
  opacity scar M2 was built to kill.
- **Honesty / harder to fool.** A separate session commits its own diff to git; the Manager
  reviews *that committed diff*, not a self-reported summary (`manager_invocation.md` rule 1).
- **Isolation.** One worker per branch/tree (rule 3); separate sessions make the tree
  ownership explicit and let the reaper/kill-path act on a real process.

The Manager "invokes" the worker via the `dispatch_worker` MCP tool (a real dispatch), then
tracks it via `wait_for_worker` + the Work substrate. This is the user's "manager calls a
tool that dispatches the task" made concrete.

---

## 4. The Manager role — reviews from a higher vantage (not mechanical)

M3 is **not** "find an agent and hand off." The Manager holds a point of view *above* the
worker and must be defined up front (this is the role contract the manager session boots
with; extends `manager_invocation.md` + `operating_model.md`):

- **Grounds before dispatch** — verifies intent against spec + code + git; surfaces conflicts
  and waits rather than silently building deferred/forbidden work.
- **Reviews from a different perspective than the worker** — not "did it run," but "is the
  result *actually* what was needed?" Examples of the higher-vantage check the Manager owns:
  - research/measurement work → *are the statistics sufficient to claim significance? are we
    discarding data? is the result misleading?*
  - a fix → *does it fix the impact or a symptom? does it re-open a known trap?* (checks the
    diff against `MEMORY.md` scars + past `[Fn]` findings).
  - always → *verify claims in git* (`git show`/grep/read), never trust the worker's report;
    run `/code-review` on real code diffs.
- **Owns documentation** — updates `DISPATCH_LOG` + `CONTEXT` + the case (`flow_events`
  `review.*`, `closure_summary`) so "what is done / what is left" stays truthful. This is a
  first-class Manager duty, not an afterthought.
- **Keeps a distinct reviewer seat for the plan** (v0.6 §5.1): plan review is a separate cheap
  pass or cross-model, not Manager self-congratulation — the adversarial-separation thesis.
- **Decides** iterate / close / derive, and only interrupts the operator for genuine forks
  (merge-to-main, Level-3 approval, strategic change, unresolvable spec conflict).

**Manager memory caveat (open question O2):** the Manager has no dedicated persistent memory
today. It leans on the Claude Code agent memory + the (now rich) documentation + the queryable
substrate. Good enough to start; flagged as a pivot if a long line of work needs continuity.

---

## 5. Guardrails & cost (authored here per §5.2 — concrete proposals)

The roadmap deliberately left these blank until the F4 audit; filling them now.

- **Round-cap on the manager↔worker loop.** Default **2** fix rounds (v0.6 §5.1); unresolved
  items become explicit `waived_findings`, not another round. **Plus a hard turn ceiling**
  (operator-raised): stop the dialogue at **N manager↔worker turns** (proposal: 20) and
  surface to the operator rather than looping forever. Config knob, default on.
- **Cost / session economy (open question O3, needs measurement).** Prefer *continuing an
  existing worker session* for the next bounded task over spawning a fresh one — a new session
  re-pays orientation tokens. But this is an *assumption*: add observability first
  (`llm_events` + `backend_usage` already track per-turn/session tokens) to measure
  new-session vs reuse-session cost before hard-coding a policy. Do **not** guess.
- **Operator kill path.** A tool/endpoint to abort a running flow → `status = blocked`
  (resumable), cancel the child session, no orphan. (Terminal-outcome seam A29 already writes
  `blocked`; extend for an operator-initiated cancel — the `flow.interrupted` event A29
  deferred is exactly this.)
- **Orphan-worker reap** — already exists (stale-claim reaper, `task_server.py:699`); M3
  inherits it. Confirm it covers a Manager-crash-mid-flow (child keeps its lease; reaper
  releases; case shows `blocked`, resumable).
- **Manager-crash recovery.** The case (`flow_runs` + `flow_events`) is durable, so a crashed
  Manager's flow is *reconstructable* — a new Manager can resume from the last event. Prove in
  Phase 3.3.
- **No paid-CLI verification** (project #1 scar): plain `pytest`; live check is
  `curl .../health`; never `python main.py status`. Binds the worker packets M3 dispatches.
- **Flag-gated.** M3 behind its own flag; OFF ⇒ byte-identical. Nothing here reads
  `current_stage` to drive execution (the M1 shadow invariant holds until a separate,
  deliberately-later hardening pass).

---

## 6. Phasing (bounded, each independently reversible)

- **Phase 3.0 — F4 spike (Level 3, code).** Build the minimal `mcp_manager` with just
  `dispatch_worker` + `wait_for_worker`; prove a gateway-spawned session can dispatch a child
  worker session and get its terminal result **without starving the task loop**. Success
  criterion: parent flow → child flow visible in `/api/flows` with lineage, child result
  returned to the parent session. *If this fails, it is the whole first job (as §4 F4 warns).*
- **Phase 3.1 — Manager role wiring.** Promote `manager_invocation.md` to a spawnable manager
  session type booting with the role prompt + `mcp_manager` + grounding. One intent → one
  scoped dispatch → one review → close, all durable.
- **Phase 3.2 — Review-from-above + `review.*` emitter.** Wire the Manager's verdict into
  `flow_events` (`review.accepted` / `review.rework_requested` / `review.waived`) — the vocab
  A29 deferred "until a reviewer role exists" now has its reviewer. Distinct plan-reviewer seat.
- **Phase 3.3 — Guardrails.** Round/turn caps, kill path + `flow.interrupted`, crash-recovery
  proof, cost observability (O3).
- **(Optional, parallel) M4 generators.** Spec-draft + scored-review *prompts* are docs; can
  land any time and be used by hand (v0.6 §M4 split).

**Acceptance (v0.6 §5.3):** one operator intent → a Manager flow row → traceable parent→child
dispatches via `/api/flows` → reviewed committed diff → flow closes → all queryable; and with
every flag OFF the system is byte-identical to today.

---

## 7. Open questions / considerations (flagged, not decided — operator input welcome)

- **O1 · Skills as a pre-built library (pivotal quality question).** The operator's model:
  the Manager is *invoked with inherited qualities* and *imbues the worker with the skills the
  task needs*. Today "qualities" = per-packet prompt scope/attitude. Should we build a **skills
  library** (named professional attitudes/procedures — e.g. "statistical-rigor",
  "no-false-success", "reuse-before-build") the Manager attaches to a dispatch by reference,
  rather than re-authoring attitude prose each time? This is a quality/optimization lever that
  likely sits **alongside M4** (spec/decomposition layer). *Decision deferred; flagged as
  potentially pivotal.*
- **O2 · Manager dedicated memory.** No persistent manager memory today (see §4). Rely on
  agent memory + docs + substrate to start; revisit if a long line of work needs continuity.
- **O3 · Session-per-task token economics.** Measure new-session vs reuse-session cost before
  policy. Needs a small observability slice over `llm_events`/`backend_usage`.
- **O4 · Repo-readability tooling to cut token burn.** A repo-map / symbol-graph tool
  (`codebase-memory-mcp`, v0.6 §"Optional accelerator"; or ctags/tree-sitter repo maps) so the
  Manager/worker orient without reading N files. **Off-box trial first** (ARM64 + RSS vs the
  Pi's memory pressure) before it touches the gateway host. Directly lowers per-session tokens.
- **O5 · Manager→manager handoff (higher-order).** At some point a Manager hands off to
  another Manager (long horizons). Explicitly **out of scope for M3** — parked as higher-level
  until the single-Manager loop is proven.
- **O6 · Dialogue guardrail shape.** The hard turn-ceiling (§5) — is 20 the right number, and
  should "deviation detection" be turn-count only or also semantic (repeated no-progress)?
  Start simple (turn-count), calibrate from real runs (don't pre-build a detector).

---

## 8. One-paragraph summary for the operator

The substrate to *observe* automated dispatches is now live (M2 done, `HARNESS_FLOW_DRIVE`
on). M3 does **not** need a new orchestration engine: the dispatch path, session lifecycle,
approvals, the Work/flows read model, and — crucially — the MCP-tool-to-session pattern
(`scripts/mcp_jobs.py`) all already exist. M3 = **one new MCP tool surface (`mcp_manager`) +
the Manager role wiring + loop guardrails**, built in four reversible, flag-gated phases
starting with a small F4 spike (prove a spawned session can dispatch a child without starving
the task loop). The Manager is a real reviewing role with a vantage above the worker — it
grounds in git, reviews from a different perspective, owns the docs, and is bounded by one
operator invocation and the Level-3 gate. Six open questions are flagged (skills library,
manager memory, session-cost economics, repo-readability tooling, manager→manager handoff,
dialogue guardrail shape) — none block Phase 3.0.
