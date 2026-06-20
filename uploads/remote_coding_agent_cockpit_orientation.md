# Remote Coding-Agent Cockpit — Architecture Orientation & Refactor Guidance

Status: v0.2 orientation brief  
Owner: Nyd  
Purpose: guide future coding agents working inside the existing mature codebase so they can extend it without rediscovering the architecture, flattening abstractions, or accidentally rebuilding Hermes/OpenClaw.

---

## 1. One-line identity

A self-hosted, phone-first operations cockpit for dispatching, monitoring, resuming, reviewing, and coordinating existing coding-agent backends across local and remote machines.

---

## 2. Two-sentence product definition

This project is a remote control plane above existing coding agents such as Claude Code, Codex, OpenCode, ZAI, and future backends. Its core value is backend arbitrage and operational control: pick the right agent, machine, project folder, quota window, permission mode, review path, and continuation strategy without being physically present at the PC.

---

## 3. Critical correction: this is not a greenfield specification

The system already exists and is mature enough to have real architectural pressure.

Do **not** read this document as a request to build a new app from scratch.

Read it as:

```text
orientation for future agents
+ refactor guidance
+ abstraction target
+ UI expansion strategy
+ workflow automation direction
```

The existing system already has meaningful capabilities, including:

```text
Telegram gateway
server process
worker processes
multi-backend coding-agent spawning
project-folder scoped execution
restart resilience / state handling
job/script registration
completion notifications
permission-mode controls
remote dispatch from phone
```

Some abstractions may already exist, especially backend abstraction. Future agents must inspect the current codebase first and map existing structures to this orientation instead of creating parallel versions.

Rule:

```text
Preserve working machinery.
Extract seams where missing.
Do not rewrite stable components just to match names in this document.
```

---

## 4. What this project is not

This project is not:

```text
a new coding agent
a replacement for Claude Code / Codex / OpenCode / ZAI
a Hermes clone
a full OpenClaw clone
a generic agent marketplace
a general autonomous assistant
a memory-first self-improving agent framework
a chatbot with nicer visuals
a Telegram bot with extra screens
```

Correct category:

```text
remote coding-agent operations cockpit
personal agent broker
multi-backend coding-task control plane
quota-aware coding-agent dispatcher
operator console above agent backends
```

The cockpit is the missing operations layer above existing agents.

---

## 5. Differentiation from Hermes

Hermes is useful as a reference, but it is not the target.

Hermes-like direction:

```text
one adaptive agent core
+ memory
+ skills
+ provider registry
+ tool registry
+ platform entrypoints
+ ACP/MCP integration
+ optional runtime delegation
```

This project direction:

```text
many existing coding agents
+ remote dispatch
+ worker selection
+ quota-window use
+ backend fallback
+ session supervision
+ adversarial review
+ project handoff discipline
+ phone-first control
+ future workflow agents around the operator loop
```

What to borrow from Hermes-style systems:

```text
thin entrypoints over a stable core
registry-based discovery
optional subsystems instead of hard dependencies
provider/backend resolution patterns
session persistence discipline
tool availability checks
```

What not to borrow:

```text
broad personal-assistant scope
large skill ecosystem
self-improvement as a core dependency
global memory as an architectural center
provider/runtime complexity before needed
```

Correct relationship:

```text
Hermes may become:
  optional backend
  optional supervisor agent
  reference for registries and thin entrypoints

Hermes must not become:
  architecture owner
  identity of the project
  required orchestration layer
```

---

## 6. Differentiation from OpenClaw

OpenClaw is closer to this project than Hermes because it has gateway, sessions, channel routing, runtime adapters, subagents, and WebSocket control-plane ideas.

OpenClaw-like direction:

```text
gateway
+ channels
+ sessions
+ runtime adapters
+ ACP bridge
+ subagents
+ multi-surface clients
+ broad platform scope
```

This project direction:

```text
narrow personal coding-agent cockpit
+ concrete remote work constraints
+ quota-aware dispatch
+ multi-backend routing
+ phone approvals
+ task/session/handoff discipline
+ local/remote worker control
+ project-specific execution discipline
```

What to borrow from OpenClaw-style systems:

```text
Gateway WebSocket as a single control-plane stream
session registry
runtime/harness adapters
channel separation
session-to-session messaging/subagent pattern
evented architecture
policy-gated tool execution
client role/scope distinction
smoke-test discipline for real backend spawns
```

What not to borrow:

```text
full platform ambition
all channels
large plugin marketplace
broad autonomous-agent social layer
every protocol at once
large SDK before internal seams stabilize
```

Correct relationship:

```text
OpenClaw is a reference architecture.
This project remains a narrow operator cockpit.
```

The useful lesson is not “become OpenClaw.”

The useful lesson is:

```text
separate gateway core from channels, sessions, runtimes, workers, and clients
```

---

## 7. The real target now

The immediate target is not “build another interface.”

The target is:

```text
abstract the existing mature system so Telegram, Web UI, future mobile UI, supervisor agents, and agent-to-agent workflows all plug into the same core without separate logic paths.
```

The web UI is the forcing function.

Building it should expose and clean up the boundaries:

```text
transport boundary
command boundary
event boundary
session boundary
task boundary
backend boundary
worker boundary
workflow boundary
```

Desired outcome:

```text
Adding a web UI should not require duplicating Telegram command logic.
Adding a mobile app later should not require another backend refactor.
Adding supervisor workflows should not require rewriting session handling.
Adding A2A later should not require replacing internal delegation.
Adding a new coding backend should not touch UI code.
```

---

## 8. Core architecture rule

Everything should pass through one stable internal control contract.

Bad pattern:

```text
Telegram command -> backend-specific spawn logic
Web button -> separate backend-specific spawn logic
Supervisor -> direct backend-specific spawn logic
```

Good pattern:

```text
Transport/UI/Supervisor
  -> Command DTO
  -> Gateway Application Service
  -> Policy/Validation
  -> Task/Session/Run Service
  -> Backend Adapter / Worker Adapter
  -> Event Stream
```

All external surfaces should emit structured commands into the gateway core.

All state changes should emit events.

All clients should render events.

---

## 9. Canonical layers

Future code should converge toward these layers. The exact module names can differ if the existing codebase already has good equivalents.

```text
interfaces/
  telegram
  web_ui
  future_mobile
  future_cli
  supervisor_agents

api/
  http_routes
  websocket_gateway
  command_handlers

core/
  domain_models
  application_services
  policy
  orchestration
  workflow_state
  event_log

registries/
  backends
  workers
  transports
  roles
  prompt_profiles
  tools_scripts

adapters/
  backend_adapters
  worker_adapters
  protocol_adapters
  transport_adapters

storage/
  state_db
  event_store
  artifact_store
```

Layer law:

```text
Interfaces may depend on API contracts.
API may depend on core services.
Core may depend on abstract ports/interfaces.
Adapters implement ports/interfaces.
Core must not depend on Telegram, Web UI, Claude Code, Codex, OpenCode, or any specific transport/backend.
```

---

## 10. Stable domain objects

These are not necessarily new tables/classes. They are the conceptual objects future agents should look for or extract.

### 10.1 Task

Main unit of intent.

A task answers:

```text
What should be achieved?
For which project?
With what priority?
Using which backend/worker constraints?
What state is it in?
What is the next action?
```

Minimum desired fields:

```text
id
project_id
title
goal
status
priority
created_from
backend_preference
fallback_backends
worker_preference
permission_mode
quota_policy
session_ids
run_ids
review_ids
handoff_ids
artifacts
next_action
requires_user_input
```

Task is the object the future web UI, supervisors, and schedulers should operate on.

### 10.2 Session

Main unit of agent context.

A session answers:

```text
Which agent context/thread is this?
Which backend owns it?
Can it be resumed?
Is it contaminated or still useful?
What messages/events/artifacts belong to it?
```

Minimum desired fields:

```text
id
project_id
backend_id
worker_id
cwd
status
parent_session_id
child_session_ids
upstream_resume_id
thread_summary
event_ids
context_usage_if_available
token_usage_if_available
```

### 10.3 Run

Main unit of execution attempt.

A run answers:

```text
What concrete backend process/session attempt happened?
When did it start/end?
What did it output?
Did it fail?
Can it be retried or resumed?
```

Minimum desired fields:

```text
id
task_id
session_id
backend_id
worker_id
started_at
ended_at
status
exit_code
failure_reason
permission_mode
artifacts
event_ids
```

### 10.4 Worker

Main unit of machine capability.

A worker answers:

```text
Which machine can execute work?
Is it alive?
What backends are installed?
Which projects can it access?
What is currently running?
```

Minimum desired fields:

```text
id
machine_name
reachable
last_heartbeat
available_backends
active_sessions
active_runs
project_mounts
trust_level
resource_state
```

### 10.5 Backend

Main unit of coding-agent capability.

A backend answers:

```text
Which coding agent is available?
How do we spawn it?
Can it resume?
Can it stream?
Can it report tool calls/diffs/context usage?
What is its quota/auth/health state?
```

Minimum desired fields:

```text
id
name
adapter_type
supports_spawn
supports_resume
supports_noninteractive
supports_streaming
supports_cancel
supports_permission_events
supports_context_usage
supports_diffs
supports_tool_events
supports_quota_detection
quota_state
auth_state
health_state
```

### 10.6 Event

Main unit of observability.

Every important change should become an event.

Minimum desired fields:

```text
id
timestamp
source_type
source_id
event_type
project_id
task_id
session_id
run_id
worker_id
backend_id
payload
visibility
```

Events are what make Telegram notifications, WebSocket UI, audit logs, and future automations cheap.

### 10.7 Review

Main unit of adversarial quality control.

A review answers:

```text
Did the agent actually solve the task?
What did it miss?
Should it fix something?
Is the work ready for handoff/commit?
```

Minimum desired fields:

```text
id
task_id
session_id
reviewer_role
reviewer_backend_id
status
findings
severity
recommended_action
created_at
completed_at
```

### 10.8 Handoff

Main unit of context transfer.

A handoff answers:

```text
What changed?
What is done?
What remains?
Can the next agent start fresh?
Which files/logs/artifacts matter?
```

Minimum desired fields:

```text
id
project_id
task_id
session_id
summary
changed_files
open_questions
known_failures
next_recommended_task
context_files_updated
ready_for_new_context
created_at
```

### 10.9 Artifact

Main unit of durable output.

Artifacts include:

```text
logs
diffs
patches
script results
test results
handoffs
summaries
reports
screenshots
```

### 10.10 QuotaState

Main unit of backend availability constraint.

Minimum desired fields:

```text
backend_id
account_id_if_relevant
state
last_checked_at
reset_at_if_known
confidence
source
notes
```

Rule:

```text
Unknown is valid.
Do not invent quota precision.
```

---

## 11. Refactor objective

The main refactor is not a rewrite.

The main refactor is to make the existing system more pluggable:

```text
Telegram should become one transport.
Web UI should become another transport.
Backend agents should remain adapters behind one backend interface.
Workers should register capabilities.
Supervisor workflows should emit commands like humans do.
All surfaces should consume the same event stream.
```

Target shape:

```text
                ┌──────────────────┐
Telegram ──────▶│                  │
Web UI ────────▶│  Gateway Core    │──▶ Worker Registry ──▶ Worker Daemons
Supervisor ────▶│                  │──▶ Backend Registry ─▶ Backend Adapters
Future Mobile ─▶│                  │──▶ Event Log ────────▶ All Clients
                └──────────────────┘
```

No interface should bypass the gateway core.

---

## 12. Gateway WebSocket guidance

A Gateway WebSocket is the correct direction for the new interface because it makes the cockpit event-native.

The WebSocket should not be only a chat stream.

It should carry structured events:

```text
worker.heartbeat
backend.health_updated
backend.quota_updated
task.created
task.status_changed
session.created
session.message.user
session.message.agent
run.started
run.output
run.failed
run.completed
tool_call.started
tool_call.completed
artifact.created
review.requested
review.completed
handoff.created
approval.requested
approval.granted
```

Web UI should render these events into views:

```text
Dashboard
Task Queue
Active Runs
Session Timeline
Worker Status
Backend Status
Review Queue
Artifact/Handoff View
```

Telegram should consume only filtered/high-signal events:

```text
run completed
run failed
approval requested
review failed
ready to commit
quota reset / backend available
human attention needed
```

Rule:

```text
If a feature needs live state, it should subscribe to events.
If a feature changes state, it should issue a command that produces events.
```

---

## 13. Command contract guidance

The gateway should expose structured commands independent of UI.

Examples:

```text
CreateTask
DispatchTask
ScheduleTask
ContinueSession
SpawnFreshSessionFromHandoff
CancelRun
RequestReview
ApplyReviewFix
WriteHandoff
CommitCheck
SwitchBackend
MoveTaskToWorker
AskProjectManagerNextTask
InviteAgentToTask
```

Each command should have:

```text
command_id
actor
source_transport
project_id/task_id/session_id as applicable
payload
idempotency_key if needed
created_at
```

Command handler responsibilities:

```text
validate input
check policy
call domain/application service
persist state change
emit event(s)
return accepted/rejected result
```

UI responsibility:

```text
construct command
send command
render command result/events
```

UI must not contain orchestration logic.

---

## 14. Backend abstraction guidance

If a backend abstraction already exists, preserve it and harden it. If missing pieces exist, extend the interface instead of branching in UI code.

Desired backend adapter surface:

```text
spawn(task, cwd, mode, env, permission_profile) -> session/run reference
send(session_id, message) -> accepted/rejected
stream(session_id) -> event iterator if supported
cancel(session_id) -> result
status(session_id) -> status
resume(upstream_resume_id, message) -> session/run reference
list_sessions() -> sessions if supported
collect_artifacts(session_id) -> artifacts
healthcheck() -> BackendHealth
quota_status() -> QuotaState
capabilities() -> BackendCapabilities
```

Backend adapters must report missing features honestly:

```text
supports_resume: true/false/unknown
supports_streaming: true/false/unknown
supports_tool_events: true/false/unknown
supports_context_usage: true/false/unknown
supports_quota_detection: true/false/unknown
```

Rule:

```text
Do not normalize by pretending all backends can do the same thing.
Normalize by exposing capabilities and letting the scheduler/UI adapt.
```

---

## 15. Registry guidance

Use registries as the first modularity mechanism. Do not jump straight to a public plugin SDK.

Desired registries:

```text
BackendRegistry
WorkerRegistry
TransportRegistry
RoleRegistry
PromptProfileRegistry
ToolScriptRegistry
WorkflowRegistry
```

Each registry should answer:

```text
what exists
what is enabled
what capabilities it has
how to instantiate/use it
what health/config state it has
```

Example:

```text
BackendRegistry:
  claude_code -> ClaudeCodeAdapter
  codex -> CodexAdapter
  opencode -> OpenCodeAdapter
  zai -> ZaiAdapter
  hermes_optional -> HermesAdapter
  openclaw_optional -> OpenClawAdapter
```

Future agents should prefer:

```text
register a new adapter
```

over:

```text
add if backend == "x" branches across the codebase
```

---

## 16. Workflow automation direction

The repeated manual workflow should become explicit workflow primitives.

Current human pattern:

```text
ask what is next
check context/handoff
choose backend/session
ask agent to execute professionally
wait
ask for adversarial review
ask agent to fix review findings
ask if ready to commit/handoff
decide whether to continue or start fresh
```

Target workflow:

```text
Task created
Project Manager determines next meaningful advance
Context Keeper prepares context/handoff bundle
Router chooses backend + worker + quota strategy
Coder Agent executes
Supervisor audits
Fix pass runs if needed
Handoff Writer creates continuation packet
Commit Gatekeeper checks readiness
Next task is suggested or queued
```

This should begin as explicit commands, not full autonomy.

Initial commands:

```text
/next_task project_id
/prepare_context task_id
/dispatch task_id backend worker
/audit task_id
/fix_from_review task_id review_id
/write_handoff task_id
/commit_check task_id
/invite_agent task_id role worker backend
```

Only after each command is reliable should chaining be enabled:

```text
run -> audit -> fix -> audit -> handoff -> commit_check
```

Rule:

```text
Automate by formalizing the user's proven workflow.
Do not invent a new autonomous workflow.
```

---

## 17. Supervisor roles

Supervisor agents are not the main product. They are workflow components inside the cockpit.

Useful roles:

```text
ProjectManager
ContextKeeper
RouterScheduler
Coder
AdversarialReviewer
ImplementationAuditor
HandoffWriter
CommitGatekeeper
DebugCoordinator
```

Each role should have:

```text
role_id
allowed_commands
prompt_profile
input_contract
output_contract
backend_preferences
permission_profile
```

Supervisor outputs should be structured when possible:

```text
recommended_next_task
review_findings
fix_required
handoff_ready
commit_ready
blocked_reason
```

Rule:

```text
Supervisor agents should produce decisions/artifacts.
Gateway services should execute state transitions.
```

---

## 18. Agent-to-agent communication direction

There are two different cases.

### 18.1 Internal coordination first

Use internal task/session delegation for agents already under this gateway.

Example:

```text
Parent task: diagnose distributed bug
  Child task A: inspect local/frontend worker
  Child task B: inspect server/backend worker
  Child task C: adversarial synthesis/review
```

Gateway coordinates:

```text
child task creation
worker/backend assignment
artifact collection
session-to-session messages
result aggregation
parent timeline updates
```

This does not require official A2A first.

### 18.2 External A2A later

Use A2A only when crossing outside this gateway or talking to independent external agents/frameworks.

Good future A2A use cases:

```text
external specialist agent
remote framework not controlled by this gateway
agent discovery/capability cards
cross-framework task delegation
third-party agent service
```

Rule:

```text
Internal delegation first.
A2A facade later.
```

---

## 19. ACP guidance

ACP is useful for client/editor-to-coding-agent interoperability. It should not own this system.

Use ACP concepts for:

```text
session create/load/resume semantics
prompt-turn lifecycle
streamed agent updates
tool-call/diff/permission mapping when available
future editor integration
```

Map ACP into internal objects:

```text
ACP session -> Session
ACP prompt turn -> Run or Session message
ACP tool update -> Event / Artifact / ToolCall
ACP permission -> ApprovalRequest
ACP resume id -> upstream_resume_id
```

Do not use ACP for:

```text
task model
quota scheduling
worker routing
transport abstraction
project management
supervisor workflow
security policy
```

Rule:

```text
ACP is an adapter/bridge, not the architecture.
```

---

## 20. A2A guidance

A2A is useful for external agent-to-agent delegation. It should not be required for same-gateway coordination.

Use A2A later for:

```text
external agent discovery
capability declaration
remote delegated tasks
artifact exchange across framework boundaries
independent agent services
```

Map A2A into internal objects:

```text
A2A task -> Task or ChildTask
A2A artifact -> Artifact
A2A message -> Event / SessionMessage
A2A agent card -> ExternalAgentCapability
```

Do not use A2A for:

```text
basic web UI
Telegram integration
same-gateway child sessions
MCP replacement
worker daemon control
```

Rule:

```text
A2A is for boundary crossing.
The gateway's internal task/session model remains primary.
```

---

## 21. MCP guidance

MCP remains the tool/resource bridge.

Use MCP for:

```text
long script registration
repo tools
database access
structured tool calls
external resources
script/job completion notifications
```

Do not use MCP as:

```text
human interface protocol
session registry
agent-to-agent protocol
worker scheduler
core orchestration model
```

Rule:

```text
MCP tools are capabilities under policy, not trusted shortcuts around policy.
```

---

## 22. Web UI target

The first web UI should be a cockpit, not a ChatGPT clone.

Primary purpose:

```text
make parallel task/session/backend/worker state visible and controllable from phone or browser
```

Minimum surfaces:

```text
Dashboard
Task Queue
Active Runs
Session Timeline
Worker Status
Backend/Quota Status
Review Queue
Artifacts/Handoffs
Approvals
```

Minimum actions:

```text
create task
dispatch task
continue session
spawn fresh session from handoff
switch backend
move task to worker
request audit
approve fix pass
write handoff
cancel run
mark task blocked/completed
```

UI design principle:

```text
The web UI should expose the real system objects, not hide everything inside chat.
```

Chat remains useful inside a session timeline, but the cockpit is task/session/run oriented.

---

## 23. Phone-first deployment posture

The first interface should be a private web/PWA cockpit over Tailscale or equivalent private access.

Do not build native Android first unless there is a hard reason.

Reason:

```text
web/PWA gives faster iteration
same interface works on desktop and phone
WebSocket/event UI is easier to debug
future native app can reuse same gateway contract
```

Telegram remains useful as:

```text
notification surface
quick reply surface
fallback control surface
low-bandwidth interface
```

Telegram should stop being the only serious interface.

---

## 24. Security posture

The dangerous operation is not only YOLO mode.

The dangerous operation is:

```text
remote input -> spawn agent -> trusted workspace -> tools/files/secrets/network
```

Therefore:

```text
all transports are untrusted input
all backend agents are powerful but fallible executors
all tools/scripts need policy checks
all runs need event logs
all project roots need allowlists
all dangerous modes need explicit visibility
```

Permission profiles should be explicit:

```text
read_only
edit_project
run_tests
run_scripts
network_allowed
dangerous_yolo
```

The web UI is not automatically safe. It is safer only if it passes through the same policy system and is privately exposed.

Rule:

```text
Security belongs to Gateway Core + Policy, not to Telegram, Web UI, or the backend agent.
```

---

## 25. Refactor priorities for future coding agents

When a future coding agent opens the repo, it should follow this order.

### Priority 1 — Find existing seams

Inspect for existing equivalents of:

```text
backend interface
worker manager
state manager
session registry
job registry
Telegram command router
MCP/script registration
process supervisor
```

Do not duplicate them.

### Priority 2 — Extract transport-independent commands

If Telegram handlers currently own business logic, move that logic behind command handlers/services.

Target:

```text
Telegram handler -> parse message -> create command -> call gateway service
Web UI -> form/button -> create command -> call same gateway service
Supervisor -> decision -> create command -> call same gateway service
```

### Priority 3 — Add event log / event bus if missing

The web UI needs an event stream.

If events already exist, formalize them.

If events do not exist, introduce append-only events around current state changes.

### Priority 4 — Add WebSocket read model

Expose events to clients.

Start with read-only dashboard events before adding all write actions.

### Priority 5 — Add web commands

Once read model works, add structured commands for:

```text
create task
dispatch task
continue session
cancel run
request review
write handoff
```

### Priority 6 — Formalize workflow roles

Only after task/session/run/event contracts are stable.

---

## 26. Desired refactor sequence

Recommended sequence:

```text
1. Document current architecture from code.
2. Identify existing backend abstraction and preserve it.
3. Identify Telegram-specific business logic.
4. Extract command DTOs and command handlers.
5. Normalize task/session/run/worker/backend/event concepts.
6. Add or harden registries.
7. Add append-only event stream.
8. Add WebSocket endpoint.
9. Build read-only web dashboard.
10. Add write commands to web UI.
11. Add supervisor commands.
12. Add internal child-task/session delegation.
13. Add ACP/A2A bridges only where useful.
```

This sequence avoids a destructive rewrite.

---

## 27. Acceptance criteria for the next serious milestone

From a phone browser over private access, the user can:

```text
see workers online/offline
see available backends and quota/health state
see active tasks/runs/sessions
open a session timeline
create a task
dispatch task to selected backend + worker
continue an existing session
watch live events/output
request adversarial review
trigger fix pass
write or view handoff
receive Telegram notification for important state changes
```

Technical acceptance criteria:

```text
Telegram and Web UI use the same command handlers.
Backend-specific logic is not duplicated in UI code.
Worker/backend state is exposed through registries/services.
Events are persisted or replayable enough for UI recovery.
A session can be linked to a task and run.
A review/handoff can be attached to task/session.
```

---

## 28. Anti-patterns to block

Future agents should avoid:

```text
creating a second backend abstraction for the web UI
putting backend-specific logic in frontend routes
adding direct Telegram dependencies inside core services
making supervisor agents mutate state directly
introducing A2A before internal delegation exists
introducing ACP as the core task model
building native mobile before PWA/web cockpit
turning this into a general assistant platform
adding memory/skills as central architecture
rewriting working process supervision without necessity
```

---

## 29. Good implementation style

Prefer:

```text
small adapters
explicit capability flags
structured commands
append-only events
idempotent operations
registries before plugin SDK
thin interfaces
stable core services
private web UI first
manual commands before autonomy
```

Avoid:

```text
large magical orchestrators
autonomous chains without state gates
stringly-typed hidden workflows
transport-specific business logic
backend-specific UI branching
protocol-first architecture
```

---

## 30. Reference patterns to keep in mind

These references are not dependencies. They are design anchors.

### ACP

Use as reference for coding-agent session/prompt/tool-call semantics and future editor compatibility.

Relevant concept:

```text
client/editor <-> coding agent session
```

### A2A

Use as reference for external agent-to-agent delegation across framework boundaries.

Relevant concept:

```text
independent agent <-> independent agent task/artifact exchange
```

### MCP

Use as reference for tool/resource access.

Relevant concept:

```text
agent <-> tools/resources
```

### OpenClaw-style architecture

Use as reference for:

```text
Gateway WebSocket
client role/scope
session routing
channel separation
runtime adapters
subagent/session tooling
```

### Hermes-style architecture

Use as reference for:

```text
one core exposed through many entrypoints
registries
tool/provider discovery
persistent sessions
optional integrations
```

---

## 31. Final locked guidance

This project should continue as:

```text
an existing mature remote coding-agent operations system being refactored toward a transport-neutral cockpit architecture
```

The immediate engineering aim is:

```text
make Telegram, Web UI, future mobile UI, supervisor workflows, and future agent-to-agent coordination all use the same core abstractions
```

The main product aim is:

```text
let the user operate multiple coding agents across machines, sessions, quotas, reviews, and handoffs without being physically at the PC
```

The main architecture rule is:

```text
core first, adapters around it, events out of it, commands into it
```

