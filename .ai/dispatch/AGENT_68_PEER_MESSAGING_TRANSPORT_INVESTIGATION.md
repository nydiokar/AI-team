```yaml
job_id: AGENT_68_PEER_MESSAGING_TRANSPORT_INVESTIGATION
created_at: "2026-08-03T18:17:00.871619+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T18:17:00.871643+00:00"
```

# DISPATCH — A68 · Peer messaging transport — holistic investigation (NO implementation)

**Level:** 2 (investigation / design doc — docs-only on `main`) · **Type:** architecture study + recommendation
**Authored:** 2026-08-03 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (independent). This is a STUDY. It produces a design doc + recommendation. It ships **zero** production code.

> **Read this first — why this packet exists.** Reviewing **hcom** (`github.com/aannoo/hcom`)
> the operator recognized a capability we've anticipated but never built: **free-form
> agent-to-agent messaging** — agents deciding on their own when to message each other, share
> findings, ask a peer, hand off context (hcom: `agent → hooks → db → hooks → other agent`,
> broadcast + `@mention`, subscriptions, delivery mid-turn or wake-idle). Today AI-Team has NO
> general peer-message primitive: all agent interaction flows through the **governed** path
> (Manager `dispatch_worker` → worker → `wait_for_worker` → `record_review`). The operator's
> framing (validated in the design conversation): free-form chat and roles are **different
> layers** — a transport primitive at the bottom, a role/review policy on top — and we built the
> policy while never laying the primitive underneath.
>
> **The question this job answers (and ONLY this):** *Would AI-Team benefit from a governed
> peer-message transport layer underneath the existing role/review system — one that keeps the
> canonical system-of-record, the review gates, and operator control fully intact — and if so,
> what would that refactor actually cost and change?* This is a **holistic investigation**, not a
> build. The deliverable is a decision-quality design doc; building is a SEPARATE future dispatch
> that only happens if the operator says go.

## HARD CONSTRAINTS the study must respect (the operator's non-negotiables)
These are the walls the design must stay inside — a proposal that breaks any of them is a
non-starter and should be reported as such:
1. **Canonical system-of-record stays.** Any peer message is an auditable, DB-canonical event
   (like `flow_events`), never ephemeral PTY injection. We keep durability/recovery.
2. **Control stays centralized.** This is *"becoming a dangerous autonomous loop"* — messages may
   **inform** (share findings, ask a question), but **state-changing actions still go through the
   gate.** A worker telling another worker to merge, unaudited, is explicitly forbidden. The
   distinction *communication-channel vs action-channel* is the crux — carry it throughout.
3. **Roles + review gates stay intact.** The peer layer sits UNDER them, does not replace them.
4. **No PTY-persistence backbone, no SDK-decoupling.** The SDK persistent-session transport
   (model B) stays — the operator confirmed its rationale (cache-warmth: CLI `--resume` flags
   invalidate the prompt cache and long Manager sessions hit ~30M cache-read tokens; plus the
   Agent SDK's model-agnosticism lets one backend drive any model). Do NOT propose ripping it out.
   hcom's PTY-injection delivery is a REFERENCE for the *idea of mid-turn/idle delivery*, not a
   transport to adopt.

## PROACTIVE INVESTIGATION (do NOT mindlessly design — check what could break first)
The agent MUST autonomously verify these against the LATEST code + specs before proposing
anything, and STOP to flag anything that makes the idea dangerous or redundant:
1. **Do we already have most of this?** Map what exists: `flow_events` (append-only), the
   `review.*`/`task.*`/`case.*` emitters, `wait_for_worker`/`arm_wait_group`/`reconcile_waits`
   (M3.4), `read_session_history`, `release_worker`. Is a "peer message" just a new
   `flow_events` type + a `send_message`/`read_messages` MCP tool pair? Quantify how much is
   genuinely new vs a thin layer over existing substrate. **If the honest answer is "90% already
   exists, it just needs 2 MCP tools," say so — that changes the whole cost picture.**
2. **The wake/delivery problem.** hcom's cleverness is delivery: mid-turn injection + waking idle
   agents (Stop-hook `decision:block`). Our agents are SDK persistent sessions driven by the
   gateway. How would a message reach a busy worker mid-turn, or wake an idle one? Does M3.4's
   wake-dispatcher (`arm_wait_group`/IDLE-gate, MEMORY PRs #51-53) already give us the wake
   primitive? What's missing? Be concrete about the seam in `claude_driver`/orchestrator.
3. **Governance blast radius.** If workers can message each other, what NEW abuse/failure modes
   appear? Message storms (two agents ping-pong forever → cost blowout — tie to A65 cost
   governor / `sdk_max_turns`), prompt-injection peer-to-peer (a compromised worker steering
   another — tie to A67), audit gaps. For each, state the containment (rate limits, the
   action-channel gate, per-turn audit).
4. **What it would actually change.** Concrete refactor surface: which files, which MCP tools,
   which DB migration, which flags. Rough effort (S/M/L). What breaks or needs re-testing.
5. **Is it even wanted vs the role model?** Steelman the "NO" case: the governed dispatch/review
   path may be *sufficient* and a free-form layer may just add cost + attack surface for little
   real gain. Present both sides honestly; the operator decides.

## DELIVERABLE — `docs/PEER_MESSAGING_INVESTIGATION.md`
A decision-quality study containing:
- The layered framing (transport primitive vs role/review policy) grounded in OUR code.
- The five investigation findings above, with real answers (esp. #1 — how much already exists).
- **2-3 candidate designs** (e.g. "thin: 2 MCP tools over `flow_events`" / "medium: message bus
  + subscriptions" / "don't build"), each with cost/risk/payoff and how it honors ALL four hard
  constraints.
- A **recommendation** (the one the author would pick) — but the go/no-go is an OPERATOR decision
  (project-direction fork per the project CLAUDE.md); frame the choice, do not start building.
- An explicit "hard constraints honored?" checklist per candidate.

## TYPE
Level 2, docs-only → commits straight to `main`. **NO `src/` changes, NO migrations, NO MCP tool
implementations.** If the study concludes "build it," that is a NEW dispatch the operator approves
separately.

## CONTEXT (what exists — study these, reuse the substrate in the proposal)
- `flow_events`/`flow_links` (A25/A26) — append-only event substrate + case membership.
- `review.*`/`task.*`/`case.*` emitters — the pattern a `message.*` event would follow.
- `scripts/mcp_manager.py` — MCP tool surface (`dispatch_worker`, `wait_for_worker`,
  `arm_wait_group`, `reconcile_waits`, `record_review`, `read_session_history`, `release_worker`).
- M3.4 wake-dispatcher (MEMORY: IDLE/AWAITING_INPUT gate, PRs #51-53) — the existing wake
  primitive.
- `src/backends/claude_driver.py` / `src/orchestrator.py` — where a mid-turn/idle delivery seam
  would live.
- `docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md`, `context/production_vision.md` (anti-goals:
  no uncontrolled autonomy), CONTEXT.md "Architecture rules".
- A65 (cost governor) + A67 (mesh security) — the two containment stories a peer layer leans on.
- **hcom reference:** `agent → hooks → db → hooks → other agent`; `src/messages.rs` (broadcast/
  mentions/scope), `src/db/{events,subscriptions}.rs`, `src/delivery.rs`, `src/notify/wake.rs`
  (delivery + wake). Clone: `https://github.com/aannoo/hcom`. Study the SHAPE; our transport differs.

## ACCEPTANCE (proof)
1. `docs/PEER_MESSAGING_INVESTIGATION.md` written: five findings answered from OUR code (esp. the
   honest "how much already exists" number), 2-3 candidate designs with cost/risk/payoff, a
   recommendation, and the four-hard-constraints checklist per candidate.
2. Zero `src/` diff (this is a study — a code diff is a scope violation).
3. The go/no-go is framed as an operator decision, not pre-decided by starting a build.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — go/no-go on building any of it** is the operator's (project-direction fork). The study
  recommends; it does not implement.
- **R2 — if "thin layer already exists"** turns out true, flag whether even the thin version needs
  operator sign-off given the governance blast radius (§3), or is a safe additive default.

## SCOPE OUT
- ANY implementation (MCP tools, migrations, delivery code, flags) — a separate future dispatch.
- Adopting hcom's PTY-injection / hooks transport or the MQTT relay.
- Decoupling from the SDK / changing model B.
- Weakening the role/review gate or the action-channel/communication-channel boundary.
- Redesigning dispatch / CONTEXT structures.

## TRAIL / EVIDENCE (fill at close)
- `docs/PEER_MESSAGING_INVESTIGATION.md` · confirmation of zero `src/` diff · `results_ref` →
  DISPATCH_LOG row.

---
## Milestone (burndown)
- [ ] Five proactive-investigation findings answered from our code
- [ ] 2-3 candidate designs w/ cost·risk·payoff + hard-constraints checklist
- [ ] Recommendation written; go/no-go framed as operator decision
- [ ] `docs/PEER_MESSAGING_INVESTIGATION.md` on `main`; zero src diff

## Closure (fill on completion)
(fill when executed)
