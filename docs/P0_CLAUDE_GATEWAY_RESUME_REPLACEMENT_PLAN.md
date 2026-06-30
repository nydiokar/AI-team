# P0: Replace Broken Claude Gateway Resume Engine

## Closure Status - 2026-07-01

Status: implementation complete for the SDK default path and the remote usage
propagation audit gap. Deployed canary verification still must be run after this
usage patch is installed on the controller/worker pair.

Code state:

```text
Claude gateway session path selects ClaudeSDKClientDriver through build_driver("auto").
ClaudePrintResumeDriver remains available as fallback only.
Worker ExecutionResult payloads now include compact structured usage parsed from raw_stdout.
Task server accepts payload.usage and persists it to mesh_tasks.usage_json.
Remote orchestrator completion carries structured result.usage and prefers it over raw_stdout parsing.
Cache-unhealthy detection remains the implemented guardrail.
Rollover/handoff remains a deferred safety enhancement, not a P0 blocker.
```

Owner-supplied deployed SDK evidence for session
`14c0db02-0486-4e6b-97f6-d483ecc5b9ba`:

```text
Claude JSONL entrypoint: sdk-py
Claude JSONL sessionId: 14c0db02-0486-4e6b-97f6-d483ecc5b9ba
model: claude-sonnet-4-6
version: 2.1.191
final provided turn timestamp: 2026-06-30T22:49:59.604Z
usage: input_tokens=3, cache_creation_input_tokens=27,
       cache_read_input_tokens=14338, output_tokens=8
```

That evidence satisfies the critical SDK-path proof for the provided deployed
turn: the backend entrypoint was `sdk-py`, the logical session id was stable, and
cache reads dominated cache creation. It also shows why `ccusage` totals are not
the source of truth for this P0.

Targeted non-live tests added for the usage propagation fix:

```text
tests/test_usage_propagation.py
  - worker ExecutionResult usage extraction from Claude raw_stdout
  - task_server payload.usage persistence into mesh_tasks.usage_json
  - orchestrator _mesh_complete_task prefers structured result.usage when raw_stdout has no NDJSON
```

Targeted verification run locally:

```text
.venv/bin/pytest -q tests/test_usage_propagation.py
.venv/bin/pytest -q tests/test_claim_reaper.py::test_submit_result_idempotency_endpoint_function \
  tests/test_claim_reaper.py::test_submit_result_reconciles_failed_telemetry_turn \
  tests/test_mesh_dispatch_timeout.py::test_claimed_remote_task_is_not_failed_by_pickup_timeout \
  tests/test_result_text_ndjson.py \
  tests/test_claude_driver.py
```

Post-deploy canary still required:

```text
1. Install this patch on the controller and the worker that runs Claude.
2. Send only tiny prompts, for example:
   - say exactly: canary one
   - say exactly: canary two
3. Verify Claude JSONL on the worker host:
   - entrypoint == sdk-py
   - same sessionId/backend_session_id across turns
   - cache_read_input_tokens stays high relative to cache_creation_input_tokens
4. Verify controller DB:
   - mesh_tasks.usage_json is populated for the remote Claude turns
5. Restart the worker and verify live SDK sessions become explicit lost/session_lost state,
   not silent unsafe print/resume continuation.
```

## Objective

Fix the Claude gateway execution path so multi-turn Claude Code sessions remain usable from the gateway without detonating quota after a few turns.

The current broken pattern is:

```text
spawn one Claude process per turn
claude -p --resume <session-id> --output-format stream-json --include-partial-messages
growing transcript
```

This must stop being the main Claude session engine.

The goal is operational usability:

```text
gateway user sends turn 1 -> Claude works
gateway user sends turn 2 -> same logical Claude work session continues
gateway user sends turn 3 -> same logical Claude work session continues
no repeated full-context recreation every turn
no forced loss of Claude capability after a few gateway turns
```

## Hard Rule

Do not make "block unsafe resume" the main fix.

Blocking unsafe resume is only a safety sidecar. The P0 fix is to implement and select a working replacement execution mode. The old print/resume path must become fallback-only for long Claude sessions.

## Evidence Behind This P0

Faulty local Claude session:

```text
C:\Users\Cicada38\.claude\projects\C--Users-Cicada38-Projects-tokens-ingest\dac4f1ce-f955-4864-b384-53f42bff2254.jsonl
```

Concrete cache recreation evidence:

```text
turn after ~24m gap: cache_read=7,346 / cache_create=114,213
turn after ~64m gap: cache_read=0 / cache_create=149,062
turn after later resume: cache_read=7,346 / cache_create=170,316
```

This is not explained by wrong cwd/model/session:

```text
cwd: C:\Users\Cicada38\Projects\tokens_ingest
Claude Code version: 2.1.196
entrypoint: sdk-cli
permissionMode: bypassPermissions
git branch: research/lane-b-bridge
auth: claude.ai / firstParty / Pro
```

Secondary issue:

```text
stream-json + --include-partial-messages duplicates usage rows
raw total: 18.94M processed tokens
deduped-by-message-id total: 11.34M processed tokens
```

That reporting inflation is real, but it is not the P0 cause. The P0 cause is the gateway's repeated noninteractive print/resume execution mode.

## Verified Local Interfaces

Local `claude --help` on this machine confirms:

```text
-p, --print
--input-format stream-json       only works with --print
--output-format stream-json      only works with --print
--include-partial-messages       only works with --print and stream-json
--bg, --background               start as background agent
claude agents --json             list background sessions
--remote-control                 start interactive session with Remote Control enabled
```

Local help does not prove that background agents or remote control can accept programmatic follow-up prompts from the gateway. Treat them as candidates, not facts.

Official Claude Agent SDK docs confirm `ClaudeSDKClient` maintains a conversation session across multiple `query()` calls and exposes explicit lifecycle methods such as `query()`, `receive_response()`, `interrupt()`, and `disconnect()`.

Important SDK caveat: the same official docs describe SDK setup around API-key/provider authentication. The current gateway evidence uses `claude.ai` first-party Pro auth. Before choosing the SDK as the default driver, prove whether it can use the required auth/billing mode for this gateway. If it would silently move the gateway from subscription quota to API credits, it is not an acceptable default without explicit operator approval.

## Implementation Strategy

### Phase 0 - Candidate Viability Gate

Before implementing a full replacement, determine which candidate can actually satisfy the gateway constraints.

Required checks:

```text
candidate supports multi-turn sessions
candidate does not call claude -p --resume per gateway turn
candidate can use acceptable auth/billing mode
candidate can run in cwd with Claude Code tools
candidate can cancel/close
candidate can stream or at least report progress
candidate has explicit behavior after worker restart
```

Candidate order:

1. `ClaudeSDKClientDriver`
   - Primary technical candidate.
   - Must prove auth/billing compatibility first.
   - Requires adding `claude-agent-sdk` to `pyproject.toml` only after viability is accepted.
2. `ClaudeBackgroundDriver`
   - Secondary candidate.
   - Use `claude --bg` / `claude agents --json` only if programmatic follow-up prompt control is proven.
   - Log reading alone is not enough.
3. `ClaudeRemoteControlDriver`
   - Candidate only if the remote-control protocol is discoverable and scriptable.
   - Do not assume this from the presence of `--remote-control`.
4. `ClaudePrintResumeDriver`
   - Existing implementation.
   - Keep as fallback only.
   - Never default for long gateway Claude sessions once a replacement is proven.

### Phase 1 - Driver Boundary

Add a small Claude driver boundary before changing orchestration behavior deeply.

Suggested interface:

```python
class ClaudeDriver:
    def start_session(...) -> ExecutionResult: ...
    def send_turn(...) -> ExecutionResult: ...
    def cancel(...) -> None: ...
    def close(...) -> None: ...
    def status(...) -> DriverStatus: ...
```

Driver implementations:

```text
ClaudePrintResumeDriver      existing CLI print/resume fallback
ClaudeSDKClientDriver        continuous SDK client candidate
ClaudeBackgroundDriver       background-agent candidate if proven
ClaudeRemoteControlDriver    remote-control candidate if proven
```

Keep the boundary narrow. The orchestrator should choose a driver; it should not learn candidate-specific process details.

Likely files:

```text
src/backends/claude_code.py
src/orchestrator.py
src/core/session_store.py
src/core/interfaces.py
config/settings.py
tests/
```

### Phase 2 - Required State Model

Persist enough state to make driver behavior explicit.

Required fields or equivalent metadata:

```text
gateway_session_id
backend_session_id
driver_type
driver_status
repo_path
started_at
last_turn_at
process_or_client_alive
lost_after_worker_restart
cache_health: unknown | healthy | unhealthy
cache_unhealthy_count
previous_backend_session_ids
```

Do not delete old backend session ids when rolling over. Keep history for audit and debugging.

### Phase 3 - Non-Live Tests First

No quota-burning live Claude tests initially.

Use fake Claude executable and fake SDK/client implementations to test:

```text
driver starts
driver receives turn 1
driver receives turn 2
driver preserves gateway session identity
driver serializes concurrent sends
driver cancel works
driver close works
worker restart marks live session as lost/detached
fallback print path still works
cache-unhealthy state blocks unsafe print/resume continuation
```

Concurrency requirements:

```text
one in-flight Claude turn per gateway session
bounded number of live Claude sessions per worker
cancel must terminate/interrupt the child process or client
close must not leave orphaned processes
```

Do not use the real Claude CLI in tests. The existing test guard exists because this repo has previously burned paid Claude tokens.

### Phase 4 - Minimal Live Experiment Matrix

Run only after quota is available and the operator approves live testing.

Use a tiny safe repo. Do not run real coding tasks.

Keep constant:

```text
same machine
same cwd
same auth
same Claude Code version
same model
same permission mode
same tool/MCP setup where possible
short prompts only
```

Live tests:

#### A. Existing Print/Resume

```text
turn 1: claude -p "say exactly: turn one"
turn 2: claude -p --resume <id> "say exactly: turn two"
turn 3: claude -p --resume <id> "say exactly: turn three"
```

#### B. Existing Print/Resume Without Partial Messages

Same as A, but no `--include-partial-messages`.

Purpose: prove whether partial messages only affect telemetry or also affect cache behavior.

#### C. ClaudeSDKClientDriver

```text
async with ClaudeSDKClient(options=options) as client:
    await client.query("say exactly: turn one")
    async for msg in client.receive_response(): ...
    await client.query("say exactly: turn two")
    async for msg in client.receive_response(): ...
    await client.query("say exactly: turn three")
    async for msg in client.receive_response(): ...
```

Only valid as winner if auth/billing mode is acceptable.

#### D. ClaudeBackgroundDriver

Run only if programmatic follow-up control is proven.

Must prove turn 2 and turn 3 are sent into the same background session, not just that logs can be read.

#### E. Stream-JSON Stdin Probe

Probe only. Do not accept as the main fix unless it proves persistent multi-turn behavior without print/resume recreation.

Reason: local help says `--input-format stream-json` only works with `--print`, so it may still be the same print-mode lifecycle.

For every live test, extract from local Claude JSONL/NDJSON:

```text
test_name
turn_number
session_id
backend_session_id
command_or_method
input_tokens
cache_read_input_tokens
cache_creation_input_tokens
output_tokens
cache_hit_ratio
gap_seconds
new_os_process_or_client_spawned
same_logical_claude_session_continued
```

Do not rely on ccusage daily totals.

### Phase 5 - Selection Rule

The winning driver is the first candidate that proves all of this:

```text
supports gateway multi-turn use
does not require repeated claude -p --resume per user turn
preserves same logical work session
does not recreate full prior context every turn
uses acceptable auth/billing mode
has usable cancel/close behavior
can stream or report progress back to gateway
can run under current worker orchestration
has explicit behavior on worker restart
```

Expected technical winner may be `ClaudeSDKClientDriver`, but do not assume it. Prove it.

### Phase 6 - Make Selected Driver Default

After the experiment:

```text
new Claude gateway sessions -> winning continuous driver
short one-off commands -> print driver allowed
old resumed Claude sessions -> print driver fallback only
lost live session -> handoff/new session or guarded resume
cache-unhealthy session -> no silent print/resume continuation
```

Do not keep `claude -p --resume` as the default for long Claude sessions.

## Safety Sidecars

These are required, but secondary to the replacement.

### Remove Partial-Message Duplication

Current print path should remove:

```text
--include-partial-messages
```

unless it is explicitly needed for UI streaming.

If partial messages are used, usage must be deduped by assistant message id. This is not the P0 fix; it prevents false telemetry and bad decisions.

### Cache Health Detector

Parse the first assistant usage object after each gateway turn.

Mark session unhealthy if:

```text
cache_creation_input_tokens > 50_000
and
cache_read_input_tokens / max(cache_read_input_tokens + cache_creation_input_tokens, 1) < 0.2
```

Behavior:

```text
do not silently continue through ClaudePrintResumeDriver
offer handoff/new session or switch to proven live driver
```

This guardrail prevents quota suicide. It is not the replacement.

### Rollover Policy

For huge or unhealthy sessions:

```text
create deterministic gateway handoff
start fresh Claude session through winning driver
bind gateway session to new backend session id
keep old backend session id in history
```

Large/unhealthy threshold:

```text
transcript > 5 MB
or dedup processed tokens > 2M
or cache unhealthy twice
or idle gap exceeds effective cache TTL and transcript is already large
```

Without live Claude quota, the handoff must be deterministic from gateway state. With quota available, a Claude-generated handoff can be added later.

## Service Boundary Checklist

Apply this before marking any new driver endpoint/handler/worker path done.

```text
Concurrency:
  one in-flight turn per session; bounded live Claude sessions per worker.

Memory at scale:
  do not hold unbounded transcript/raw stream content in memory; stream to task artifact/DB as existing backend patterns do.

Request size:
  reject or warn on oversized user prompts before sending to Claude.

Timeout:
  per-turn timeout and inactivity timeout must still apply.

Malformed input:
  driver must fail structured, not crash the worker.

Backing resources:
  missing SDK package, missing Claude executable, auth failure, and lost live client must produce explicit session state.
```

## Acceptance Criteria

P0 is complete only when:

1. Gateway can run 3+ real Claude turns through the selected replacement driver.
2. The selected driver does not call `claude -p --resume` for each turn.
3. Claude JSONL/NDJSON proves no repeated full-context recreation pattern.
4. The old print/resume path still exists as fallback, not default.
5. Worker restart/lost-session behavior is explicit.
6. Cache-unhealthy detection exists as a guardrail, not as the main solution.
7. No tests require live Claude unless explicitly marked and operator-approved.

## Explicit Non-Goals

```text
Do not spend P0 on perfect accounting.
Do not block Claude access without implementing a replacement.
Do not assume stream-json stdin is persistent conversation mode.
Do not assume SDK is acceptable before auth/billing is proven.
Do not assume background/remote-control can accept programmatic follow-ups before proving it.
Do not rewrite the orchestrator before proving the driver.
Do not run expensive real coding tasks as experiments.
Do not trust ccusage totals alone; validate from Claude JSONL/NDJSON per turn.
```

## Final Target Architecture

```text
Gateway session
  -> ClaudeDriver boundary
    -> selected continuous driver by default
       - ClaudeSDKClientDriver if auth/billing and cache behavior prove acceptable
       - otherwise ClaudeBackgroundDriver or ClaudeRemoteControlDriver if proven
    -> ClaudePrintResumeDriver fallback only
  -> per-turn cache health observation
  -> rollover/handoff when session becomes large or unhealthy
```

Main fix:

```text
replace repeated noninteractive claude -p --resume with a proven continuous Claude driver
```
