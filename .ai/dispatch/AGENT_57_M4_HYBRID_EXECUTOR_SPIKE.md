```yaml
job_id: AGENT_57_M4_HYBRID_EXECUTOR_SPIKE
created_at: "2026-07-30T02:34:12+03:00"        # CANONICAL — set once at dispatch, never derive again
status: dead                 # ready | active | blocked | done | dead
owner: ""
depends_on: AGENT_55_M34_JOB3_CRASH_RESPAWN
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T13:21:31.071933+00:00"
```

# DISPATCH — A57 · M4 hybrid-executor spike — RETIRED

**Level:** 3 (SDK capability spike) · **Type:** spike (investigation → written verdict, not a full build)
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, gated). **Depends on:** A55 (Jobs
1–3 landed). **Ultimate goal this serves:** `SPEC_COMPLETION_PLAN.md` §0 (M4 executor decision) / design §7
"parked" hybrid spike.

> **Outcome, not a script.** Decide — with evidence — whether the intra-task parallel executor is SDK
> Dynamic Workflows / subagents rather than a hand-rolled DAG executor, and how to contain it safely. The
> deliverable is a **go/no-go + chosen mechanism + containment design**, plus a minimal contained proof —
> NOT a shipped executor.

## Why (intent)
Design `docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md` §7 "Parked … M4 task-graph — HYBRID." Keep the durable
coarse Case graph as owner of objective/state/budget/closure; **replace the hand-rolled task-DAG *executor***
with Dynamic Workflows / SDK subagents for parallel decomposition inside a selected task, whose only durable
footprint is one `task_attached` node + a synthesized result + one `review.*` verdict. This is gated on a
capability spike; do it only after Jobs 1–3 (A52/A54/A55) land, so the durable spine exists first.

## TASK
Run the three-part capability spike from design §7 and return a decision.

## TYPE
spike. Branch `spike/m4-hybrid-executor` if any code is needed for the minimal proof; otherwise a written
verdict appended to the design doc + this packet's Closure.

## CONTEXT (reuse verbatim)
- Manager SDK options: `claude_driver.py:491-501` (passes no `Task`/`Workflow`/`agents=` today); grant is
  Read/Edit/Bash + manager MCP only (`claude_role_adapter.py:24-36`) — enabling a workflow/subagent tool is
  an allowlist add.
- Anti-goal + boundary: the executor's durable footprint stays exactly ONE `task_attached` node + result +
  verdict — it must not create parallel Case state.

## CHANGES (spike steps)
- **(a)** Confirm the **Python** `claude_agent_sdk` exposes the `Workflow`/`Task` tool (docs cite TS only).
  If yes → note the exact API. If no → fall back to SDK **subagents** (`agents=` / `AgentDefinition`),
  Python-confirmed.
- **(b)** Confirm the account's workflow `/config` toggle state.
- **(c)** Design containment: a `PreToolUse` hook + git-worktree to contain subagents' `acceptEdits`
  auto-approval (`AgentDefinition.tools` is under-enforced — SDK #172/#189). Provide a minimal proof that one
  subagent runs contained in a worktree.

## ACCEPTANCE (proof, not vibes)
1. A written verdict: chosen mechanism (Workflow vs subagents), with the confirming evidence for (a) and (b).
2. The containment design (hook + worktree) documented, with a minimal run showing one subagent contained
   (no escape of its edits outside the worktree).
3. A go/no-go on building the executor, and if go, a one-paragraph follow-on scope (a NEW dispatch, not this
   one).

## REALITY CONSTRAINTS
- This is a spike: do NOT ship a full executor here. Over-building is the failure mode.
- Do not de-scope M4 — re-scope it to "invoke, don't build" per design §7.

## SCOPE OUT
Shipping the executor (a follow-on dispatch decided by this spike).

## TRAIL / EVIDENCE (fill at close)
- The verdict + evidence + containment proof.

---
## Milestone (burndown)
- [ ] Python SDK Workflow/Task capability confirmed (or subagent fallback chosen)
- [ ] account workflow toggle confirmed
- [ ] containment design + minimal contained-subagent proof
- [ ] go/no-go verdict + follow-on scope

## Closure (2026-08-04 — retired before execution)

The project contract is explicit: parallel work uses gateway-managed, observable worker
**sessions** opened or reused through `dispatch_worker`, not SDK-internal subagents. This packet
proposed a second, ephemeral execution mechanism (`Workflow`/`AgentDefinition`) and is therefore
incompatible with the canonical session/Case/telemetry model. It is retired rather than executed.
No SDK subagent was created, configured, or run.
