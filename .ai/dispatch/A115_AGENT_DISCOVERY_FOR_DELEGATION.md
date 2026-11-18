```yaml
job_id: A115_AGENT_DISCOVERY_FOR_DELEGATION
created_at: "2026-11-15T12:38:20.483147+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A115             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-15T12:39:34.390315+00:00"
```

# DISPATCH — A115 · Make same-Case agent delegation discoverable

**Level:** 3. **Type:** product workflow + gateway/tool implementation and verification.
**Authored:** 2026-11-15, Europe/Vilnius. **Status:** ready for independent verification and implementation.
**Branch:** follow repo branch policy for runtime changes; keep this packet and its evidence in this file.
**Depends on:** none. The assessment PR is a starting map, not a trusted specification or approval of every proposal.

## Read this first

- [Agent messaging assessment and design](../../docs/AGENT_MESSAGING_ASSESSMENT_AND_DESIGN.md) — problem framing, current-path graph, staged proposal and failure modes. Re-derive its claims from the current tree before relying on them.
- `.ai/CONTEXT.md`, `.ai/dispatch/DISPATCH_LOG.md`, current `git status`/`git log`, and [A82](AGENT_82_SESSION_TURN_QUEUE.md) / [A82 live validation](A82_MULTIBACKEND_VALIDATION.md).
- [Session Turn Queue design](../../docs/SESSION_TURN_QUEUE_DESIGN.md), especially Stage 5 and current API/queue contract.
- `scripts/mcp_sender.py`, `src/control/agent_sender.py`, `src/control/routes/turn_requests.py`, `src/control/db.py`, `src/worker/agent.py`, `src/backends/claude_driver.py`, `src/backends/codex_native.py`, `src/backends/opencode.py`, `src/backends/claude_role_adapter.py`, and Manager `dispatch_worker` implementation.
- Existing Case/affiliation/session reads, authorization patterns, agent inbox, queue UI/API, and targeted tests. Use the symbol index before opening named definitions.

## Goal

Make it possible for an eligible managed agent to discover which already-existing agent sessions it may safely ask for help within its current open Case, then send a bounded instruction without hand-copying opaque session IDs. Keep the gateway as the authority for identity, eligibility, Case scope, admission and durable state. This first increment is **discovery of existing in-scope capacity**; it does not create workers, grant arbitrary Manager tools, enable cross-Case visibility, or claim to implement general mesh networking or work ownership transfer. Verify that this narrow increment is valuable and safe before proposing expansion.

## Required independent verification

Before design/code changes, answer these from current code and, where available, read-only live observations:

1. Which backends currently provision the sender tool, through which concrete boot/claim path, and what does the shared OpenCode server actually do today?
2. Which canonical records define active session, Case membership/role, carrier/node, and online/availability? What does “available” mean in code (heartbeat freshness, process ownership, queue state), and what cannot be known reliably?
3. Which existing read API/helper can serve a caller-scoped roster without a new store, duplicated pending source, N+1 reads, or disclosure of foreign Case/session data?
4. Which live runtime and database evidence exists now for sender use? Re-check the current status without printing secrets or opening a production DB writable. Distinguish code/tests, committed historical probes, and current live observations.
5. Re-evaluate every claim in the assessment that matters to the proposed change. Correct the assessment in the same PR or record exact evidence and limits here. Do not assume the old one-row inventory is a lifetime count or that historical live validation describes current deployment.

If the authoritative state, privacy boundary, or feasible definition of reachability cannot be established, stop before exposing a directory and record the specific missing evidence/decision. Do not invent an “online” meaning.

## Task — minimal first increment

1. Trace the current sender from tool exposure to capability validation, durable turn admission, recipient execution, and requester-visible completion. Record actual producers/consumers and unsupported backends.
2. Define a bounded, caller-scoped discovery contract for same-open-Case sessions only. Reuse canonical DB/read-model and authorization patterns. Return the smallest useful set of fields (opaque target handle/session id, role, validated availability/queue state, and a freshness/unknown indication if the source is stale). Do not return prompts, host paths, credentials, arbitrary node inventory, unrelated sessions, or stale claims as facts.
3. Expose discovery to the same eligible agent sessions that can send, through existing role/tool filtering. Make the sender tool’s discover→select→send path understandable and provide typed refusal reasons for stale, closed, wrong-Case, unsupported, or not-enrolled recipients. Continue to revalidate target and sender atomically at admission; discovery is advisory, never authorization.
4. Bound the new read surface and review concurrency, memory at 100 concurrent calls, request/response size, timeouts, malformed input, and DB/carrier failure. Avoid per-session N+1 queries and a second inbox/presence authority. The discovery endpoint/tool must fail closed on ambiguous Case identity and must not widen `AITeamSender` authority.
5. Add focused tests for eligible/ineligible roles, same-Case isolation, closed/rebound sessions, stale/missing availability, target race between list and send, pagination/caps, DB failure, concurrent reads, and existing sender behavior. Do not use paid agent calls.
6. Update the assessment’s status/evidence and describe what Stage 1 does not solve: creating new capacity, cross-Case work, accepting/declining a handoff, and continuation/ownership transfer. Recommend the next smallest step from evidence; do not implement those by implication.

## Done when

- The changed code has a traced path from caller identity → eligible roster → target selection → server-side revalidation → durable queue receipt → recipient execution/completion surface; state explicitly which seams were not live-verified.
- Only same-Case eligible targets are discoverable, with tests proving no cross-Case/session enumeration. Tool exposure is verified against actual role/tool profiles and backend limitations.
- The service-boundary checklist above is completed with concrete bounds or documented deferrals in the assessment; no unsupported online/presence guarantee is made.
- Targeted tests for touched modules pass, static/type/build checks appropriate to touched layers pass, and the real diff is reviewed for unintended policy widening. Never run full/e2e or paid suites.
- A concise update to the durable assessment captures measured findings, remaining gaps, failure modes, and the recommended next increment. The packet links exact code/test/evidence paths and records the PR/build/live status honestly.
- Runtime changes follow the repository’s feature-branch/PR/closure policy. Do not restart workers; any worker restart need is surfaced as an operator decision. No production rollout or claim of adoption is inferred from tests.

## Explicit boundaries

- Do not give ordinary workers `dispatch_worker`, arbitrary session creation, or broad node discovery as a shortcut.
- Do not let an agent discover sessions outside its authorized Case or send across Cases.
- Do not add a broker, new message database, generic A2A framework, or a parallel inbox/queue unless current code and measured constraints prove the existing gateway-owned turn queue cannot serve the objective; if so, stop and document the case before expanding scope.
- Do not call a queued instruction “chat,” “handoff,” or accepted work ownership. Those need separate product and lifecycle contracts.
- Do not presume this feature is used regularly, currently live, or desirable beyond the evidence.

## Trail / evidence

Parent reference: [assessment and design](../../docs/AGENT_MESSAGING_ASSESSMENT_AND_DESIGN.md). Keep milestones, audit notes, and closure in this packet. Supporting evidence goes under `.ai/dispatch/evidence/` with this packet as parent. Durable design changes belong in `docs/`; link them here. Do not create top-level supporting Markdown siblings.
