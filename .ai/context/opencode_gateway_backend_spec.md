# OpenCode Gateway Backend Specification

## Purpose

Add OpenCode as a backend for a local phone-to-coding-agent gateway.

The gateway should allow a user to send coding tasks from a phone, run OpenCode locally in non-interactive mode, collect results, persist session state, and continue the same OpenCode session from later follow-up messages.

This specification assumes a phased implementation:

1. Build OpenCode CLI backend first.
2. Use explicit session IDs from the beginning.
3. Avoid fragile latest-session behavior.
4. Add OpenCode server/API mode later behind a feature flag.
5. Preserve the same gateway interface regardless of backend mode.

---

## Final Architecture

```text
Phone
  ↓
Gateway API / Telegram Bot / Local Controller
  ↓
AgentBackend Interface
  ↓
OpenCodeBackend
  ├── Phase 1: CLI subprocess mode
  └── Phase 2: OpenCode server/API mode
  ↓
Repo Worktree / Branch / Sandbox
```

The gateway must not care whether OpenCode is driven through CLI or HTTP.

The gateway should only call backend-level methods:

```text
start_task()
continue_task()
get_task()
list_tasks()
abort_task()
get_diff()
```

---

## Core Decision

Use **CLI-first with explicit session control**, then add **server/API mode as a second backend**.

Do not build production behavior around:

```bash
opencode run --continue "follow-up"
```

Use explicit session IDs:

```bash
opencode run --session "<opencode_session_id>" "follow-up"
```

`--continue` targets the latest session and is fragile when multiple repositories, agents, tasks, or phone-triggered runs exist.

---

## Required OpenCode CLI Commands

### Start New Non-Interactive Task

```bash
opencode run \
  --dir "<repo_path>" \
  --format json \
  --title "<gateway_task_title>" \
  "<prompt>"
```

### Continue Exact Existing Task

```bash
opencode run \
  --dir "<repo_path>" \
  --format json \
  --session "<opencode_session_id>" \
  "<follow_up_prompt>"
```

### Optional Flags

```bash
--model "<provider/model>"
--agent "<agent_name>"
```

### Forbidden Production Pattern

```bash
opencode run --continue "follow-up"
```

`--continue` may be acceptable for manual terminal usage. It should not be used by the gateway except as a deliberate manual fallback.

---

## Implementation Phases

---

# Phase 1 — OpenCode CLI Backend

Implement:

```text
OpenCodeCliBackend
```

The backend must run OpenCode through subprocess calls.

### Required Behavior

The backend must:

1. Start OpenCode non-interactively.
2. Capture `stdout`.
3. Capture `stderr`.
4. Parse JSON events from `stdout`.
5. Extract OpenCode session ID.
6. Store the session ID against a gateway task ID.
7. Continue only by explicit session ID.
8. Collect git diff after each run.
9. Return final response to the phone gateway.
10. Store full run logs locally.

---

## Command Builder

Use subprocess argument arrays.

Do not shell-concatenate commands.

```python
cmd = [
    "opencode",
    "run",
    "--dir", repo_path,
    "--format", "json",
]

if model:
    cmd += ["--model", model]

if agent:
    cmd += ["--agent", agent]

if session_id:
    cmd += ["--session", session_id]
else:
    cmd += ["--title", title]

cmd.append(prompt)
```

---

## Backend Interface

Use a shared backend interface so OpenCode can later switch from CLI mode to server mode without changing gateway logic.

```python
class AgentBackend:
    def start_task(
        self,
        repo_path: str,
        prompt: str,
        title: str | None = None,
        model: str | None = None,
        agent: str | None = None,
    ) -> AgentRunResult:
        ...

    def continue_task(
        self,
        gateway_task_id: str,
        prompt: str,
    ) -> AgentRunResult:
        ...

    def get_task(
        self,
        gateway_task_id: str,
    ) -> AgentTask:
        ...

    def get_diff(
        self,
        gateway_task_id: str,
    ) -> AgentDiff:
        ...

    def abort_task(
        self,
        gateway_task_id: str,
    ) -> AgentAbortResult:
        ...
```

---

## Result Object

```python
from dataclasses import dataclass

@dataclass
class AgentRunResult:
    gateway_task_id: str
    backend: str
    repo_path: str
    session_id: str | None
    status: str
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    parsed_events: list[dict]
    final_message: str | None
    changed_files: list[str]
    git_diff_stat: str | None
    git_diff: str | None
    started_at: str
    finished_at: str
    duration_seconds: float
    error: str | None
```

---

# Phase 2 — Session Persistence

Use SQLite unless the project already has a storage layer.

## Table: `agent_tasks`

```sql
CREATE TABLE agent_tasks (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    title TEXT,
    opencode_session_id TEXT,
    status TEXT NOT NULL,
    model TEXT,
    agent TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_exit_code INTEGER,
    last_stdout_path TEXT,
    last_stderr_path TEXT,
    last_final_message TEXT,
    last_error TEXT
);
```

## Required Statuses

```text
created
running
completed
failed
timed_out
needs_manual_attention
aborted
```

## Continuation Rules

`continue_task(gateway_task_id, prompt)` must:

1. Load the stored gateway task.
2. Read the stored OpenCode session ID.
3. Reject continuation if no session ID exists.
4. Run OpenCode with `--session <opencode_session_id>`.
5. Never silently fall back to latest session.
6. Never call `--continue` automatically.

---

## Session ID Extraction

Session ID extraction must be strict.

### Extraction Order

1. Parse `stdout` as OpenCode JSON events.
2. Extract session ID from official event fields if present.
3. If missing, run:

```bash
opencode session list --format json --max-count 10
```

4. Match by title, repo path if available, and recency.
5. If still missing, mark the task:

```text
needs_manual_attention
```

6. Do not continue the session until the session ID is known.

## Rule

Do not hard-code only one session ID format. Common OpenCode sessions may use a `ses_...` shape, but the backend should parse JSON fields rather than rely only on regex.

---

# Phase 3 — Git Safety

Before every OpenCode run, validate the repository.

## Pre-Run Commands

```bash
git status --porcelain
git branch --show-current
git rev-parse --show-toplevel
```

## Pre-Run Checks

The backend must verify:

1. `repo_path` exists.
2. `repo_path` is inside an allowlisted root.
3. `repo_path` is a Git repository.
4. The current branch is known.
5. Dirty state is known.

## Dirty Repo Policy

Default:

```text
Reject dirty repositories unless config explicitly allows dirty runs.
```

Better default for the phone gateway:

```text
Create a dedicated branch/worktree per gateway task.
```

Example:

```bash
git worktree add "../worktrees/<task_id>" -b "agent/<task_id>"
```

---

## Post-Run Git Collection

After every OpenCode run:

```bash
git status --porcelain
git diff --stat
git diff
```

Store:

1. Changed files.
2. Diff stat.
3. Full diff.
4. Dirty status.
5. Optional test result.

The phone response should never rely only on the agent's final message. It must include actual repository state.

---

# Phase 4 — Timeout, Locking, and Logging

## Required Controls

Add:

1. Per-run timeout.
2. Repo-level lock.
3. Task-level lock.
4. Full command audit log.
5. `stdout` log file.
6. `stderr` log file.
7. Structured logs.
8. Clear status transitions.

## Concurrency Rule

Only one mutating OpenCode task may run per repo/worktree at a time.

Minimum locking:

```text
<repo_path>/.gateway-lock
```

Better locking:

```text
SQLite advisory task lock + file lock
```

---

# Phase 5 — Gateway Integration

Expose OpenCode through the same interface as Claude Code and Codex backends.

## Required Gateway Functions

```python
start_agent_task(
    backend: str,
    repo_path: str,
    prompt: str,
    title: str | None = None,
    model: str | None = None,
    agent: str | None = None,
)

continue_agent_task(
    gateway_task_id: str,
    prompt: str,
)

list_agent_tasks()

get_agent_task(
    gateway_task_id: str,
)

get_agent_task_diff(
    gateway_task_id: str,
)
```

## Backend Selection

If:

```text
backend == "opencode"
```

Then use:

```text
OpenCodeCliBackend
```

until server mode is explicitly enabled.

---

## Phone Response Format

### Completed

```text
Status: completed
Backend: opencode
Task: <gateway_task_id>
Session: <opencode_session_id>

Changed files:
- <file>

Tests:
<test status if available>

Agent:
<final message>
```

### Failed

```text
Status: failed
Backend: opencode
Task: <gateway_task_id>
Session: <opencode_session_id or null>
Exit code: <exit_code>

Error:
<short error>

Manual attention required:
<reason if applicable>
```

### Timed Out

```text
Status: timed_out
Backend: opencode
Task: <gateway_task_id>
Session: <opencode_session_id or null>

Error:
OpenCode exceeded timeout.

Manual attention required:
Check logs and repository state.
```

---

# Phase 6 — Optional Test Command

Add configuration:

```json
{
  "opencode": {
    "default_model": null,
    "default_agent": null,
    "timeout_seconds": 1800,
    "allow_dirty_repo": false,
    "collect_diff": true,
    "run_tests_after": false,
    "test_command": null
  }
}
```

If:

```json
"run_tests_after": true
```

Then run the configured test command after OpenCode finishes.

Capture:

1. Test `stdout`.
2. Test `stderr`.
3. Test exit code.
4. Pass/fail status.

---

# Phase 7 — OpenCode Server Mode Prototype

Add server mode only after CLI mode works end-to-end.

Implement:

```text
OpenCodeServerBackend
```

behind a feature flag.

Do not replace CLI mode.

## Server Config

```json
{
  "opencode": {
    "mode": "cli",
    "server": {
      "enabled": false,
      "host": "127.0.0.1",
      "port": 4096,
      "username": "opencode",
      "password_env": "OPENCODE_SERVER_PASSWORD"
    }
  }
}
```

## Server Startup

```bash
opencode serve \
  --hostname 127.0.0.1 \
  --port 4096
```

## Server Backend Requirements

The server backend should support:

1. Start or connect to OpenCode server.
2. Use auth if configured.
3. Create sessions through HTTP API if available.
4. Send messages to a specific session.
5. Retrieve messages and session state.
6. Retrieve diffs if available.
7. Abort a running task if available.
8. Stream or poll events if available.
9. Return the same `AgentRunResult` object as CLI mode.

## Design Rule

The gateway layer must not care whether OpenCode runs through CLI or server.

---

# Phase 8 — Tests

Use mocked subprocess calls.

Unit tests must not require OpenCode to be installed.

## Required Tests

1. Starting a task successfully.
2. Continuing a task successfully.
3. Missing session ID.
4. OpenCode nonzero exit.
5. Timeout.
6. Dirty repo rejection.
7. Diff collection.
8. JSON parse failure.
9. Session list fallback.
10. Concurrent same-repo lock rejection.
11. Gateway response formatting.
12. Status transition correctness.

---

# Phase 9 — README

Add a short README section documenting:

1. How to enable the OpenCode backend.
2. Required OpenCode installation check.
3. Example start task.
4. Example continue task.
5. How session IDs are stored.
6. Why `--session` is used instead of `--continue`.
7. How to inspect logs.
8. How to enable experimental server mode.
9. Known limitations.
10. Recovery steps for `needs_manual_attention`.

---

# Pitfalls and Required Avoidance Rules

## Pitfall 1 — Latest Session Ambiguity

Bad:

```bash
opencode run --continue "continue"
```

Good:

```bash
opencode run --session "$SESSION_ID" "continue"
```

Rule:

```text
The gateway must never accidentally continue the wrong/latest session.
```

---

## Pitfall 2 — Missing Session ID

Bad behavior:

```text
Session ID missing, so use latest session.
```

Correct behavior:

```text
Session ID missing, so mark needs_manual_attention.
```

---

## Pitfall 3 — Trusting Agent Summary

Bad behavior:

```text
Agent says it changed files, so assume it did.
```

Correct behavior:

```text
Collect git status, git diff --stat, and git diff after every run.
```

---

## Pitfall 4 — Dirty Repo Damage

Bad behavior:

```text
Run agent against dirty working tree without warning.
```

Correct behavior:

```text
Reject dirty repo or create isolated worktree.
```

---

## Pitfall 5 — Concurrent Repo Mutation

Bad behavior:

```text
Allow two OpenCode sessions to edit the same repo at once.
```

Correct behavior:

```text
Use repo-level lock or per-task worktree.
```

---

## Pitfall 6 — Permission Prompt Deadlock

Bad behavior:

```text
Let unattended run hang forever waiting for permission.
```

Correct behavior:

```text
Use timeout, logs, explicit permission config, and manual-attention state.
```

---

## Pitfall 7 — Server Mode Overbuild

Bad behavior:

```text
Start with server mode before CLI mode works.
```

Correct behavior:

```text
Implement CLI mode first, then add server mode behind a feature flag.
```

---

# Recommended Final Build Sequence

```text
1. Add OpenCodeCliBackend.
2. Add session persistence.
3. Add explicit continuation by --session.
4. Add JSON parsing.
5. Add diff/test collection.
6. Add repo/task locks.
7. Add phone response formatting.
8. Test one repo, one task, several follow-ups.
9. Test failed command.
10. Test timeout.
11. Test missing session ID.
12. Add OpenCodeServerBackend behind feature flag.
13. Compare CLI vs server behavior on same task.
14. Promote server mode only if it is better locally.
```

---

# Acceptance Criteria

The implementation is complete when:

1. A new OpenCode task can be sent from the phone.
2. The gateway runs OpenCode non-interactively.
3. The gateway captures the final response.
4. The gateway stores the OpenCode session ID.
5. A later phone message continues the same OpenCode session.
6. The gateway returns changed files and diff summary.
7. The gateway never accidentally continues the wrong/latest session.
8. Failure states are explicit and recoverable.
9. CLI mode works before server mode is attempted.
10. Server mode is implemented only behind a feature flag.

---

# Handoff Prompt for Implementation Agent

```text
You are implementing an OpenCode backend for my local phone-to-coding-agent gateway.

Context:
I already use coding agents from my phone by sending tasks to a local gateway. The gateway runs local coding agents non-interactively and sends the result back to my phone. I want OpenCode added as a backend with proper session control from the beginning.

Goal:
Add OpenCode support in a production-shaped but minimal way.

Core requirement:
OpenCode must support:
1. Starting a non-interactive task.
2. Capturing stdout/stderr.
3. Parsing machine-readable output.
4. Persisting the OpenCode session ID.
5. Continuing the same OpenCode session from later phone messages.
6. Returning final response, changed files, and test status to the gateway.
7. Avoiding fragile “latest session” behavior.

Important:
Do not build around `opencode run --continue`.
Use explicit session IDs only.

PHASE 1 — OpenCode CLI backend

Implement `OpenCodeCliBackend`.

Start command:

opencode run --dir <repo_path> --format json --title <title> "<prompt>"

Continue command:

opencode run --dir <repo_path> --format json --session <opencode_session_id> "<prompt>"

Optional flags:
- --model <provider/model>
- --agent <agent_name>

Never shell-concatenate commands. Use subprocess argument arrays.

Required inputs:
- repo_path
- prompt
- title optional
- model optional
- agent optional
- gateway_task_id optional for continuation

Required output object:
- gateway_task_id
- backend = "opencode"
- repo_path
- opencode_session_id
- status
- command
- exit_code
- stdout
- stderr
- parsed_events
- final_message
- changed_files
- git_diff_stat
- git_diff
- started_at
- finished_at
- duration_seconds
- error

PHASE 2 — Session persistence

Create persistent storage for gateway tasks.

Use SQLite unless the existing project already has a storage layer.

Store:
- gateway_task_id
- backend
- repo_path
- title
- opencode_session_id
- status
- model
- agent
- created_at
- updated_at
- last_exit_code
- last_stdout_path
- last_stderr_path
- last_final_message
- last_error

Continuation behavior:
- `continue_task(gateway_task_id, prompt)` must load the stored OpenCode session ID.
- If no session ID exists, reject continuation and mark `needs_manual_attention`.
- Never fall back to latest session.
- Never call `--continue` automatically.

Session ID extraction:
1. Parse stdout as OpenCode JSON events.
2. Extract session ID from official event fields if present.
3. If missing, run:
   opencode session list --format json --max-count 10
4. Match by title, repo path if available, and recency.
5. If still missing, mark `needs_manual_attention`.

PHASE 3 — Git safety

Before every run:
- verify repo path exists
- verify it is inside an allowlisted root
- verify it is a git repo
- collect current branch
- collect dirty state with `git status --porcelain`

Default safety rule:
- If repo is dirty, reject the run unless config allows dirty runs.
- Prefer creating a dedicated branch/worktree per gateway task if the project already supports that.

After every run:
- run `git status --porcelain`
- run `git diff --stat`
- run `git diff`
- extract changed files
- store diff output
- include changed files summary in gateway response

PHASE 4 — Timeout, locking, and logging

Add:
- per-run timeout
- repo-level lock
- task-level lock
- full command audit log
- stdout/stderr log files
- structured logs

Statuses:
- created
- running
- completed
- failed
- timed_out
- needs_manual_attention
- aborted

Concurrency rule:
Only one mutating OpenCode task may run per repo/worktree at a time.

PHASE 5 — Gateway integration

Expose OpenCode through the same interface as existing Claude Code/Codex backends.

Required functions:
- start_agent_task(backend, repo_path, prompt, title=None, model=None, agent=None)
- continue_agent_task(gateway_task_id, prompt)
- list_agent_tasks()
- get_agent_task(gateway_task_id)
- get_agent_task_diff(gateway_task_id)

Phone response format:

Completed:
Status: completed
Backend: opencode
Task: <gateway_task_id>
Session: <opencode_session_id>
Changed files:
- <file>

Tests:
<test status if available>

Agent:
<final message>

Failed:
Status: failed
Backend: opencode
Task: <gateway_task_id>
Session: <opencode_session_id or null>
Exit code: <exit_code>

Error:
<short error>

Manual attention required:
<reason>

PHASE 6 — Optional test command

Add config:

{
  "opencode": {
    "default_model": null,
    "default_agent": null,
    "timeout_seconds": 1800,
    "allow_dirty_repo": false,
    "collect_diff": true,
    "run_tests_after": false,
    "test_command": null
  }
}

If `run_tests_after = true`, run the configured test command after OpenCode finishes.
Capture:
- test stdout
- test stderr
- test exit code
- pass/fail status

PHASE 7 — Server mode prototype behind feature flag

After CLI mode works end-to-end, add `OpenCodeServerBackend`.

Do not replace CLI mode.

Config:

{
  "opencode": {
    "mode": "cli",
    "server": {
      "enabled": false,
      "host": "127.0.0.1",
      "port": 4096,
      "username": "opencode",
      "password_env": "OPENCODE_SERVER_PASSWORD"
    }
  }
}

Server backend requirements:
- start/connect to `opencode serve --hostname 127.0.0.1 --port <port>`
- support auth if configured
- create sessions through HTTP API if available
- send messages to a specific session
- retrieve messages/session state
- retrieve diffs if available
- abort if available
- stream or poll events if available
- return the same result object as CLI mode

Design rule:
The gateway layer must not care whether OpenCode runs through CLI or server.

PHASE 8 — Tests

Add mocked tests for:
1. starting a task successfully
2. continuing a task successfully
3. missing session ID
4. OpenCode nonzero exit
5. timeout
6. dirty repo rejection
7. diff collection
8. JSON parse failure
9. session list fallback
10. concurrent same-repo lock rejection

Use mocked subprocess calls. Do not require OpenCode installed for unit tests.

PHASE 9 — README

Add a short README section:

- how to enable OpenCode backend
- required OpenCode install check
- example start task
- example continue task
- how session IDs are stored
- why `--session` is used instead of `--continue`
- how to inspect logs
- how to enable experimental server mode

Acceptance criteria:
1. I can send a new OpenCode task from my phone.
2. The gateway runs OpenCode non-interactively.
3. The gateway captures the final message.
4. The gateway stores the OpenCode session ID.
5. I can send a follow-up from my phone and it continues the same OpenCode session.
6. The gateway returns changed files and diff summary.
7. The system never accidentally continues the wrong/latest session.
8. Failure states are explicit and recoverable.
9. CLI mode works before server mode is attempted.
10. Server mode is implemented only behind a feature flag.
```

---

# Final Decision

```text
Build OpenCode CLI backend first.
Use explicit --session from the beginning.
Persist every session.
Never depend on --continue.
Collect git diff after every run.
Add server mode second, behind a feature flag.
Promote server mode only after local reliability testing.
```
