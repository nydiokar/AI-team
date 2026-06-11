# Remote Failure Diagnostics Gap

Date: 2026-06-11

## Incident

Telegram reported this for a remote Claude session:

```text
#s_2ba1b4aee6d2 #t_task_6cbee27a
Task failed: Claude failed
```

The gateway artifact for `task_6cbee27a` contains no useful diagnostic payload:

- `errors`: `["Claude exited with code 1"]`
- `raw_stdout`: empty
- `raw_stderr`: empty
- `parsed_output.content`: empty
- `execution_time`: `0.0` on the gateway-side artifact

The controller event stream shows the remote node did run the task:

- gateway `kanebra` created and dispatched the task at `2026-06-11T18:21:27`
- remote node `Horse` emitted `task_failed` at `2026-06-11T18:22:09`
- remote duration was about `35.375s`
- `error_detail` was empty

So the failure was real, but the system lost the data needed to explain it.

## What Regressed In The Gateway/Worker Split

Before the mesh split, the gateway process ran the backend locally. Local
execution converts backend `ExecutionResult` into a full `TaskResult`, preserving:

- `raw_stdout`
- `raw_stderr`
- `parsed_output`
- `return_code`
- `backend_session_id`
- file metadata

That local path is still visible in `src/orchestrator.py` inside
`_run_backend_local`.

After the worker split, the remote worker became the process that actually sees
the backend subprocess output. That means the remote worker must forward the same
diagnostic fields back to the task server, and the gateway must persist them into
the artifact. Today it does not.

## Current Loss Points

### 1. Worker truncates the backend result shape

`src/worker/agent.py` builds an `ExecutionResultPayload`-compatible dictionary
from backend `ExecutionResult`, but only forwards a small subset:

- `success`
- `output`
- `errors`
- `files_modified`
- `execution_time`
- `timestamp`
- `return_code`
- `backend_session_id`

It does not forward:

- `raw_stdout`
- `raw_stderr`
- `parsed_output`
- `file_changes`
- backend-specific diagnostics

If Claude exits with code `1` and the backend parser cannot extract a cleaner
error, the worker may only send `Claude exited with code 1`.

### 2. Task server discards result JSON on failures

`src/control/task_server.py::submit_result` builds `result_dict`, but on failure
it calls:

```python
db.fail_task(task_id, error_str)
```

`db.fail_task()` only writes `mesh_tasks.status`, `mesh_tasks.error`,
`completed_at`, and `updated_at`. It does not store `result_dict` in
`mesh_tasks.result`.

For `task_6cbee27a`, the DB row confirms this:

- `status = failed`
- `error = Claude exited with code 1`
- `result = NULL`

This makes the failure unrecoverable from the gateway side even if the worker had
sent richer fields.

### 3. Gateway reconstructs remote failures from the short error only

The gateway polls the DB for remote completion. When it sees a failed remote row,
it builds a `TaskResult` from the row error string. Because `mesh_tasks.result`
is empty, the artifact and Telegram notification can only say the generic error.

### 4. Telegram summarizer is backend-biased and fallback-heavy

`TaskOrchestrator._short_failure_reason()` has several hardcoded Claude labels.
When it cannot classify the failure, it falls back to `Claude failed`. That is
especially misleading for other backends, and still unhelpful for Claude if the
raw diagnostics were dropped earlier.

## Required Fix

Remote execution should preserve diagnostic parity with local execution. The
server needs enough data to answer: what command/backend failed, what did stdout
say, what did stderr say, what structured event did the backend emit, and what
node/session/task produced it.

### 1. Extend `ExecutionResultPayload`

Add fields to `src/control/task_server.py::ExecutionResultPayload`:

- `raw_stdout: str = ""`
- `raw_stderr: str = ""`
- `parsed_output: Optional[Any] = None`
- `file_changes: List[Dict[str, Any]] = []`
- optionally `error_class: str = ""`

Cap large text fields before transport or storage. Suggested caps:

- full `raw_stdout`/`raw_stderr` stored in artifact if available
- DB row stores bounded values, for example 64 KiB each
- event stream stores short heads/tails only

### 2. Forward full backend diagnostics from the worker

In `src/worker/agent.py`, when `raw` is an `ExecutionResult`, include:

- `raw_stdout=getattr(raw, "raw_stdout", "")`
- `raw_stderr=getattr(raw, "raw_stderr", "")`
- `parsed_output=getattr(raw, "parsed_output", None)`
- `file_changes=getattr(raw, "file_changes", [])`

Do this for both success and failure. Failure diagnostics are the most important
case.

### 3. Store result JSON for failed rows too

Add a DB method or extend `fail_task()` so failed rows can keep the serialized
result payload:

```text
mesh_tasks.status = failed
mesh_tasks.error = short human string
mesh_tasks.result = full bounded result JSON
```

The task server should call this failure path instead of discarding
`result_dict`.

### 4. Rehydrate remote `TaskResult` from `mesh_tasks.result`

In the gateway remote polling path, when a row is `completed` or `failed`, parse
`row["result"]` if present and rebuild a `TaskResult` with the same fields the
local path uses:

- `output`
- `errors`
- `raw_stdout`
- `raw_stderr`
- `parsed_output`
- `files_modified`
- `file_changes`
- `return_code`
- `execution_time`
- `backend_session_id`

Only fall back to `row["error"]` when `result` is missing, which should become a
legacy/degraded case.

### 5. Make user-facing errors backend-aware

Replace hardcoded `Claude failed`, `Claude authentication error`, and similar
strings with backend-aware labels:

- `Claude failed`
- `Codex failed`
- `OpenCode failed`

For auth failures, use the backend:

- Claude: `Claude authentication error`
- Codex: `Codex/OpenAI authentication error`
- OpenCode: `OpenCode authentication error`

Suggested actions must also be backend-aware. For example, a Codex/OpenAI `401`
must not suggest `claude auth status`.

## Expected Outcome

After the fix, a remote failure should produce a Telegram message closer to:

```text
Task failed: Claude exited with code 1
stderr: <first useful line>
```

or, for structured backend failures:

```text
Task failed: Claude usage limit reached - resets at 19:00
```

or:

```text
Task failed: Codex/OpenAI authentication error - 401 Unauthorized: missing bearer authentication
```

The artifact should contain the complete bounded diagnostic payload so a later
agent can inspect the root cause without access to the remote worker console.

## Tests To Add

Add a worker/server/gateway regression test that simulates a remote backend
failure with:

- `errors=["backend exited with code 1"]`
- non-empty `raw_stdout`
- non-empty `raw_stderr`
- structured `parsed_output`

Assert that:

- the worker POST includes these fields
- `mesh_tasks.result` is non-empty for failed rows
- the gateway artifact includes `raw_stdout`, `raw_stderr`, and `parsed_output`
- Telegram summary uses a specific reason instead of generic `Claude failed`

Also add a backend-aware summary test for a Codex `401 Unauthorized` so it cannot
be mislabeled as Claude.

## Bottom Line

The worker-agent abstraction did not carry over all the diagnostics the old
gateway-local worker path had. The fix is not to make Telegram smarter first; the
fix is to preserve full backend result data across the worker -> task server ->
gateway boundary, then make Telegram summarize that preserved data.
