# Telegram Coding Gateway — Desired State, Required Changes, and LLM Implementation Prompt

## 1. What this project is actually aiming at

This project is **not** trying to become a general autonomous agent framework.

It is trying to become a **safe, lightweight, session-aware remote gateway** that lets Telegram control a local coding agent running on any machine.

The intended use is:

- A machine runs this gateway locally
- Telegram acts as the remote control interface
- The gateway can start or continue work through a local coding agent such as Claude Code or Codex
- The gateway keeps enough explicit state to let work continue across turns
- The gateway remains constrained, inspectable, file-backed, and operator-controlled
- The gateway does **not** become a broad always-on autonomous agent with opaque memory and dangerous generalized permissions

In one sentence:

**This system should become a Telegram-controlled remote shell for local coding agents, with persistent session routing, safe execution boundaries, and resumable work.**

---

## 2. The key architectural conclusion

The correct way to maintain continuity is:

- use the coding agent's **native resume/continue/session mechanism**
- store a lightweight gateway-level session record in this repo
- map Telegram conversations to those session records
- resume the agent session on demand when the next Telegram instruction arrives

The system should **not** be built primarily around:

- keeping a terminal window open forever
- injecting text into a live shell as the core session model
- relying on fragile PTY/stdin/stdout persistence as the main continuity mechanism

Live terminal attachment may exist later as an optional mode for streaming or active monitoring, but it should **not** be the backbone.

### Correct session model

**Telegram session -> local gateway session record -> native Claude/Codex session ID -> resume on demand**

### Wrong backbone

**Telegram session -> permanently alive terminal process -> keep writing messages into stdin**

---

## 3. Honest assessment of current repo state

The current repo already has the right foundation.

It already contains:

- a task orchestrator
- a Telegram interface
- a Claude bridge
- persistence for queue/state/artifacts
- constrained execution patterns
- cancellation/status/reporting

That means the project is already pointed in the right direction.

What it currently is:

- a **task runner** for a local coding agent

What it is not yet:

- a **session-aware remote coding gateway**

The missing piece is not a bigger framework.

The missing piece is:

**first-class session semantics**

---

## 4. Desired end state

The desired end state is a system with the following properties.

### Core behavior

- A user can open a session from Telegram against a specific machine/repo/path/backend
- A user can continue that same session later from Telegram
- The gateway knows which coding backend the session uses
- The gateway stores explicit local metadata for the session
- The gateway resumes Claude/Codex using the backend's native session/continue mechanism
- The user can inspect, switch, list, cancel, and close sessions
- The system remains file-backed and debuggable

### Safety model

- bounded tools
- bounded cwd/repo scope
- explicit backend selection
- explicit session ownership/routing
- rate limits
- allowlists
- cancellation
- no uncontrolled autonomous behavior

### Operational model

- sessions are persistent
- tasks are events inside sessions
- results/artifacts are attached to sessions
- context is compacted explicitly
- resumes are native to the backend, not reimplemented through terminal hacks

---

## 5. What needs to change in the repo

## A. Introduce a first-class Session model

Create a session abstraction that is independent from individual tasks.

Each session should store at minimum:

- `session_id` — gateway-level stable identifier
- `backend` — e.g. `claude`, `codex`
- `backend_session_id` — native session/thread/conversation ID if available
- `machine_id` — machine identity / hostname / configured node name
- `repo_path` or `cwd`
- `status` — `idle`, `busy`, `awaiting_input`, `error`, `closed`
- `created_at`
- `updated_at`
- `last_task_id`
- `last_artifact_path`
- `last_summary`
- `last_user_message`
- `last_result_summary`
- optional `telegram_chat_id`
- optional `telegram_thread_id`
- optional `owner_user_id`

Store this in file-backed form first.

Suggested path:
- `state/sessions/<session_id>.json`

---

## B. Separate sessions from tasks

Right now the system is task-centric.

It needs to become:

- sessions = long-lived unit of continuity
- tasks/messages = things that happen inside a session

That means:

- creating a Telegram message should not automatically imply a brand-new independent task
- instead, Telegram input should route to an active session when one exists
- ad hoc tasks can still exist, but session-based operation should become the main flow

---

## C. Add Telegram session routing

The Telegram interface should gain explicit session management.

Add commands such as:

- `/session_new <backend> <path>`
- `/session_list`
- `/session_use <session_id>`
- `/session_status [session_id]`
- `/session_close <session_id>`
- `/session_cancel <session_id>`
- `/say <message>`
- `/run <instruction>`

Behavioral requirement:

- each Telegram user/chat/thread should be able to have an active session binding
- plain follow-up messages should go to the active session unless explicitly overridden
- replies should reference the session they operated on

---

## D. Add backend abstraction

The repo should stop assuming a one-off Claude-only execution shape.

Introduce a backend interface such as:

```python
class CodingBackend(Protocol):
    def create_session(self, session: Session) -> SessionStartResult: ...
    def resume_session(self, session: Session, message: str) -> ExecutionResult: ...
    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult: ...
    def cancel(self, session: Session) -> None: ...
    def summarize(self, session: Session) -> str: ...
    def close(self, session: Session) -> None: ...

Implement first:

ClaudeCodeBackend
CodexBackend

The local llama component should remain optional and narrow:

prompt extension
routing
compact summarization
command shaping

It should not become the main agent brain.

E. Use native resume/continue mechanisms

This is the most important implementation rule.

Do not invent your own session continuity through terminal persistence if the backend already supports continuation.

The repo should prefer:

Claude Code native resume
Codex native resume

Gateway responsibilities:

track which native session belongs to which gateway session
know how to invoke resume for each backend
know how to update local metadata after each turn

Backend responsibilities:

maintain actual coding conversation continuity
F. Add compact session context

Even when native backend resume exists, the gateway should maintain a compact explicit summary.

For each session, keep:

objective
current repo/path
recent instructions
latest files changed
known blockers
next expected step
latest artifact references

This summary should be updated after every completed turn.

Purpose:

recover visibility
support inspection/debugging
help rebuild if backend history is unavailable
avoid opaque state

Suggested paths:

state/sessions/<session_id>.json
state/summaries/<session_id>.md
G. Preserve file-backed explicit state

Do not overengineer this into a database-heavy system first.

The current project already has a file-backed style. Keep that style.

Suggested structure:

state/
  sessions/
    <session_id>.json
  telegram/
    active_bindings.json
  summaries/
    <session_id>.md
results/
  sessions/
    <session_id>/
      <timestamp>_result.json
      <timestamp>_summary.md
      <timestamp>_artifacts.json
logs/
  session_events/
    <session_id>.log
H. Add session lifecycle management

Support these transitions:

created
active
busy
awaiting_input
idle
error
cancelled
closed

Required operations:

create
resume
inspect
list
cancel
retry
close
I. Add observability

Every session should be inspectable without reading raw code output manually.

Track:

backend
backend session id
cwd/repo
last start time
last completion time
last error
files changed on last turn
artifact locations
last summary
active/inactive state

Telegram should expose concise status reporting.

J. Keep live terminal support optional

Do not make "keep subprocess alive forever" the core architecture.

Optional later feature:

live attached mode for streaming long-running activity

Use cases:

watching tests live
monitoring a long-running debug/refactor
interactive human-in-the-loop commands

But the base system should work without this.

Primary mode remains:

resume native session on demand
run turn
collect result
persist summary
idle
6. What should not happen

The repo should not drift into:

generalized autonomous agent orchestration
multi-agent planning systems
tool-discovery frameworks
broad self-directed execution across the machine
opaque memory systems
terminal-PTY hacks as the main persistence model
replacing explicit state with "the process is still alive"

That is not the target product.

7. Clear implementation priority

Build in this order:

Phase 1 — Session foundation
create Session model
persist sessions to disk
add active Telegram session binding
add session CRUD commands
Phase 2 — Backend session support
abstract backend interface
implement Claude backend resume flow
implement Codex backend resume flow
store native backend session IDs
Phase 3 — Session execution flow
route Telegram messages to active sessions
execute backend resume for each follow-up message
capture result
update summary and artifacts
Phase 4 — Observability
session status commands
event logs
changed file reporting
compact summaries
Phase 5 — Optional enhancements
live streaming mode
attached terminal mode
web UI
better summarization
machine registry / multi-node awareness
8. Final target definition

The final product should be:

A safe Telegram-controlled gateway that can open, continue, inspect, and manage persistent coding sessions on any machine by orchestrating local coding agents like Claude Code or Codex, while keeping state explicit, file-backed, resumable, and tightly constrained.