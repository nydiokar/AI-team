# Agent messaging: assessment and design direction

**Status:** assessment and proposed direction; no implementation authorized by this document

**Reviewed against:** repository `main` at `0d94810` plus the committed A82/A104 evidence below

**Question:** does the current sender provide natural, discoverable agent-to-agent communication and delegation?

## Assessment

### Verdict

The current `ai_team_sender` is implemented and its narrow contract has one recorded live proof. It is **not** a mesh messaging or self-service delegation system. It is a session-scoped capability to enqueue a model turn for a known, eligible session in the same open Case. It neither discovers recipients nor creates them. The user’s criticism identifies real product gaps: discovering a peer, identifying a useful recipient, getting a recipient into scope, and asking for new help are all outside the sender contract.

The implementation is not simply dead code: A82 records one live call, accepted as task `task_834db4e9`, and A104’s database inventory later found that same row completed. But the inspected inventory found exactly one row with `sender_session_id`; this is evidence of a working probe, not evidence of regular use or a functioning general delegation loop.

### What was specified, and what shipped

The parent design is [Session Turn Queue](SESSION_TURN_QUEUE_DESIGN.md), implemented under [A82](../.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md). A82 Stage 5 explicitly says “not a new messaging platform.” Its contract intentionally requires an existing target session, same open Case, allowed role pair, and a managed claim that provisions a scoped send capability. It forbids broadcast and cross-Case sends. The capability is validated by the gateway and the accepted request enters the existing durable per-session turn queue.

| Requirement or implied user outcome | Shipped state | Assessment |
| --- | --- | --- |
| Secure, attributable instruction admission | Per-session capability, validated against current session/Case/role/incarnation; durable queue row records sender | Implemented; tested and one live send recorded |
| Send to a known session in the same Case | Caller supplies `target_session_id`; gateway checks eligibility | Implemented, but target discovery/ID resolution is absent from this tool |
| Find online/useful agents | No sender-side directory/search/presence or selection tool | Missing |
| Bring a new agent into the work | Sender cannot create or dispatch sessions; Manager `dispatch_worker` is separate and role-gated | Missing from sender flow by design |
| Reach an ordinary worker outside a Case | Cross-Case/out-of-Case send is refused; sender grant itself is Case-bound | Missing by design |
| Free-form messages that do not consume a model turn | Sender is explicitly an instruction/turn request, not chat | Out of scope by design |
| Natural delegation / handoff of unfinished work | No work-package transfer, acceptance/decline, ownership handoff, or successor lifecycle in sender | Missing; queueing a prompt is not a handoff protocol |
| Broad mesh communication | No broadcast or unrestricted discovery; no transport across Cases | Not implemented and not promised |

The strict authorization is a sensible safety boundary for *sending a turn*. The product mismatch is that a narrow, low-level send primitive was presented alongside “agent-to-agent” language without the discovery, routing, onboarding, or handoff layer that would make it feel natural. The implementation substantially fulfills the narrow A82 Stage 5 contract; that contract does not fulfill the broader mesh expectation.

### What consumes it today

There are two routes to putting work on an agent session:

1. **Manager delegation:** Manager MCP `dispatch_worker` opens or reuses a worker session and dispatches a task. This is the documented normal workflow in [manager.md](harness/roles/manager.md). It does not need `ai_team_sender`.
2. **Scoped agent send:** an already-running, provisioned Claude or Codex session calls `send_instruction` with a target session ID. The gateway validates the grant and same-Case role policy, then queues a turn for that recipient. This does not spawn or discover a worker.

The receiver consumes the resulting row through the existing managed turn scheduler/carrier. A completion can return to the requester’s inbox through the established completion path. This makes the sender a real producer of the turn queue, but no automatic consumer chooses recipients or invokes the sender on behalf of agents. OpenCode server deliberately refuses per-session provisioning because its MCP seam is shared per process; sharing one session’s raw capability would violate isolation.

### Live and durable evidence

- [A82_MULTIBACKEND_VALIDATION.md](../.ai/dispatch/A82_MULTIBACKEND_VALIDATION.md) records a live 2026-10-07 tool call from sender `53dc2f3c328c` to recipient `ee93d4266cd2`, both in Case `fc6661f439d74b9db10f213af2db119d`. The accepted task was `task_834db4e9`, `turn_source=agent`, `queue_protocol=1`, `queue_sequence=2`, initially `queued`.
- [A104_GATE1_INVENTORY.md](../.ai/dispatch/evidence/A104_GATE1_INVENTORY.md) later records that row as `completed` and says it found exactly one non-null `mesh_tasks.sender_session_id` row in the inspected DB snapshot.
- [A100](../.ai/dispatch/AGENT_100_A82_STAGE8B_LEGACY_CUTOFF.md) records that this live-send proof cleared a Stage 8b gate. The dispatch ledger says A82 is merged and live.
- The repo also has targeted tests in `tests/test_turn_queue_sender.py`; tests establish behavior under controlled conditions, not production adoption.

These are durable repository artifacts reporting observations from a real database. This review did not query the currently running production database, so it cannot establish whether later sends exist. The one-row count is scoped to the A104 snapshot, not a lifetime total.

## Why the current flow feels backwards

The path starts with a **recipient session ID**, although an agent generally needs to start from intent (“find someone who can review this Python change”). That means the caller must get the ID elsewhere. Even with an ID, the recipient must already be in the sender’s Case and meet the permitted role pairing. Finally, the sender capability appears only for an eligible managed claim. A plain worker cannot turn this tool into a worker-spawn API, and ordinary Manager dispatch is a separate tool and policy surface.

The order of missing steps is therefore:

1. Discover candidates and their current availability/capabilities.
2. Decide whether an existing candidate is suitable or new capacity is needed.
3. If needed, request authorized creation/admission of a worker.
4. Establish explicit work scope and recipient membership.
5. Deliver an instruction or work package with a durable acknowledgement.
6. Observe completion and let the requester accept, continue, or hand off the result.

Today only step 5 (with a pre-known, in-scope recipient) and the turn-completion substrate exist in this feature. Steps 1–4 and the explicit work-handoff semantics are not part of `ai_team_sender`.

## Proposed direction

Do not expand this into unrestricted peer-to-peer authority. Keep the durable queue and sender identity as useful transport/admission components, while adding a discoverable, policy-governed delegation layer above them. Discovery should return *eligible choices*, not grant authority by itself. Worker creation should remain an explicit gateway/Manager-controlled operation with capacity, Case scope, backend, node, and budget policy enforced server-side.

### Stage 0 — resolve operational posture

Treat the sender as a low-volume advanced capability until the design below is accepted. Keep its current same-Case and scoped-capability checks. Do not advertise it as a general mesh network. If it is not wanted even as this restricted capability, disable provisioning/tool exposure through a deliberate flag and retain queued records and migration data for audit; do not delete history or schema as cleanup.

### Stage 1 — make existing-peer delegation discoverable

Add a read-only, caller-scoped directory tool/API such as `list_case_agents` returning only same-Case sessions the caller is authorized to address. Return stable opaque session handles, role, online/availability state with timestamp, backend/node summary where policy permits, and declared capability tags. Do not expose arbitrary sessions, host paths, credentials, or unrestricted node inventory. The send tool should accept a directory handle or validated target ID and clearly explain why a target is unavailable. Manager should be able to use the same directory, not maintain a parallel roster.

This is the fastest meaningful improvement for current sessions: it removes ID guessing and teaches the agent who is reachable. It does **not** solve spawn-new-capacity or cross-Case delegation, and must be described accordingly.

### Stage 2 — one governed delegation entry point

Add an intent-oriented operation, for example `request_delegation(objective, required_capabilities, preferred_node?, case_id?)`, backed by one gateway-owned policy service. Resolve an eligible in-Case peer first; if none fits, make a bounded request for new worker capacity through the existing dispatch/session-creation machinery. A non-Manager should not receive raw `dispatch_worker` power by default: allow it to request a delegation, while the gateway checks Case policy, per-Case/per-sender rate and concurrency limits, allowed backends/nodes, model/budget ceilings, and whether worker creation is enabled. The request must have a durable ID and states such as `proposed → admitted → assigned → accepted/running → completed/declined/expired/failed`.

The chosen agent must receive a bounded work package with objective, constraints, relevant artifact references, deadline/expiry and return contract. Support explicit accept/decline before execution where an already-running recipient is being asked to take over work. Return a durable receipt immediately; surface outcomes in the requester inbox. Retries must be idempotent. A failed or unavailable target must not silently turn into an unbounded spawn loop.

### Stage 3 — explicit handoff and continuity

Treat “continue this work when I stop” as a separate handoff contract, not merely another prompt. Persist the source task/Case, current work summary, artifacts/revisions, remaining objective, authority bounds, and the successor’s accepted ownership. Keep the original owner until the successor accepts; timeout/decline returns the item to the requester/Manager or a visible queue. Do not claim exactly-once execution across backend crashes; use the existing managed-queue recovery states and surface uncertain execution for resolution.

### Stage 4 — broaden scope only with evidence

Consider cross-Case collaboration or a general directory only after same-Case discovery/delegation has measured use and policy requirements are clear. Cross-Case work needs an explicit sharing grant, data/artifact boundary, requester authorization, and audit trail. “Mesh” should not mean every session can enumerate or prompt every other session. Evaluate a broker only if measured load, independent consumers, replay, or multi-gateway needs outgrow the existing durable DB-backed queue; a new transport alone will not provide discovery, authorization, or handoff semantics.

## Failure modes to design and verify

| Failure | Required behavior |
| --- | --- |
| Stale session presence or target ID | Reject with a typed stale/unavailable result; directory carries freshness; never silently route to a different agent |
| Target not in Case / role no longer allowed | Refuse at gateway admission; do not rely on MCP-side filtering |
| No suitable existing worker | Return “no candidate”; only create one through bounded policy, otherwise surface to Manager/requester |
| Capacity, quota, or model budget exhausted | Durable refused/deferred state with reason and retry condition; no spawn storm |
| Recipient closes or loses Case membership after discovery | Revalidate atomically at admission; return stale/scope error |
| Duplicate tool retry or sender restart | Idempotency key returns same delegation/turn receipt; no duplicate execution admission |
| Gateway/carrier crash after acceptance | Existing queue recovery applies; if execution status is uncertain, hold and surface it rather than replay blindly |
| Capability rotation/revocation | In-flight admission rechecks current grant; stale capability fails closed; no secret in prompts, logs, telemetry, artifacts, or task payload |
| Recipient declines or never accepts | Expire visibly and return ownership to requester/Manager; never leave delegation silently pending |
| Malicious/oversized instructions or artifacts | Enforce request/body/artifact bounds, schema validation, references and ownership checks before scheduling |
| Cross-Case data leakage | No discovery or payload access without explicit sharing authorization; audit requester, recipient, Case and artifact grants |
| Unbounded concurrent requests | Server-side per-sender, per-Case and global admission/concurrency limits; bounded response and persistence costs |

## Why MCP, and what MCP does not solve

MCP is a reasonable **tool adapter** here: Claude/Codex expose named tools to sessions, and the existing code already has tool filtering and per-session tool provisioning. The sender tool can call the gateway over a narrow, session-scoped credential while the gateway remains the authority. That is a pragmatic way to make an operation callable from a model.

MCP is not a mesh directory, discovery protocol, identity system, routing policy, durable inbox, agent scheduler, or work-handoff model. Those are application capabilities. A direct MCP server per agent can also be the wrong sharing boundary when a backend shares one server process across sessions—as the deliberate OpenCode refusal demonstrates. Keep MCP as one possible front end for user/agent invocation; put discovery, authorization, admission, durable state, and lifecycle in gateway-owned services. Other front ends (UI, API, future agent protocols) can then use the same service without duplicating policy.

## Dependency and data-flow graph

```mermaid
flowchart LR
  subgraph Current[Current narrow sender path]
    A[Eligible managed agent session] -->|carrier provisions scoped capability| B[Claude in-process MCP or Codex stdio MCP]
    B -->|send_instruction(target ID, body, operation ID)| C[agent_sender client]
    C -->|AITeamSender capability| D[turn-requests API]
    D --> E[Validate sender, Case, role, target]
    E --> F[mesh_tasks durable managed turn queue]
    F --> G[Scheduler and worker carrier]
    G --> H[Recipient agent session]
    F --> I[Completion and requester inbox]
  end
  subgraph Missing[Missing product layer]
    J[Intent / work package] --> K[Directory and capability discovery]
    K --> L[Candidate selection or bounded capacity request]
    L --> M[Case / sharing / budget policy]
    M --> N[Durable delegation lifecycle and acceptance]
  end
  N -->|admitted delivery| D
  G -->|result / handoff| N
  O[Manager dispatch_worker] -->|separate current creation path| P[Create or reuse worker session]
  P --> F
  Q[OpenCode server shared MCP process] -. no per-session capability .-> B
```

### Upstream and downstream ownership

| Component | Role | Current status |
| --- | --- | --- |
| Managed carrier claim (`src/control/task_server.py`, `src/worker/agent.py`) | Upstream identity/ownership proof; provisions sender grant | Exists; only eligible managed claims receive it |
| Backend sender seam (`claude_driver.py`, `codex_native.py`) | Attaches the tool to an isolated session/thread | Exists for Claude/Codex; OpenCode server fails closed |
| `scripts/mcp_sender.py`, `src/control/agent_sender.py` | MCP/HTTP adapter | Exists; one `send_instruction` tool |
| `src/control/routes/turn_requests.py`, `control_api.py` | Authenticate capability and submit agent-origin turn | Exists; converges on shared admission |
| `src/control/db.py`, `mesh_tasks` | Durable receipt, sender attribution, queue order/idempotency | Exists; canonical turn queue |
| Scheduler/carrier + recipient backend | Execute the accepted recipient turn | Exists as managed queue consumer |
| Agent inbox/completion outbox | Return completion to requester | Exists downstream; current usage evidence is sparse |
| Manager `dispatch_worker` + session creation | Create/reuse worker capacity | Exists as a separate Manager-only path; not called by sender |
| Directory/presence/capability discovery | Let caller find a suitable peer | Missing from sender path |
| Delegation admission/policy service | Reuse peer or authorize bounded spawn for worker requests | Missing |
| Work-package acceptance and ownership handoff | Explicit transfer/continuity lifecycle | Missing |
| UI/API audit surface for delegation lifecycle | Inspect candidate, assignment, acceptance, outcome | Turn queue UI exists; delegation lifecycle surface does not |

## Acceptance gates for a future implementation packet

1. The operator chooses the intended scope: same-Case discoverable collaboration first (recommended), or explicitly defer/disable the sender. Do not infer approval for broad cross-Case mesh authority.
2. A caller can discover only eligible recipients, with freshness and typed refusal reasons; a test proves no foreign-Case/session disclosure.
3. A worker can request bounded delegation without receiving unrestricted Manager spawning authority; policy and capacity are server-enforced.
4. Delegation is durable, idempotent, attributable, bounded, and visible from proposal through terminal result; restart/retry/decline/timeout are covered.
5. Handoff preserves objective and artifact revisions and has one clear owner at each point; uncertain execution is visible, never silently duplicated.
6. Service-boundary review covers concurrency, memory, request size, timeouts, malformed inputs, and unavailable DB/carrier behavior for every new endpoint/handler.
7. Live verification demonstrates the real tool exposure, eligible discovery, admission, recipient execution, and requester-visible result. Unit tests alone do not establish the full path.

## Source documents

- [Session Turn Queue design](SESSION_TURN_QUEUE_DESIGN.md) — queue contract and Stage 5 sender subsection.
- [A82 implementation packet](../.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md) — staged implementation, review history, constraints and deviations.
- [A82 live validation](../.ai/dispatch/A82_MULTIBACKEND_VALIDATION.md) — recorded sender probe and managed backend validations.
- [A82 certification](../.ai/dispatch/A82_E2E_CERTIFICATION.md) — milestone gates and certification state.
- [A100 cutoff packet](../.ai/dispatch/AGENT_100_A82_STAGE8B_LEGACY_CUTOFF.md) — records live sender proof as a gate met.
- [A104 evidence inventory](../.ai/dispatch/evidence/A104_GATE1_INVENTORY.md) — observed sender row count/status in its DB snapshot.
- [Peer messaging investigation](PEER_MESSAGING_INVESTIGATION.md) — prior options and rationale for retaining gateway DB/Case authority; useful background, not current production behavior.
- [Manager role instructions](harness/roles/manager.md) — current `dispatch_worker` delegation workflow.

## Follow-on work

The next bounded implementation/audit job is [A115](../.ai/dispatch/A115_AGENT_DISCOVERY_FOR_DELEGATION.md), registered in [DISPATCH_LOG](../.ai/dispatch/DISPATCH_LOG.md#A115). It begins with independent verification of this assessment and current runtime, then targets same-Case discovery for already-existing sessions. It explicitly leaves worker creation, cross-Case access, and handoff ownership out of scope pending evidence and a separate decision.
