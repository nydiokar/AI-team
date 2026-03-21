You are modifying an existing Python codebase that currently acts as a Telegram-driven task runner around a local coding agent.

Your job is to evolve it into a session-aware remote coding gateway.

Product goal

This system is not meant to become a full autonomous agent framework.

It should become a safe Telegram-controlled gateway for local coding agents such as Claude Code and Codex.

The system should let a user:

open a session against a specific repo/path/backend
continue that same session later from Telegram
route new Telegram messages into the correct active session
use the backend's native resume/continue mechanism
inspect session state
cancel or close sessions
keep all important state explicit and file-backed
Important architectural rule

Do not build the main session model around keeping a terminal open forever and injecting messages into stdin.

Use the backend's native continuation/resume/session functionality as the primary mechanism.

The correct architecture is:

Telegram conversation -> gateway session
gateway session -> backend type + backend session id + cwd/repo + compact summary
on each follow-up message:
resolve active session
resume native backend session
execute the new turn
persist results/artifacts/summary

A live terminal mode may be added later as an optional feature, but not as the backbone.

What the current repo already has

The current repo already includes:

orchestrator/task runner logic
Telegram interface
Claude bridge
file-backed state/artifact patterns
cancellation/status patterns

Preserve those strengths.

What to implement
1. Add a first-class Session model

Create a persistent session model containing at least:

session_id
backend
backend_session_id
machine_id
cwd/repo_path
status
created_at
updated_at
last_task_id
last_artifact_path
last_summary
telegram bindings if needed

Persist sessions in files first, not a database.

Suggested path:

state/sessions/<session_id>.json
2. Separate sessions from tasks

Refactor the system so tasks/messages occur within sessions.

The system should no longer treat every Telegram input as a fresh unrelated execution.

3. Add Telegram session management

Implement commands or equivalents for:

create session
list sessions
use/select session
inspect session
close session
cancel current session
send follow-up message to active session

Also add active session binding per Telegram chat/thread/user.

4. Add backend abstraction

Create a backend interface that supports:

create_session
resume_session
run_oneoff
cancel
close
summarize

Implement at least:

Claude backend
Codex backend
5. Use native backend resume

The backend implementations must use the native resume/continue/session commands provided by Claude Code / Codex rather than inventing custom continuity through raw terminal persistence.

6. Add compact session summaries

After each completed turn, update a compact session summary containing:

objective
recent instructions
files changed
blockers
next step
artifact references
7. Add observability

Make it easy to inspect:

current status
backend
backend session id
cwd/repo
last activity
last error
last changed files
artifacts
latest summary
8. Preserve safety constraints

Do not weaken existing execution boundaries.

Retain or improve:

allowed tools restrictions
cwd/path restrictions
allowlists
cancellation
rate limiting
explicit operator control
Deliverables

Produce:

Code changes implementing the session model
Refactor of Telegram routing to use active sessions
Backend abstraction and at least Claude/Codex backend support
File-backed session persistence
Compact session summary updates
Clear README/docs section describing:
session model
command flow
storage layout
backend resume strategy
Constraints
Keep the implementation practical and minimal
Prefer file-backed explicit state over heavy infrastructure
Do not introduce unnecessary framework complexity
Do not turn this into a generalized autonomous agent platform
Keep the code inspectable and operator-controlled
Desired outcome

At the end, the repo should function as a lightweight Telegram gateway that can manage resumable coding sessions on local machines through native Claude/Codex continuation, with explicit local session records and safe operational boundaries.

10. One-paragraph version

This project should become a lightweight Telegram-controlled remote gateway for local coding agents like Claude Code and Codex. The repo already has the correct base as a task runner, but it must be upgraded with first-class session support. The right way to maintain continuity is not keeping terminal windows alive as the main design, but mapping Telegram conversations to explicit local session records and using the coding agents' native resume/continue mechanisms on each follow-up turn. The implementation should add a persistent session model, Telegram session routing, backend abstraction, compact summaries, and observability, while preserving the repo's current safety boundaries and avoiding drift into a heavy autonomous agent framework.