# Session Cache Heartbeat Specification

Status: proposed. Not built. Flag-gated design, default OFF.
Date: 2026-08-25.

## 1. Purpose

Keep expensive Claude Code prompt caches hot while an agent is intentionally waiting
for long-running work, without turning idle sessions into an unbounded paid loop.

This feature is about preserving an existing live session's cached prefix. It is not
quota-window prewarming, not context rollover, and not a replacement for watched jobs
or Case wait-groups.

## 2. Current Substrate

The implementation must reuse the existing control-plane patterns:

- `session_id` is the heartbeat key. A session is the object whose native Claude Code
  conversation and prompt cache are being preserved. Cases and jobs are reasons, not
  identities.
- The Wake-Dispatcher already scans open Cases and drives bounded autonomous paid
  work (`TaskOrchestrator._start_wake_dispatcher`,
  `_wake_dispatcher_tick_once`, `_continue_case_once`).
- Single-flight paid automation already uses deterministic `mesh_tasks` rows plus
  `claim_task()` leases. Continuation, respawn, quota resume, and transient retry all
  follow this pattern.
- Case waits are durable `flow_events`, especially `worker.wait_pending`,
  `task.finished`, and `worker.wait_resolved`.
- Watched jobs already detach long commands, store `jobs.session_id`, and can post a
  follow-up instruction when `notify_agent` is true.
- Claude cache telemetry is observed after turns through usage fields such as
  `cache_read_input_tokens` and `cache_creation_input_tokens`.

## 3. Provider Facts And Constraints

Anthropic supports a raw Messages API cache prewarm using `max_tokens: 0`, but this
gateway does not use the raw Messages API for live agents. It uses Claude Code through
the Agent SDK / CLI. Therefore v1 heartbeats are ordinary turns sent to the existing
session.

Claude Code supports background Bash commands, and its docs state that background
commands allow new prompts while they continue running. The docs also distinguish
foreground subagents/commands and process termination behavior. The gateway must not
assume that injecting a prompt into a BUSY Agent SDK session is safe while a foreground
tool call is active.

Resulting rule:

> v1 never heartbeats a BUSY session. It only heartbeats sessions that are safely idle
> and whose wait condition is durable outside the model turn.

## 4. Non-Goals

- No heartbeat for arbitrary foreground Bash/tool execution in v1.
- No heuristic "last activity was Bash and then nothing happened" wake in v1.
- No raw Anthropic API call pretending to touch a Claude Code session cache.
- No unbounded agent-controlled keepalive.
- No new Manager-only scheduler. Managers are just one producer of session heartbeat
  intent.
- No separate global daily budget in v1. The binding controls are per session
  heartbeat episode, per owner expiry, and operator kill switch.

## 5. Heartbeat Intent

Add a durable, session-keyed heartbeat controller plus separate owner records. The
controller answers "may this session be heartbeated now?" Owners answer "why is this
session worth preserving?" This avoids both duplicate heartbeats and premature stop
when two valid waits overlap.

A new table pair is clearer than encoding policy state in `mesh_tasks`:

```sql
CREATE TABLE session_cache_heartbeats (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    ttl_sec INTEGER NOT NULL,
    interval_sec INTEGER NOT NULL,
    next_due_at TEXT,
    expires_at TEXT,
    beat_count INTEGER NOT NULL DEFAULT 0,
    max_beats INTEGER NOT NULL,
    hard_max_beats INTEGER NOT NULL,
    last_beat_task_id TEXT,
    last_cache_touch_at TEXT,
    last_cache_read_tokens INTEGER,
    last_cache_creation_tokens INTEGER,
    circuit_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE session_cache_heartbeat_owners (
    id TEXT PRIMARY KEY,
    heartbeat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_runtime_sec INTEGER,
    started_at TEXT NOT NULL,
    expires_at TEXT,
    stop_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Uniqueness:

- One active heartbeat controller per `session_id`.
- Multiple active owner records may point at that controller.
- The controller stops when it has no active owners, or when a guardrail trips.
- Beat counts are per session heartbeat episode, not per owner. Adding a new owner
  does not reset the beat count unless the prior controller is already stopped and a
  new episode is explicitly created.

Valid `reason` values:

- `case_wait_group`
- `watched_job`
- `manual`
- `agent_requested`

Valid `status` values:

- `observe_only`
- `active`
- `completed`
- `stopped`
- `circuit_open`

## 6. Producers

### 6.1 Manager Case Wait-Groups

When `arm_wait_group` succeeds, the gateway may create or update a heartbeat owner
for the Manager session if:

- `CACHE_HEARTBEAT_ENABLED` is true, or observe-only mode is enabled;
- the bound Manager session exists and has `backend_session_id`;
- the Manager is Claude Code SDK backed;
- recent telemetry shows an expensive cache worth preserving.

The first heartbeat is not sent at arm time. It is sent only if the group remains
unresolved when the session's cache is approaching expiry. This means the Manager
does not need to predict worker runtime perfectly: elapsed durable waiting time is
enough.

Optionally add `expected_wait_sec` or a `heartbeat` object to `arm_wait_group`. If
present and above threshold, the coordinator can create an active owner immediately.
If absent, it creates an observe-only owner that auto-promotes only after elapsed wait
time proves the risk.

### 6.2 Watched Jobs

When `watch_job` registers a session-owned job, the gateway may create/update a
heartbeat owner if:

- `session_id` is present and valid;
- `notify_agent=true`;
- command mode or attach mode produced a durable `jobs` row.

Extend `watch_job` with optional fields:

- `expected_runtime_sec`
- `cache_heartbeat`: `auto | on | off`, default `auto`

`expected_runtime_sec` lets the coordinator arm earlier, but is not required. If a
session-owned watched job is still running near the cache deadline, that is durable
evidence of long-running work. This is still different from guessing from arbitrary
Bash silence because the command has been detached and registered in `jobs`.

The tool description should also be reworded so agents understand it as "run a long
command detached and come back when it finishes", not as a generic job-creation
distraction.

### 6.3 Manual Operator

The Web UI can enable/disable heartbeat for a session from a session menu. Manual
activation must require:

- `max_beats`;
- `expires_at` or duration;
- reason text.

Manual activation should emit a Telegram notification so the operator does not forget
that a paid heartbeat was armed.

### 6.4 Agent Requested

Expose manager/session tools:

- `request_cache_heartbeat(expected_wait_sec, reason)`
- `stop_cache_heartbeat(reason)`

The agent request is advisory. The gateway still enforces all eligibility, TTL,
cache-cost, and beat-count guards.

The tool defaults to the current `SESSION_ID` supplied by the harness. It must not let
an ordinary agent heartbeat or stop arbitrary sessions by passing someone else's
`session_id`. A Manager may target a session only through a Case-linked worker/session
relationship that the gateway can verify.

## 7. Eligibility Gate

Every scheduled beat re-checks eligibility from current state. If any check fails, the
beat is skipped or the intent is stopped.

Required:

- feature flag is enabled for active mode;
- session row exists;
- session has `backend_session_id`;
- backend is Claude Code SDK, not `print_resume`;
- session status is `AWAITING_INPUT` or another explicitly proven idle state;
- session is not `BUSY`, `CLOSED`, `CANCELLED`, or terminal-error;
- session's pinned node can receive the turn;
- at least one owner is still live:
  - wait-group not resolved/drained for `case_wait_group`;
  - job still `running` for `watched_job`;
  - manual/agent intent not expired or stopped;
- `beat_count < max_beats`;
- `beat_count < hard_max_beats`;
- current time is before `expires_at`;
- recent cache telemetry exceeds `CACHE_HEARTBEAT_MIN_CACHE_TOKENS`;
- TTL class is known.

TTL must be measured or explicitly configured. Do not assume every Claude Code session
has a 1-hour cache. Use observed usage where available, and add a separate cache-TTL
classification field if Claude Code exposes 1-hour vs 5-minute buckets in raw usage on
this installation. If v1 cannot reliably observe the TTL class, require operator
configuration before active heartbeats.

## 8. Scheduling

Default proposed values:

- `CACHE_HEARTBEAT_TTL_SEC=3600`
- `CACHE_HEARTBEAT_INTERVAL_SEC=2700` (45 minutes)
- `CACHE_HEARTBEAT_MAX_BEATS_DEFAULT=6`
- `CACHE_HEARTBEAT_MAX_BEATS_HARD=15`
- `CACHE_HEARTBEAT_MIN_CACHE_TOKENS=100000`

The due time is based on the last successful real turn or heartbeat. For wait-groups
and watched jobs, the coordinator can also use owner `started_at` to avoid sending
anything before the wait has actually lasted long enough to matter.

```text
next_due_at = last_cache_touch_at + min(interval_sec, ttl_sec * 0.75)
```

Never schedule so late that ordinary dispatcher jitter can cross TTL expiry.

## 9. Delivery

Each due heartbeat creates one deterministic `mesh_tasks` lease row:

```text
cachehb:{session_id}:{slot_epoch}
```

The row is pinned to the existing continuation sentinel or a new reserved sentinel so
workers do not claim it as normal work. The Wake-Dispatcher/heartbeat coordinator
claims it atomically before sending the turn. This means concurrent gateway ticks can
all compute the same due heartbeat, but only one can send it.

Heartbeat prompt:

```text
[cache-heartbeat]
The gateway is waking this session only to keep the Claude Code prompt cache warm
while you are waiting on durable long-running work.

Do not call tools. Do not inspect files. Do not make decisions.
Reply exactly CACHE_HEARTBEAT_OK.

If you are not actually waiting on useful work, reply exactly STOP_CACHE_HEARTBEAT.
```

Use:

- `source="cache_heartbeat"`
- `extra_metadata={heartbeat_id, owner_type, owner_id, beat_number}`
- short task timeout, e.g. 120 seconds

Do not rely on the agent prompt as the main guardrail. The gateway already decided the
session is eligible before this prompt is sent.

## 10. Result Handling

On heartbeat return:

- increment `beat_count`;
- store the task id and observed cache read/write tokens;
- set `last_cache_touch_at` to the heartbeat completion time only if the turn produced
  usable cache-read evidence;
- update `next_due_at`;
- if the response contains `STOP_CACHE_HEARTBEAT`, stop the intent;
- if the turn fails with quota/rate-limit/transient provider error, do not retry from
  the heartbeat subsystem; let existing quota/transient machinery own that class;
- if `cache_creation_tokens` is large and `cache_read_tokens` is low, mark
  `circuit_open`.

Cache miss circuit rationale:

The heartbeat exists to avoid a large rewrite. If the heartbeat itself observes that
the prefix was rewritten, the previous prevention attempt already failed. Continuing
blindly can still be useful after the new write, but v1 should stop and require a new
intent so we do not mask bad TTL assumptions or create repeated paid writes.

## 11. Stop Conditions

Stop an active intent when:

- all owner waits resolve:
  - wait-group satisfied and consumed or resolved;
  - watched job terminal (`done`, `failed`, `lost`);
- session closes/cancels;
- Case closes, for `case_wait_group`;
- max beats reached;
- `expires_at` reached;
- cache-miss circuit opens;
- operator disables it;
- agent requests stop;
- feature flag is turned off.

If a Manager re-arms a wait-group after consuming a result, that is a new producer
event and may create a new intent under the same session guardrails.

## 12. Observability And UI

Add heartbeat attribution to cost/usage views:

- source: `cache_heartbeat`;
- reason and owner;
- beat count and max;
- estimated protected cache tokens;
- actual `cache_read_tokens` and `cache_creation_tokens`;
- stopped/circuit reason.

UI:

- session row indicator when heartbeat is armed;
- session menu action to enable/disable;
- detail panel history of beat attempts;
- warning when manually enabled;
- no modal for automatic Case/job intents unless they circuit-open or hit max beats.

Telegram:

- notify on manual enable;
- notify on circuit open;
- notify on hard max reached;
- avoid per-beat notifications by default.

## 13. Rollout Plan

1. Observe-only.
   - Add table/API/read-model.
   - Record would-arm decisions for Manager wait-groups and watched jobs.
   - No paid turns.

2. Manager wait-group active mode.
   - Enable active beats only for `case_wait_group`.
   - Requires explicit wait horizon or operator/manual activation.

3. Watched-job active mode.
   - Extend `watch_job` with `expected_runtime_sec` and `cache_heartbeat`.
   - Reword tool description to make detached long command usage clearer.

4. Agent controls.
   - Add request/stop tools.
   - Keep gateway policy authoritative.

5. Foreground command research.
   - Inspect real Claude Code OTel/tool activity and local DB samples.
   - Only consider BUSY-session heartbeat after proving the SDK can accept the
     prompt without interrupting foreground tool execution. Until then, it remains
     out of scope.

## 14. Acceptance Tests

Use fake backends and real `MeshDB`; no paid CLI in tests.

- Flag OFF writes no intents and sends no heartbeat.
- Observe-only records candidates but sends no heartbeat.
- One active controller per session even if two producers arm it.
- Overlapping owners do not reset beat count and do not stop the controller until all
  owners stop.
- Due heartbeat creates one deterministic lease; racing ticks produce one send.
- BUSY session is skipped and not interrupted.
- Wait-group resolution stops the intent.
- Watched job terminal status stops the intent.
- Manual max beats stops before the hard ceiling.
- Cache miss evidence opens the circuit.
- Agent `STOP_CACHE_HEARTBEAT` stops the intent.
- Session close/cancel stops the intent.
- Remote-pinned session routes through existing affinity path, never locally.

## 15. Service Boundary Checklist

Concurrency:
The scheduler may tick concurrently. Deterministic ids plus `claim_task()` are required
before every paid heartbeat.

Memory at scale:
The scheduler reads bounded due intents. It must not scan or materialize all sessions,
jobs, flow events, or telemetry rows.

Request size:
New API/tool fields are bounded: reason text, ids, and expected runtime. Heartbeat
prompt is fixed-size.

Timeout:
Heartbeat turns use a short timeout. A timeout marks the beat failed and does not
retry inside the heartbeat subsystem.

Malformed input:
Invalid session ids, negative durations, oversized reason strings, unknown modes, and
missing ownership are rejected with structured errors.

Backing resource failure:
If DB is unavailable, no heartbeat is sent. If the target node is unavailable, the
intent waits until expiry or hits normal session/node recovery behavior.

## 16. Adversarial Review And Corrections

Wrong assumption: "A BUSY session waiting on Bash can safely receive another prompt."
Correction: v1 forbids heartbeats to BUSY sessions. Claude Code supports background
Bash, but the gateway does not have proof that all long commands are backgrounded or
that SDK prompt injection is harmless during foreground tool execution.

Wrong assumption: "The gateway can infer long-running foreground commands from silence."
Correction: silence is ambiguous. It could be Bash, model thinking, blocked permission,
network trouble, a dead stream, or a provider wait. v1 uses explicit durable waits only.

Wrong assumption: "Jobs are unreliable, so do not use them."
Correction: watched jobs are currently the best existing primitive for long commands
because they detach work, store DB state, and can re-enter the session. The fix is
tool wording plus optional heartbeat metadata, not a parallel process tracker.

Wrong assumption: "Global daily budget is enough."
Correction: a global budget can starve useful sessions and still allow one session to
loop until it consumes the shared pool. v1 uses one active controller per session,
episode max beats, hard max beats, owner expiry, and an operator kill switch.

Wrong assumption: "Cache rewrite evidence means continue normally."
Correction: a large heartbeat-time cache write means the premise was wrong or the TTL
was missed. v1 opens a circuit and requires a new intent rather than hiding repeated
misses.

Wrong assumption: "Case close stop is redundant."
Correction: it is redundant in healthy flows, but cheap and useful as a backstop. A
closed Case must never keep a Manager heartbeat alive because the wait-group stop path
failed to observe a final event.

Wrong assumption: "Agent-requested heartbeat can be trusted."
Correction: agent request is useful signal, not authority. The gateway enforces
eligibility, cost, TTL, max beats, and stop conditions.

Wrong assumption: "One active owner per session is enough."
Correction: the session is the heartbeat key, but owners are many. A single
controller with multiple owner records prevents duplicate beats while preserving the
session until every valid owner has stopped.

Wrong assumption: "Session id alone is sufficient for authorization."
Correction: ordinary agent tools default to their current session. Cross-session
targeting requires a gateway-verified Case/session relationship.

## 17. Live Evidence: BUSY Worker Session

Observed on 2026-08-25 against live `state/mesh.db`:

- Worker session `1b3f4686632b` was `busy`, pinned to `Horse`, role `worker`,
  with task `task_a3d6e415` claimed since `2026-08-25T15:20:56Z`.
- The task prompt explicitly expected hours-scale bounded serial GCM execution and
  mentioned a prior step taking about 67 minutes.
- The session had no watched-job row and no completed model request usage rows while
  the turn was still running.
- Live activity logs showed repeated `Using Bash` and `Writing response...` events for
  `task_a3d6e415`.
- The session row had no persisted `backend_session_id` yet because the first turn had
  not completed.
- The same Case had wait-group `batch-7-serial-sweep` waiting on `task_a3d6e415`.
- The Manager session for that Case, `737546235615`, was `awaiting_input`, Claude SDK
  live, and had large observed cache usage.

Design consequence:

- v1 would preserve the Manager cache while it waits on this worker.
- v1 would not heartbeat the worker session itself because it is BUSY and mid-turn, only on watched job
bounded to that worker session.
- This supports the rollout order: Manager wait-groups first, watched jobs second,
  foreground-Bash/BUSY-session handling only after a separate live safety proof.

## 18. Sources

- Anthropic prompt caching docs: `https://platform.claude.com/docs/en/build-with-claude/prompt-caching`
- Claude Code prompt caching docs: `https://code.claude.com/docs/en/prompt-caching`
- Claude Code interactive mode, background Bash commands:
  `https://docs.anthropic.com/en/docs/claude-code/interactive-mode`
- Claude Code headless/programmatic behavior:
  `https://docs.anthropic.com/en/docs/claude-code/headless`
- Claude Code monitoring, Bash telemetry fields:
  `https://docs.anthropic.com/en/docs/claude-code/monitoring-usage`
