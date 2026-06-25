# LLM Turn Observability and Usage Accounting Specification

Status: M1/M2 release candidate; implementation handoff below
Date: 2026-06-25
Original design baseline: `AI-team` commit `5f246b7`
Primary implementation backend: Codex CLI
Later adapters: Claude Code, OpenCode CLI, OpenCode server

---

## Implementation handoff — 2026-06-25

The M0–M2 architecture in this specification is implemented on `main` through
commit `86cd4d7`. The implementation was delivered as these reviewable
checkpoints:

- `36d1166` — telemetry contracts, Codex adapter, persistence, APIs, and UI;
- `3f2647e` — lifecycle propagation and transport hardening;
- `4536f82` — stale-turn reconciliation and duplicate-process signaling;
- `6b3455f` — required turn-accounting metrics and continuity rules;
- `90c05c3` — retention, reduced-detail behavior, and cleanup controls;
- `f6dd83f` — dashboard/API integration coverage;
- `393ed48` — real worker-path mesh correlation test;
- `e49e30e` — permanent rejection and cancellation lifecycle hardening;
- `86cd4d7` — explicit LLAMA postprocess coverage gap and monotonic Codex duration.

### Implemented

- typed, default-deny telemetry envelope and immutable invocation context;
- gateway, worker, backend, process, retry, timeout, result, and exit correlation;
- streaming Codex JSONL parsing with aggregate-only token semantics;
- idempotent controller ingestion, bounded upload batches, spool/replay, and expiry;
- deterministic SQLite projection and all required v1 metric keys;
- raw and deduplicated token-work totals;
- backend-session-aware cross-turn context growth;
- authenticated turn list/detail/graph/diagnostic/timeline APIs and dashboard views;
- transactional detailed-event and summary retention;
- `telemetry-reconcile` and `telemetry-cleanup` maintenance commands;
- explicit unknown/unsupported coverage for unavailable facts and LLAMA postprocessing.

Validation currently passes:

```text
76 focused observability, dashboard, ingestion, storage, privacy, and mesh tests
```

Re-run the focused gate with:

```bash
.venv/bin/pytest -q \
  tests/test_telemetry_*.py \
  tests/test_dashboard.py \
  tests/test_backend_call.py \
  tests/test_observability_logging.py \
  tests/test_codex_telemetry_adapter.py \
  tests/test_codex_duplicate_process.py
```

The real worker execution path is covered by
`tests/test_telemetry_mesh_integration.py` without network access or paid
backend calls.

### Remaining release validation

These are the next tasks. They are validation/capability work, not permission to
redesign the schema:

1. Capture additional sanitized fixtures from the deployed Codex version:
   plain answer, MCP tool, retry/failure, and subagent if supported.
2. Add cumulative-counter/reset fixtures if a deployed backend emits cumulative
   usage. Keep aggregate-only semantics if Codex exposes only final totals.
3. Run one controlled real local Codex smoke test and one controlled real mesh
   Codex smoke test; inspect all three dashboard views and scan DB/spool/API
   output for privacy sentinels.
4. Run the §16.5 SQLite ingestion/query/concurrency benchmarks and record actual
   numbers. Fix batching/indexing before considering sampling.
5. After those gates pass, mark M1/M2 shipped and begin M3 Claude support.

Do not start M3/M4 by changing the turn model or dashboard contracts. Add
backend adapters and coverage states under the existing schema.

### Working-tree warning

At this handoff, `src/core/process_utils.py` has a pre-existing unrelated
uncommitted modification. Preserve it unless its owner explicitly asks for it
to be changed or committed.

---

## 0. Outcome

Build a privacy-preserving observability subsystem whose primary diagnostic unit is one
logical user turn.

For every turn, the system must reconstruct:

- which gateway and worker handled it;
- every backend invocation and subprocess;
- every observable model request, tool call, subagent, retry, timeout, result, and exit;
- token usage without double-counting cached input;
- whether high token usage came from real context growth, repeated model calls, retries,
  duplicate subprocesses, or agent/tool loops.

The dashboard must expose:

1. one per-turn execution graph;
2. one per-turn diagnostic table;
3. one chronological event timeline.

The first shippable implementation supports Codex end to end, locally and through the mesh.
The common schema and UI must accept partial data from other backends without inventing
values. Claude and OpenCode adapters are follow-up milestones, not blockers for the first
release.

This document is the implementation contract. If implementation discovers that a backend
does not expose a required fact, store `NULL`/`unknown` and a coverage reason. Do not infer
precise token usage from text length, process duration, cost, or aggregate totals.

---

## 1. Repository facts and constraints

The implementation must extend these existing boundaries:

| Concern | Existing location | Required use |
|---|---|---|
| Structured event envelope | `src/core/observability.py` | Extend; do not create a second unrelated logger |
| Correlation context | `log_context` / `set_log_context` | Add turn and invocation IDs |
| Backend contract | `src/core/interfaces.py` | Add structured telemetry to `ExecutionResult` without changing backend routing |
| Gateway turn execution/retries | `src/orchestrator.py:process_task` | Assign turn/invocation IDs and retry relationships here |
| Local subprocess ownership | `src/backends/*.py` | Emit process and backend-native events here |
| Remote worker execution | `src/worker/agent.py:_execute_task` | Propagate correlation and upload telemetry |
| Storage and migrations | `src/control/db.py` | Add schema through numbered migrations; task-server SQLite remains canonical |
| Read-only UI/API | `src/control/dashboard.py` | Add per-turn APIs and views |
| Existing event tail | `logs/events.ndjson` | Keep for operational logs; it is not the durable accounting source |

Important current limitations:

- `task_id` is already the logical user-turn identifier in normal session flows.
- One-off tasks have no session. Their `session_id` is nullable; do not manufacture a fake
  session row.
- Gateway retries currently repeat backend execution inside one `process_task` call.
- Remote workers return only an `ExecutionResult` subset today.
- Codex NDJSON exposes a `turn.completed.usage` object in existing fixtures.
- Claude and OpenCode output shapes are version-dependent and only partly parsed today.
- Existing result artifacts may contain raw stdout/stderr. This subsystem must not copy
  those fields into telemetry storage or APIs.
- The current `emit_event()` writer is best-effort, process-local, and rotation-based. It
  cannot by itself provide complete cross-machine accounting.

---

## 2. Terminology and identity model

### 2.1 Session

A gateway conversation that may span multiple user turns.

- Field: `session_id`
- Type: string or `NULL`
- Existing source: `Session.session_id`
- `NULL` is valid for one-off tasks.

### 2.2 Turn

One accepted user instruction and all work caused by it until the gateway records a final
result.

- Field: `turn_id`
- Type: non-empty string
- Initial mapping: `turn_id == task.id`
- A retry does not create a new turn.
- A recreated backend session does not create a new turn.
- A subagent does not create a new turn.
- A `/compact` request is its own turn because it is independently submitted and has its own
  task ID.

Do not use backend-native "turn" terminology as the gateway turn ID. A backend can make
multiple model requests during one gateway turn.

### 2.3 Invocation

One deliberate call from the gateway/worker into a `CodingBackend` method.

- Field: `invocation_id`
- Type: UUIDv7/ULID-style sortable unique string; UUID4 is acceptable if no UUIDv7 helper is
  added.
- Created immediately before each call to `create_session`, `resume_session`, or
  `run_oneoff`.
- One retry creates a new invocation.
- Session recreation creates a new invocation.
- A backend's transparent internal fallback creates a child invocation only if it spawns a
  separate agent process or sends a separate model-bearing HTTP request. Otherwise record a
  lifecycle event under the current invocation.

Required fields:

- `attempt`: 1-based gateway attempt number;
- `spawn_reason`: `initial`, `retry`, `session_recreate`, `backend_fallback`,
  `manual_replay`, or `unknown`;
- `parent_invocation_id`: nullable;
- `retry_of_invocation_id`: nullable;
- `duplicate_of_invocation_id`: nullable.

### 2.4 Process

An operating-system process owned or observed by an invocation.

- Field: `process_id` is the OS PID and is not globally unique.
- Stable key: `(node_id, process_start_time, process_id)`.
- Also store `process_instance_id`, generated at spawn, because PID reuse is normal.
- The direct backend CLI process has `process_role=agent`.
- Persistent OpenCode server processes have `process_role=backend_server` and may serve many
  invocations.
- Child processes are recorded only when observable through a backend event/hook or explicit
  gateway spawn. Do not poll the entire process tree in v1.

### 2.5 Model request

One provider inference request, not one CLI invocation.

- Field: `model_request_id`
- Prefer backend-provided request IDs.
- Otherwise generate `<invocation_id>:mr:<1-based sequence>`.
- Multiple model requests can occur inside one invocation because of tool loops.
- Aggregate `turn.completed.usage` without request-level events has
  `usage_granularity=invocation_total`; it counts as one observed aggregate, but request
  count and peak context are `unknown` unless the backend proves there was one request.

### 2.6 Tool call

One agent-requested tool execution. It may be shell, file edit, MCP, browser, or another
backend-specific tool.

- Field: `tool_call_id`
- Store tool name/category and status.
- Never store tool arguments, command text, paths, tool output, source code, or result text.

### 2.7 Subagent

A child agent run explicitly identified by a backend event/hook or gateway dispatch.

- Field: `subagent_id`
- Link to `parent_invocation_id` and, when available, `parent_model_request_id`.
- Do not classify ordinary tool calls as subagents.
- If the backend exposes no subagent events, report `subagent_count=NULL` with coverage
  `unsupported`; do not report zero.

---

## 3. Correlation propagation

### 3.1 Correlation fields

Every telemetry event must carry:

- `schema_version`
- `event_id`
- `event_name`
- `event_time`
- `observed_time`
- `node_id`
- `emitter_process_instance_id` of the emitting gateway/worker process
- `session_id` nullable
- `turn_id`
- `invocation_id` nullable for turn-level queue events
- `source`
- `source_sequence` nullable

When applicable:

- `model_request_id`
- `tool_call_id`
- `subagent_id`
- `pid`
- `backend`
- `model`

### 3.2 Propagation path

The gateway must put these fields in the mesh task payload:

```json
{
  "telemetry": {
    "schema_version": 1,
    "turn_id": "task_...",
    "session_id": "sess_...",
    "gateway_node_id": "main-pc"
  }
}
```

The execution owner creates each invocation ID immediately before calling a backend:

- local execution: the gateway;
- mesh execution: the worker after it claims the task.

The gateway remains authoritative for gateway-side retries and links the resulting remote
attempts through dispatch metadata. The process that actually owns `Popen` is authoritative
for PID/process events.

The task server is the authoritative telemetry ingestion and persistence owner. Gateway and
worker processes send normalized events to it. They must not assume that their local
`state/mesh.db` is the controller database.

Subprocess environments receive only non-sensitive correlation values:

```text
AI_TEAM_SESSION_ID
AI_TEAM_TURN_ID
AI_TEAM_INVOCATION_ID
AI_TEAM_NODE_ID
```

Keep the existing `SESSION_ID` variable for compatibility, but do not overload it with
`turn_id`.

### 3.3 Context variables

Extend `src/core/observability.py` so log context accepts:

- `turn_id`
- `invocation_id`
- `backend`

Keep `task_id` as a compatibility alias in general operational events. New accounting code
uses `turn_id`. During migration, emit both with the same value.

### 3.4 Clock handling

- Persist UTC RFC 3339 timestamps with microseconds and `Z`.
- Also capture process-local monotonic nanoseconds for duration calculation.
- Never compare monotonic clocks across processes or nodes.
- Cross-node timelines sort by `event_time`, then `source`, then `source_sequence`.
- Store `clock_quality`: `local`, `ntp_synced`, or `unknown`. V1 may use `unknown`.
- Negative derived durations caused by clock skew must become `NULL` and set
  `data_quality_flags=["clock_skew"]`.

---

## 4. Event contract

### 4.1 Envelope

Add a typed `TelemetryEvent` model in `src/core/telemetry.py`. Pydantic is already a project
dependency and should be used for validation.

```python
class TelemetryEvent(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    event_name: str
    event_time: datetime
    observed_time: datetime
    node_id: str
    emitter_process_instance_id: str
    source: Literal["gateway", "worker", "backend", "hook", "reconciler"]
    source_sequence: int | None = None

    session_id: str | None = None
    turn_id: str
    invocation_id: str | None = None
    model_request_id: str | None = None
    tool_call_id: str | None = None
    subagent_id: str | None = None

    backend: str | None = None
    model: str | None = None
    pid: int | None = None
    attributes: dict[str, JsonScalar | list[JsonScalar]]
```

`attributes` is allowlisted per event name. Unknown or nested object payloads are rejected
before persistence.

### 4.2 Required event names

Turn lifecycle:

- `turn.accepted`
- `turn.queued`
- `turn.started`
- `turn.timeout_requested`
- `turn.cancel_requested`
- `turn.result_recorded`
- `turn.completed`

Invocation lifecycle:

- `invocation.created`
- `invocation.started`
- `invocation.retry_scheduled`
- `invocation.duplicate_detected`
- `invocation.completed`

Process lifecycle:

- `process.spawned`
- `process.timeout_detected`
- `process.termination_requested`
- `process.exited`
- `process.exit_unknown`

Model lifecycle:

- `model.request.started`
- `model.request.usage`
- `model.request.completed`
- `model.request.failed`

Tool lifecycle:

- `tool.call.started`
- `tool.call.completed`
- `tool.call.failed`

Subagent lifecycle:

- `subagent.started`
- `subagent.completed`
- `subagent.failed`

Backend parsing/coverage:

- `telemetry.coverage`
- `telemetry.parse_error`
- `telemetry.batch_dropped`
- `telemetry.reconciled`

### 4.3 Allowed attributes

Examples of allowed values:

- lifecycle status and reason codes;
- counts;
- token counts;
- durations;
- exit codes and signal numbers;
- timeout kind and configured limit;
- retry class/reason and delay;
- backend event type;
- tool category/name after sanitization;
- model identifier;
- opaque provider request ID if it contains no user data;
- parser/backend version;
- data-quality and coverage flags.

Forbidden values:

- prompt or system prompt;
- source code;
- file contents or diffs;
- filesystem paths other than a coarse repo identifier already approved for the dashboard;
- shell commands;
- tool arguments;
- tool results;
- raw stdout/stderr;
- raw model responses;
- assistant text;
- exception tracebacks containing payload data;
- environment variable values;
- authorization data or API keys.

Error storage uses stable codes such as `rate_limit`, `network`, `context_overflow`,
`permission_block`, `timeout`, `parse_error`, and `fatal`. A bounded sanitized message may
be retained only if it passes the existing redaction filter and contains no backend
payload. Prefer code plus exception class.

### 4.4 Event ordering and idempotency

- `event_id` is globally unique.
- The controller inserts events with `INSERT OR IGNORE`.
- Each emitter maintains a monotonically increasing `source_sequence` for an invocation.
- Re-uploading a telemetry batch is safe.
- Events may arrive after `turn.completed`; the turn projection must be recomputed or marked
  dirty and recomputed on read.
- No consumer assumes delivery order equals event-time order.

---

## 5. Token accounting semantics

### 5.1 Canonical fields per model request

Store nullable non-negative integers:

- `input_tokens`
- `output_tokens`
- `cache_read_tokens`
- `cache_creation_tokens`
- `reasoning_tokens`
- `context_tokens`

Also store:

- `input_token_semantics`: `includes_cache`, `excludes_cache`, or `unknown`;
- `usage_granularity`: `request`, `invocation_total`, or `turn_total`;
- `usage_source`: backend event/path name;
- `usage_is_estimated`: boolean, always `false` in v1;
- `usage_coverage`: `complete`, `partial`, `aggregate_only`, `unsupported`, or
  `parse_error`.

### 5.2 Context-token normalization

Token APIs differ in whether cached tokens are a subset of `input_tokens` or additional to
it. Normalize only with a verified adapter rule:

```text
if input_token_semantics == includes_cache:
    context_tokens = input_tokens
elif input_token_semantics == excludes_cache:
    context_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
else:
    context_tokens = NULL
```

Never add cache fields to input tokens unless the backend adapter has a fixture proving they
are exclusive. This prevents the most likely accounting bug: double-counting cached input.

### 5.3 Missing values

- Unknown is `NULL`, not zero.
- Zero is valid only when a backend explicitly reports zero or the event type proves no such
  work occurred.
- Aggregates must include a coverage object so the UI can distinguish `0` from unavailable.

### 5.4 Per-turn calculations

All sums operate only on non-duplicate model usage records. A usage record is duplicate if
it has the same backend-provided request ID, or the adapter explicitly marks it as a repeated
summary of already-recorded requests.

Required metrics:

`peak_context_tokens`

```text
max(context_tokens for request-granularity model requests)
```

If only invocation-total usage exists, this is `NULL`; an aggregate input-token total is not
a context-window size.

`total_token_work`

```text
sum(context_tokens + output_tokens + coalesce(reasoning_tokens, 0))
```

over request-granularity records. If only aggregate usage is available, calculate the same
from the aggregate and mark `metric_quality=aggregate_only`.

`work_amplification`

```text
total_token_work / max(single_model_request_token_work)
```

where single request work is `context_tokens + output_tokens + reasoning_tokens`.
This is `1.0` for one model request and grows with repeated model calls. It is `NULL` when
request-level usage is unavailable.

`turn_entry_context_tokens`

The `context_tokens` of the earliest request-level model request in the first non-duplicate
primary invocation. Retries do not replace this value.

`turn_exit_context_tokens`

The `context_tokens` of the last request-level model request in the invocation that produced
the final turn result.

`intra_turn_context_growth`

```text
turn_exit_context_tokens - turn_entry_context_tokens
```

`context_growth_between_turns`

```text
current.turn_entry_context_tokens - previous.turn_exit_context_tokens
```

The previous turn must have the same non-null `session_id` and backend-native session ID.
If either session was recreated/compacted, return `NULL` and set
`context_discontinuity_reason`.

`model_request_count`

Count request-granularity model requests. If only aggregate usage exists, `NULL`, not `1`.

`invocations_per_turn`

Count non-duplicate `invocation.created` records.

`tool_call_count`

Count distinct tool call IDs. If tool telemetry is unsupported, `NULL`.

`subagent_count`

Count distinct subagent IDs. If subagent telemetry is unsupported, `NULL`.

`retry_count`

Count invocations with `spawn_reason` equal to `retry` or `session_recreate`. Present both
the total and counts grouped by reason.

`cache_read_ratio`

```text
sum(cache_read_tokens) /
sum(context_tokens)
```

Only for request records whose semantics make `context_tokens` known. Clamp display to
`0..1`; values outside that range indicate an adapter bug and add a data-quality flag.

Additional required metrics:

- `wall_time_ms`: turn start to terminal result;
- `active_invocation_time_ms`: sum of invocation durations;
- `parallelism_factor`: active invocation time / wall time;
- `failed_invocation_count`;
- `duplicate_invocation_count`;
- `timeout_count` grouped by gateway, inactivity, HTTP, and backend;
- `tool_loop_rounds`: model-request transitions followed by one or more tool calls;
- `tokens_per_tool_call`;
- `output_to_input_ratio`;
- `cache_creation_ratio`;
- `unattributed_token_count`: aggregate tokens that cannot be assigned to a request;
- `telemetry_event_count`;
- `coverage_score`: percentage of supported required facts observed, never a claim that an
  unsupported backend emitted them.

### 5.5 Attribution categories

Each model request and invocation receives one work category:

- `primary`
- `tool_loop`
- `subagent`
- `retry`
- `session_recreate`
- `duplicate`
- `unknown`

The diagnostic table must show token work grouped by category. This is how the system
distinguishes context growth from repeated work.

---

## 6. Duplicate and loop detection

### 6.1 Intentional retries

The orchestrator is authoritative. It links retries using `retry_of_invocation_id` and emits
the stable retry reason from `_classify_error()`.

Do not detect an orchestrator retry heuristically.

### 6.2 Duplicate subprocesses

Mark an invocation as a probable duplicate when all are true:

- same `turn_id`;
- same backend;
- same invocation action;
- overlapping runtime;
- neither invocation declares the other as retry/fallback/subagent;
- both represent agent processes, not a persistent backend server.

Store `duplicate_confidence=probable`; do not delete its usage. Exclude probable duplicates
from default accounting only when an operator or deterministic gateway relationship confirms
the duplicate. Until then expose:

- raw totals;
- deduplicated totals;
- the rule used.

The direct replacement behavior in backend `_register_process()` must emit
`invocation.duplicate_detected` before terminating the stale process.

### 6.3 Tool loops

A tool loop is one or more tool calls between two model requests in the same invocation.
Compute loop rounds from event relationships, not from process count.

### 6.4 Repeated aggregate summaries

Some backends emit cumulative usage repeatedly. Each adapter must declare whether a usage
event is:

- delta;
- request total;
- invocation cumulative total;
- final invocation total.

For cumulative values, persist the raw value and derive non-negative deltas. If counters
decrease, start a new series and add `counter_reset`.

---

## 7. Storage model

SQLite in the task server's configured `state/mesh.db` is the durable accounting source.
`events.ndjson` remains a human-operational stream and fallback spool.

Add migrations after schema version 12.

### 7.1 `llm_turns`

One projection row per turn:

```sql
CREATE TABLE llm_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT,
    task_id TEXT NOT NULL,
    gateway_node_id TEXT,
    execution_node_id TEXT,
    backend TEXT,
    backend_session_id_start TEXT,
    backend_session_id_end TEXT,
    requested_model TEXT,
    observed_models TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    ended_at TEXT,
    final_status TEXT NOT NULL DEFAULT 'running',
    timeout_status TEXT NOT NULL DEFAULT 'none',
    final_exit_code INTEGER,
    final_invocation_id TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    projection_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`final_status` enum:

- `queued`
- `running`
- `success`
- `failed`
- `cancelled`
- `timed_out`
- `detached`
- `unknown`

`timeout_status` enum:

- `none`
- `gateway_timeout`
- `backend_inactivity_timeout`
- `backend_http_timeout`
- `backend_reported_timeout`
- `multiple`

### 7.2 `llm_invocations`

One row per invocation:

```sql
CREATE TABLE llm_invocations (
    invocation_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    parent_invocation_id TEXT,
    retry_of_invocation_id TEXT,
    duplicate_of_invocation_id TEXT,
    attempt INTEGER NOT NULL,
    spawn_reason TEXT NOT NULL,
    action TEXT NOT NULL,
    node_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    requested_model TEXT,
    observed_model TEXT,
    process_instance_id TEXT,
    pid INTEGER,
    process_started_at TEXT,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL,
    timeout_kind TEXT,
    exit_code INTEGER,
    signal INTEGER,
    retry_reason TEXT,
    model_request_count INTEGER,
    tool_call_count INTEGER,
    subagent_count INTEGER,
    usage_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(turn_id) REFERENCES llm_turns(turn_id) ON DELETE CASCADE
);
```

The PID columns above are a denormalized pointer to the primary agent process for fast table
rendering. They are not the complete process model.

### 7.3 `llm_processes` and `llm_invocation_processes`

Processes require a separate relation because one invocation may own multiple subprocesses
and one persistent OpenCode server may serve multiple invocations.

```sql
CREATE TABLE llm_processes (
    process_instance_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    pid INTEGER,
    parent_process_instance_id TEXT,
    process_role TEXT NOT NULL,
    backend TEXT,
    executable_name TEXT,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    signal INTEGER,
    status TEXT NOT NULL,
    data_quality_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE llm_invocation_processes (
    invocation_id TEXT NOT NULL,
    process_instance_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    PRIMARY KEY(invocation_id, process_instance_id),
    FOREIGN KEY(invocation_id) REFERENCES llm_invocations(invocation_id) ON DELETE CASCADE,
    FOREIGN KEY(process_instance_id) REFERENCES llm_processes(process_instance_id)
);
```

`executable_name` is a basename/category such as `codex`, `claude`, or `opencode`; never
persist the full command line.

### 7.4 `llm_model_requests`

```sql
CREATE TABLE llm_model_requests (
    model_request_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    provider_request_id TEXT,
    model TEXT,
    work_category TEXT NOT NULL DEFAULT 'unknown',
    started_at TEXT,
    ended_at TEXT,
    status TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    reasoning_tokens INTEGER,
    context_tokens INTEGER,
    input_token_semantics TEXT NOT NULL DEFAULT 'unknown',
    usage_granularity TEXT NOT NULL,
    usage_source TEXT,
    usage_coverage TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(invocation_id) REFERENCES llm_invocations(invocation_id) ON DELETE CASCADE
);
```

Unique index where provider request ID is present:

```sql
CREATE UNIQUE INDEX idx_llm_model_provider_request
ON llm_model_requests(invocation_id, provider_request_id)
WHERE provider_request_id IS NOT NULL;
```

Aggregate-only usage is stored as a synthetic row with ID
`<invocation_id>:usage:aggregate`, `usage_granularity=invocation_total`, and no provider
request ID. It is excluded from `model_request_count`.

### 7.5 `llm_events`

Append-only normalized events:

```sql
CREATE TABLE llm_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    event_time TEXT NOT NULL,
    observed_time TEXT NOT NULL,
    node_id TEXT NOT NULL,
    emitter_process_instance_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_sequence INTEGER,
    session_id TEXT,
    turn_id TEXT NOT NULL,
    invocation_id TEXT,
    model_request_id TEXT,
    tool_call_id TEXT,
    subagent_id TEXT,
    backend TEXT,
    model TEXT,
    pid INTEGER,
    attributes TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL
);
```

Indexes:

- `(turn_id, event_time, source_sequence)`
- `(invocation_id, event_time)`
- `(session_id, event_time)`
- `(event_name, event_time)`

### 7.6 Retention

Defaults:

- turn/invocation/model-request summaries: 180 days;
- detailed events: 30 days;
- local failed-upload spool: 7 days or 256 MiB, whichever comes first.

Retention is configurable. Deletion must be transactional per turn so no orphan rows remain.
V1 may ship with a manual cleanup command; automatic cleanup is a later operational task.

---

## 8. Collection architecture

### 8.1 Components

Add:

- `src/core/telemetry.py`: models, redaction/allowlist, ID generation, in-process recorder;
- `src/core/telemetry_projection.py`: event-to-summary projection and metric formulas;
- `src/core/telemetry_adapters/base.py`: backend adapter protocol;
- `src/core/telemetry_adapters/codex.py`: Codex NDJSON adapter;
- `src/core/telemetry_uploader.py`: shared idempotent batching and spool used by gateway/workers;
- `src/control/telemetry_store.py`: DB insert/query operations;
- `src/control/telemetry_sink.py`: common sink interface plus HTTP and in-process implementations;
- tests and fixtures under `tests/fixtures/telemetry/`.

Do not put all parsing and SQL into `src/core/observability.py`; keep its current logging
responsibility small.

### 8.2 Recorder behavior

`TelemetryRecorder` is scoped to one turn/invocation and:

- emits normalized events;
- maintains sequence numbers;
- sends events through a `TelemetrySink`;
- uses the HTTP sink from gateway and worker processes;
- may use the in-process DB sink only when running inside the embedded task server;
- never raises into task execution;
- increments an in-memory dropped-event counter on validation/storage failure;
- emits one `telemetry.batch_dropped` summary when possible.

Accounting loss must not fail the user task, but it must be visible.

The sink boundary is mandatory even in a single-machine deployment. Do not let gateway
business logic import `TelemetryStore` directly; doing so silently breaks split deployments.

### 8.3 Remote upload

Add an authenticated controller endpoint in `src/control/task_server.py`:

```text
POST /telemetry/batches
```

Body:

```json
{
  "batch_id": "uuid",
  "node_id": "worker-1",
  "events": [ ... up to configured bounds ... ]
}
```

Response:

```json
{
  "accepted": 50,
  "duplicates": 2,
  "rejected": 0
}
```

Rules:

- reuse worker bearer authentication;
- maximum 200 events or 512 KiB per request;
- reject the whole batch on authentication failure;
- validate each event independently after authentication;
- return per-event rejection codes only, never echo payloads;
- retry network/5xx failures with bounded exponential backoff;
- do not retry 4xx schema failures;
- spool unsent batches under `logs/telemetry_spool/`;
- write spool files atomically using temp file plus rename;
- spool file names use batch ID, never session/prompt text;
- delete only after controller acknowledgement;
- replay on worker startup;
- preserve event IDs across retries.

Flush conditions:

- 50 events;
- 1 second elapsed;
- invocation completed;
- worker shutdown.

The gateway uses the same endpoint for turn/queue/retry/result events. It may batch less
aggressively because event volume is low. If the task server is unavailable, the gateway
uses the same atomic spool/replay behavior as a worker.

### 8.4 OpenTelemetry use

Use OpenTelemetry conventions where useful, but do not make an OTLP collector mandatory.

Recommended:

- add optional dependency group `telemetry` with `opentelemetry-api` and
  `opentelemetry-sdk`;
- map turn/invocation/model/tool lifecycles to spans;
- use the same trace ID derived from `turn_id`;
- use `invocation_id` as the invocation span correlation attribute;
- default exporter is none;
- SQLite normalized events remain the accounting source of truth.

Do not use OTel auto-instrumentation on subprocess stdout or HTTP request bodies; it risks
capturing prompts/tool payloads. Any exporter must receive the same allowlisted attributes
as SQLite.

The first milestone may omit OTel package installation if it would delay end-to-end
accounting. Schema and names must remain OTel-compatible.

---

## 9. Integration points

### 9.1 `src/core/interfaces.py`

Add:

```python
@dataclass
class ExecutionTelemetry:
    invocation_id: str
    events: list[dict] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)

@dataclass
class ExecutionResult:
    ...
    telemetry: ExecutionTelemetry | None = None
```

For remote execution, events normally upload separately. `ExecutionTelemetry` carries the
invocation ID, final coverage, and an emergency bounded fallback summary, not raw backend
output.

### 9.2 `src/orchestrator.py`

At task acceptance:

- send `turn.accepted` through the telemetry sink; the task server creates/upserts
  `llm_turns`;
- emit `turn.accepted`, `turn.queued`, and `turn.started`;
- use `turn_id=task.id`.

In the process that owns every backend call:

- create invocation ID;
- set attempt and spawn reason;
- emit `invocation.created`;
- pass a `TelemetryContext` to the backend.

The cleanest contract change is to add optional keyword-only telemetry context:

```python
backend.create_session(session, *, telemetry_context=None)
backend.resume_session(session, message, *, telemetry_context=None)
backend.run_oneoff(cwd, message, *, telemetry_context=None)
```

Because `CodingBackend` is internal, update all adapters in one commit. Do not store mutable
context on singleton backend instances; concurrent tasks would cross-contaminate IDs.

On retry:

- emit `invocation.retry_scheduled` with stable reason;
- link the next invocation to the failed invocation.

On gateway timeout:

- emit `turn.timeout_requested`;
- ask backend to cancel;
- do not assume the process exited;
- wait for/record actual process exit separately;
- terminal turn status may be `timed_out` while process exit remains unknown.

On result:

- emit `turn.result_recorded`;
- compute projection;
- emit `turn.completed` exactly once per terminal transition.

### 9.3 Backend adapters

All adapters must:

- emit process spawn immediately after `Popen` succeeds;
- record PID and generated process instance ID;
- emit timeout detection separately from termination request;
- emit process exit after `wait()` returns;
- emit `process.exit_unknown` if the process cannot be reaped;
- record real return code, including negative signal codes;
- never default a missing return code to success.

Current timeout branches often return `ExecutionResult` without `return_code`. Fix this while
instrumenting.

### 9.4 Codex adapter, milestone 1

Parse line by line while stdout is read, not only after process exit. This enables a live
timeline and bounds memory.

Map known events:

- `thread.started` -> backend session observation;
- `turn.started` -> `model.request.started` only if fixture/version evidence proves it is one
  provider request; otherwise emit a backend lifecycle event and leave request count unknown;
- `item.started` / `item.completed` -> tool/subagent/model message lifecycle based on an
  explicit item-type mapping;
- `turn.completed.usage` -> usage event.

Before implementing item mappings, capture sanitized fixtures from the installed Codex
version for:

- plain text answer;
- one shell tool;
- one MCP tool;
- one file edit;
- one retry/failure;
- one subagent if supported;
- cached input if exposed.

Fixtures must replace prompt/text/arguments/results with placeholders before commit.
Tests assert that sanitizer removes those values.

If Codex only provides one aggregate usage object per CLI turn, milestone 1 reports:

- invocation aggregate token totals;
- model request count `NULL`;
- peak context `NULL`;
- tool counts if item events expose them;
- explicit `aggregate_only` coverage.

Do not label the aggregate as a single model request merely to fill the table.

### 9.5 Claude adapter, milestone 2

Use stream-json result/assistant/tool events and, where available, Claude hooks for:

- subagent start/stop;
- tool lifecycle;
- model usage.

Hook configuration is optional deployment integration and must not be required for the
backend to run. Coverage states distinguish `hooks_enabled`, `stream_only`, and
`unsupported`.

Claude's final result usage may be aggregate. Apply the same aggregate-only rule.

### 9.6 OpenCode adapters, milestone 3

For server mode, parse `response.info.tokens` through a versioned fixture. Confirm whether
input includes cache fields before setting token semantics.

The persistent `opencode serve` PID is not one invocation per turn. Model it as:

- one long-lived `backend_server` process;
- per-message invocations without a new PID;
- HTTP timeout events linked to the invocation;
- server process exit linked to every active invocation affected by it.

For CLI mode, follow ordinary subprocess mapping.

### 9.7 LLAMA summarization

One-off tasks may invoke the local LLAMA summarizer after the primary backend. That is model
work caused by the same turn and must eventually be recorded as a child invocation with
`work_category=postprocess`.

Codex milestone may initially record a coverage gap for LLAMA, but the projection and graph
must not silently imply that backend execution was the only model work.

---

## 10. Projection and reconciliation

### 10.1 Projection

`TelemetryProjector.rebuild_turn(turn_id)` reads normalized events and upserts:

- invocation rows;
- model request rows;
- turn summary and metrics.

It must be deterministic and idempotent. Given the same event set, it produces byte-equivalent
JSON metrics after canonical key ordering.

Projection runs:

- after each accepted batch;
- after terminal result;
- on explicit repair command.

### 10.2 Reconciliation after crashes

Add command:

```text
python main.py telemetry-reconcile [--turn-id ID] [--since HOURS]
```

It must:

- find running turns with no recent event;
- inspect existing `mesh_tasks` terminal state;
- synthesize only reconciliation events with `source=reconciler`;
- never invent token usage/tool calls;
- set process exit unknown when no reliable exit is available;
- close detached/failed turns according to existing mesh state;
- rebuild projections.

### 10.3 Invariants

Projector validates:

- one terminal turn status;
- invocation end is not before start;
- token counts are non-negative;
- retry links stay within one turn;
- process PID without node/process-instance ID is invalid;
- model request belongs to the same turn as its invocation;
- cache ratio cannot exceed 1 when semantics are known;
- final invocation exists when final status is terminal, unless coverage explains why.

Violations add data-quality flags and do not crash the dashboard.

---

## 11. Read APIs

Add authenticated endpoints to `src/control/dashboard.py`:

```text
GET /api/turns?session_id=&status=&backend=&limit=&before=
GET /api/turns/{turn_id}
GET /api/turns/{turn_id}/graph
GET /api/turns/{turn_id}/diagnostics
GET /api/turns/{turn_id}/events?after=&limit=
```

### 11.1 Turn detail

Returns identifiers, final state, timing, aggregate metrics, coverage, and data-quality
flags. It does not return prompts, raw output, tool arguments/results, or source paths.

### 11.2 Graph response

```json
{
  "turn_id": "task_123",
  "nodes": [
    {
      "id": "inv_1",
      "kind": "invocation",
      "label": "codex attempt 1",
      "status": "failed",
      "started_at": "...",
      "ended_at": "...",
      "metrics": {"token_work": 12000}
    }
  ],
  "edges": [
    {"from": "turn:task_123", "to": "inv_1", "kind": "contains"},
    {"from": "inv_1", "to": "inv_2", "kind": "retry"}
  ],
  "coverage": {...}
}
```

Node kinds:

- turn;
- invocation;
- process;
- model request or aggregate usage;
- tool group;
- individual tool call;
- subagent;
- result.

To prevent unusable graphs, collapse more than 20 consecutive same-category tool calls into
a group node by default. The API supports `?expand_tools=true` up to 500 nodes. Above that,
return a truncation marker and counts; the timeline remains pageable.

### 11.3 Diagnostic table

Rows:

- one turn-total row;
- one row per invocation;
- optional expandable model-request rows.

Columns:

- attempt;
- reason/category;
- backend/model;
- node/PID;
- start/end/duration;
- input/output/cache-read/cache-create/reasoning/context tokens;
- model requests;
- tool calls;
- subagents;
- retries;
- timeout;
- exit code;
- status;
- coverage/data-quality.

Unknown values display as `—`, not `0`.

### 11.4 Timeline

Page chronologically with:

- timestamp;
- elapsed time from turn start;
- event name;
- invocation/process/model/tool/subagent IDs;
- sanitized reason/status;
- source node;
- coverage markers.

No raw attribute JSON is rendered without an explicit allowlist.

---

## 12. Dashboard behavior

Extend the current no-build HTML dashboard. Do not introduce React or a frontend toolchain
for this feature.

Add:

- turns list below current tasks;
- click-through turn detail;
- SVG graph built from API nodes/edges;
- HTML diagnostic table;
- virtualized or paginated timeline.

Graph layout can use a small vendored permissive library only if license and bundle size are
acceptable. Otherwise implement a deterministic left-to-right layered SVG:

```text
Turn -> Invocation(s) -> Process/model/tool/subagent groups -> Final result
```

Color rules:

- green: success;
- red: failed;
- amber: retry/timeout/partial coverage;
- gray: unknown/unsupported;
- purple: subagent;
- blue: model request;
- cyan: tool group.

The UI must always show a coverage banner, for example:

```text
Codex usage: aggregate only · tool calls: complete · subagents: unsupported
```

This prevents incomplete telemetry from looking authoritative.

---

## 13. Privacy and security

### 13.1 Default-deny serializer

Telemetry attributes are serialized through an event-specific allowlist. There is no generic
`**payload` persistence path.

Tests must feed secrets, prompts, source code, command lines, tool arguments, and model text
through every adapter and assert none appears in:

- `llm_events`;
- projection JSON;
- telemetry upload requests;
- telemetry spool;
- dashboard API responses.

### 13.2 Identifiers

Backend-native request/session IDs may be stored because they are correlation identifiers,
not content. If a backend embeds user text in an ID field, hash it with a deployment-local
salt and mark `identifier_hashed=true`.

### 13.3 Tool names

Store:

- canonical category: `shell`, `file_read`, `file_write`, `search`, `mcp`, `browser`,
  `subagent`, `other`;
- sanitized tool name limited to `[A-Za-z0-9_.:-]`, 80 characters.

For MCP names, the server/tool identifier is acceptable; arguments and results are not.

### 13.4 API access

Reuse dashboard bearer auth initially. Telemetry upload uses worker auth. Do not expose these
endpoints unauthenticated.

### 13.5 Existing raw artifacts

This feature does not remove current `raw_stdout`/`raw_stderr` artifact behavior. It must
document that those artifacts have a different privacy posture. A later task may disable
raw backend artifact retention by default, but that is outside this implementation unless
the user explicitly expands scope.

---

## 14. Configuration

Add environment/config fields:

```text
TELEMETRY_ENABLED=true
TELEMETRY_DETAILED_EVENTS=true
TELEMETRY_UPLOAD_BATCH_SIZE=50
TELEMETRY_UPLOAD_INTERVAL_MS=1000
TELEMETRY_UPLOAD_MAX_BYTES=524288
TELEMETRY_SPOOL_MAX_BYTES=268435456
TELEMETRY_EVENT_RETENTION_DAYS=30
TELEMETRY_SUMMARY_RETENTION_DAYS=180
TELEMETRY_OTLP_ENDPOINT=
```

Behavior:

- disabled: no telemetry DB/event writes, existing gateway behavior unchanged;
- detailed events disabled: retain turn/invocation/model summaries and coverage only;
- telemetry failures never fail a user turn;
- log one rate-limited warning per failure class, not one per event.

---

## 15. Implementation milestones

### M0 — Fixtures and contract tests

Deliver:

- telemetry models;
- privacy allowlist tests;
- sanitized Codex fixtures from the installed version;
- metric formula tests;
- schema migration tests.

Exit criteria:

- no backend runtime changes;
- fixtures prove what Codex actually exposes;
- unsupported values are represented as `NULL`.

### M1 — Local Codex end to end

Deliver:

- turn/invocation/process lifecycle;
- streaming Codex event parser;
- usage and tool accounting supported by fixtures;
- SQLite storage/projection;
- per-turn APIs;
- graph/table/timeline UI;
- gateway retry and timeout correlation.

Exit criteria:

- a local Codex turn appears in all three views;
- two retries remain one turn and three invocations;
- timeout and actual process exit are separate facts;
- no prompt/model/tool payload is stored.

### M2 — Mesh Codex

Deliver:

- propagation in task payload;
- worker batching/spool/replay;
- controller ingestion endpoint;
- idempotent upload tests;
- cross-node timeline and clock-quality markers.

Exit criteria:

- remote Codex turn has gateway, worker, process, result, and usage events;
- controller restart and duplicate batch upload do not duplicate counts;
- worker offline spool replays after reconnect.

### M3 — Claude

Deliver stream-json adapter and optional hook integration. Update capability matrix and
coverage UI.

### M4 — OpenCode CLI/server and LLAMA postprocessing

Deliver:

- persistent server process modeling;
- per-message usage parsing;
- LLAMA child invocation accounting;
- backend comparison tests.

Do not begin M3/M4 before M1 and M2 acceptance tests pass.

### 15.1 Suggested commit sequence

Keep commits independently testable:

1. telemetry models, allowlists, IDs, and unit tests;
2. DB migrations, store, projector, and migration/projection tests;
3. immutable backend telemetry context and no-op integration in all adapters;
4. local turn/invocation/process lifecycle;
5. Codex sanitized fixtures and streaming adapter;
6. retry/timeout/duplicate correlation;
7. read APIs;
8. graph/table/timeline dashboard;
9. mesh upload endpoint, uploader, and spool;
10. mesh integration and recovery tests;
11. documentation/config/runbook update.

Do not combine schema migration, backend parser rewrite, and dashboard work into one commit.
That makes failures impossible to isolate.

### 15.2 Realistic effort

Estimate for one engineer already familiar with this repository:

| Milestone | Expected effort | Main uncertainty |
|---|---:|---|
| M0 | 1–2 engineering days | obtaining representative sanitized Codex fixtures |
| M1 | 4–6 engineering days | streaming parser and projection/UI correctness |
| M2 | 3–5 engineering days | crash-safe spool and cross-process integration tests |
| M3 | 2–4 engineering days | Claude version/hook coverage |
| M4 | 3–5 engineering days | persistent OpenCode server modeling and LLAMA path |

M0–M2, the required definition of done, is approximately 8–13 focused engineering days.
This assumes no frontend rewrite and no mandatory external telemetry service. Attempting all
backends in the first release materially increases schema churn and test time without
improving the core turn model.

### 15.3 Assumptions that must be verified in M0

- the deployed Codex version emits stable NDJSON event types under `codex exec --json`;
- usage fields can be extracted without retaining raw events;
- tool item IDs are stable enough to pair start/completion;
- the task server process can write the same configured `mesh.db`;
- worker authentication is acceptable for telemetry upload;
- SQLite write volume remains within the performance targets;
- UTC clocks on mesh nodes are reasonably synchronized.

If any assumption fails, update the capability/coverage result, not the core identity model.
The only expected design fallback is aggregate-only usage for Codex.

---

## 16. Test plan

### 16.1 Unit tests

- ID generation and context propagation;
- event schema and attribute allowlists;
- token normalization for inclusive/exclusive/unknown cache semantics;
- cumulative-counter delta handling;
- metric calculations;
- duplicate/retry/loop classification;
- projector idempotency;
- privacy redaction;
- each adapter fixture.

### 16.2 Database tests

- upgrade existing version-12 database in place;
- fresh database converges to the same schema;
- duplicate events are ignored;
- foreign relationships remain valid;
- projection rebuild is deterministic;
- deleting a turn cascades correctly;
- WAL concurrent inserts do not produce `database is locked` under expected load.

### 16.3 Integration tests

- local success with one model aggregate;
- local tool loop;
- retry then success;
- session recreation then success;
- gateway timeout followed by process exit;
- inactivity timeout;
- cancellation;
- backend exits non-zero without final result;
- backend process disappears before `wait`;
- two same-session tasks accidentally overlap and duplicate detection fires;
- worker uploads duplicate batch;
- worker loses network, spools, restarts, and replays;
- controller receives terminal result before late telemetry;
- controller receives telemetry before turn row;
- dashboard reads a partially covered turn.

### 16.4 Privacy tests

Use unique sentinel strings:

```text
PROMPT_SECRET_...
SOURCE_SECRET_...
TOOL_ARG_SECRET_...
TOOL_RESULT_SECRET_...
MODEL_RESPONSE_SECRET_...
API_KEY_SECRET_...
```

Search the telemetry DB, spool files, API JSON, and event logs. Test fails if any sentinel is
present.

### 16.5 Performance tests

Target:

- telemetry adds less than 5 ms p95 synchronous overhead per emitted event batch;
- task runtime overhead below 2% for a 60-second turn;
- dashboard turn detail query below 250 ms for 5,000 events;
- ingestion sustains 1,000 events/second on local SQLite without blocking task completion.

If detailed tool events exceed limits, batch insertion and indexes must be fixed before
sampling is considered. Silent sampling violates "every observable tool call."

---

## 17. Acceptance scenarios

### Scenario A — ordinary turn

Given one Codex turn with one process and aggregate usage:

- one turn row;
- one invocation row;
- one process spawn/exit pair;
- final status and exit code correct;
- aggregate token totals shown;
- peak context/model-request count shown as unknown if request granularity is absent.

### Scenario B — retry

Given rate limit then success:

- one turn;
- two invocations;
- retry count one with reason `rate_limit`;
- raw token work includes both;
- work is grouped into `primary` and `retry`;
- context growth between turns does not count retry repetition as session growth.

### Scenario C — tool loop

Given three model requests and two groups of tool calls:

- model request count three;
- tool-loop rounds two;
- peak context is max request context, not sum;
- total token work is sum;
- amplification is greater than one;
- graph shows one invocation, not three subprocesses.

### Scenario D — duplicate process

Given two overlapping agent processes for the same turn without retry linkage:

- both are visible;
- probable duplicate flag appears;
- raw and deduplicated totals are separately labeled;
- no usage is silently discarded.

### Scenario E — timeout race

Given gateway timeout, terminate request, and process exit five seconds later:

- timeout timestamp and process exit timestamp are distinct;
- turn status is timed out;
- actual exit code/signal is retained;
- no fake exit code zero.

### Scenario F — privacy

Given all sentinel payloads:

- none exist in telemetry persistence, upload, spool, or APIs;
- counts, durations, categories, and statuses remain available.

---

## 18. Known failure modes and required mitigations

| Failure/fuck-up | Required mitigation |
|---|---|
| Cached tokens counted twice | Adapter declares inclusive/exclusive semantics from fixtures; otherwise context is unknown |
| Aggregate usage mislabeled as one model request | `usage_granularity`; request count and peak context stay `NULL` |
| Retry appears as context growth | Compare first/last request positions and group retry work separately |
| Tool loop appears as subprocess duplication | Tool/model events remain children of one invocation |
| Duplicate subprocess silently replaces old PID | Emit duplicate event before current `_register_process()` replacement/termination |
| Timeout recorded as process exit | Separate timeout, termination request, and actual exit events |
| PID reused | Use node + start time + generated process instance ID |
| OpenCode server PID counted once per turn | Model persistent server separately from message invocation |
| Remote events lost during outage | Atomic local spool and idempotent replay |
| Duplicate uploads inflate totals | Stable event IDs plus `INSERT OR IGNORE` |
| Late events overwrite final state incorrectly | Deterministic projection from full event set |
| Worker/gateway clocks disagree | UTC plus source sequence; flag skew; do not derive negative durations |
| Backend schema changes | Versioned sanitized fixtures, parse-error event, coverage degradation rather than crash |
| Backend does not expose subagents/tools | `unsupported`, never false zero |
| Parser stores raw event object | Default-deny field extraction; raw object never reaches recorder |
| Exception leaks prompt/tool payload | Stable error codes and exception classes only |
| Telemetry DB lock delays agent | batched short transactions, WAL/busy timeout, recorder never raises |
| Event volume makes graph unusable | grouped tool nodes, paging, explicit truncation markers |
| Event volume makes storage unbounded | configurable retention and spool cap; emit loss summary when cap forces deletion |
| Gateway crashes before turn creation | ingestion may create a skeletal turn projection from first valid event |
| Result arrives before telemetry | terminal state accepted; late telemetry rebuilds metrics |
| Telemetry arrives before result | turn remains running until result/reconciliation |
| Existing raw artifacts violate expectation | Explicitly separate telemetry privacy guarantee from existing artifact retention |
| Concurrent backend singleton leaks IDs | pass immutable telemetry context per method call; no mutable shared current-invocation field |
| Retry reason contains sensitive backend text | map to stable classified reason; do not persist original error text |
| One-off has no session ID | `session_id=NULL`; all APIs key by mandatory `turn_id` |
| Compaction makes context-growth comparison meaningless | mark discontinuity and return `NULL` |
| Session recreation changes backend context | mark discontinuity and return `NULL` |
| A backend emits cumulative usage several times | persist series semantics and calculate deltas |
| Negative/corrupt token values | reject value, flag event, keep remaining telemetry |

---

## 19. Explicit non-goals

- storing or searching prompts, responses, code, diffs, commands, tool arguments, or tool
  results;
- estimating tokens when the backend does not report them;
- replacing existing gateway routing/session architecture;
- requiring a hosted observability vendor;
- process-tree tracing of arbitrary grandchildren in v1;
- distributed trace perfection across unsynchronized machines;
- cost accounting until model price/version metadata is reliable;
- changing user-task success based on telemetry health;
- implementing all backends before the first useful Codex release.

---

## 20. Definition of done

The task is complete when M1 and M2 are shipped and all their acceptance tests pass:

- Codex local and mesh turns are correlated end to end;
- every observable invocation/process/model/tool/retry/timeout/result/exit fact is retained;
- requested fields and calculations are available with explicit coverage;
- repeated work is separated from context growth;
- graph, diagnostic table, and timeline are usable from the existing dashboard;
- telemetry is idempotent, crash-tolerant, and privacy-safe by default;
- missing backend facts remain visibly unknown rather than fabricated.

Claude/OpenCode support may follow under the same schema without changing the turn model or
dashboard contracts.
