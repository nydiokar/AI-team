```yaml
job_id: AGENT_92_BACKEND_EVENT_NORMALIZATION
created_at: "2026-09-27T16:59:55.027719+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active             # ready | active | blocked | done | dead
owner: ""
depends_on:
  AGENT_91_OPENCODE_BACKEND_PARITY
results_ref: DISPATCH_LOG.md
evidence: []
updated_at: "2026-09-28T09:52:37+00:00"
```

# DISPATCH — A92 · Canonical backend activity normalization

**Level:** 3 (crosses backend/worker/API boundaries) · **Type:** code + tests
**Authored:** 2026-09-27 · **Status of this packet:** active (implementation and focused offline checks complete; PR closure pending)
**Depends on:** A91 (`AGENT_91_OPENCODE_BACKEND_PARITY`) — integrate after its OpenCode server changes are merged/reviewed; do not edit its in-flight branch or duplicate its implementation.
**Branch:** `feat/backend-event-normalization` + PR + self-merge at close. Do not auto-enqueue a `.task.md` for this Level-3 job.

> **Read this first — why this packet exists.** The Web session running indicator remains the generic “Working…” whenever the gateway SSE stream has no `task_activity` event matching the session’s current task. The event transport and worker forwarder already exist, but live activity creation is scattered among backend implementations: Claude SDK and OpenCode server create labels directly, while Codex app-server events currently feed durable telemetry without producing live activity. This job establishes one validated internal activity contract and connects the in-scope event sources to it without redesigning the durable telemetry contract.

## Why (intent)

Give each supported live backend source one safe, consistent route from native progress events to the UI’s live activity signal. Keep backend-specific parsing at the source boundary, normalize and validate before publishing, preserve durable facts in the existing typed telemetry store, and prevent source payloads from leaking into the API. The literal symptom is Codex’s generic pill; the intended outcome is that Claude SDK, Codex app-server, and OpenCode server all use the same activity contract and future in-scope adapters have an obvious integration point.

## TASK

1. **Re-derive the current tree after A91 lands.** Read `CodingBackend`, `TelemetryContext`, `TelemetryEvent` / `build_event`, `TelemetrySink`, the activity SSE route and worker forwarder, plus the three in-scope producer paths below. Confirm the deployed/public contract before editing. Do not assume that A91’s branch implementation is already on `main`; record the exact commit/PR used as the dependency.

2. **Keep the two event purposes explicit.** Durable usage/tool/process facts continue through existing `TelemetryEvent` validation, per-backend adapters, `TelemetrySink`, `/telemetry/batches`, and `llm_events` / `llm_turns`. High-rate `task_activity` remains a transient, live-only UI hint on the established `emit_event` → worker forwarding → `/events/activity` → gateway SSE path. Do not persist every activity label, store raw backend payloads, create a parallel generic event database/table, or route tool telemetry through the activity event API.

3. **Add the smallest shared activity boundary in `src/core/`.** Define an invocation-scoped, typed/validated canonical activity value and one shared publisher/dispatcher used by backend producers. It must require the correlation needed by the UI (`session_id` and current turn/task ID), accept only a bounded safe label/category and known optional metadata, reject or omit malformed/unscoped values without raising into execution, and emit the established `task_activity` envelope. Use the existing observability event spine so remote-worker forwarding remains automatic. Keep transport, SSE, and frontend contracts unchanged unless code evidence proves the existing contract cannot carry the normalized value.

4. **Keep source parsing backend-specific; normalize at the shared boundary.** Add/adjust only the adapters needed for the live sources:
   - Claude SDK: route its existing `ToolUseBlock` / thinking / text-derived progress callback through the common activity publisher. Do not change Claude CLI or the legacy print-resume driver.
   - Codex app-server: map supported `item/started` / `item/completed` kinds from the already-consumed event loop into safe progress labels; use the existing `TelemetryContext` correlation. Do not copy command text, file paths, MCP arguments/results, agent message text, or raw params into activity or telemetry.
   - OpenCode server: route the A91 server-SSE labels through the same publisher and preserve its existing bounded stream parsing and tool telemetry. Do not change the OpenCode CLI backend.

5. **Preserve durable telemetry semantics and avoid duplicate facts.** Retain the current Codex and Claude telemetry adapters and OpenCode server `build_event`/sink behavior. When one native event represents both a live activity and a durable tool lifecycle event, emit one canonical instance through each appropriate channel (activity live-only; `tool.call.*` durable), without duplicate telemetry rows or a second tool-event mapping layer. Do not turn every raw provider event into a canonical event; add only mappings backed by observed fixtures or current code contracts.

6. **Verify correlation and event handling end to end with fakes/fixtures only.** Trace `TelemetryContext.turn_id` and `session_id` to the session’s `lastTaskId` and prove the UI adapter can match the emitted event. Add focused tests for each in-scope producer and shared boundary: valid mapping, required IDs, malformed/unrecognized source event, bounded/sanitized label, no secret/raw payload leakage, and exactly-once durable tool telemetry. Exercise the existing forwarder/adapter/API contract without a live worker, network, paid CLI, or runtime restart. Run only relevant targeted tests and record commands/results.

7. **Close with the service-boundary answers in this packet.** State producer call concurrency, queue/backpressure behavior, per-event/request bounds, timeouts, malformed-event handling, and behavior when the gateway/forwarder/telemetry sink is unavailable. Reuse the existing bounded worker forwarder and best-effort telemetry semantics; if the new shared boundary adds a queue/resource or discovers an uncovered case, bound it or record a concrete deferral here before closure.

## Adversarial pre-dispatch pass (one round)

### F1 (P1 — generic raw-event adapter) — A catch-all adapter can silently trust provider dictionaries
**Failure scenario:** a future backend adds a nested field containing command arguments or response text; a generic pass-through serializer persists or emits it because the source schema was treated as universal.
**Resolution:** fixed in this packet: each provider keeps a narrow source parser; only the bounded canonical activity value crosses the shared live boundary. Durable data continues through the event-specific allowlist in `TelemetryEvent`.

### F2 (P1 — live/durable event conflation) — Persisting every activity pulse duplicates telemetry and creates unbounded event volume
**Failure scenario:** a backend emits frequent text/thinking updates; each becomes an `llm_events` row, creating write load and a misleading action history while tool lifecycle rows already exist.
**Resolution:** fixed in this packet: activity labels remain transient; only existing canonical durable facts go to `TelemetrySink` and the telemetry database. Add no table, migration, or second telemetry schema.

### F3 (P1 — correlation mismatch) — Events arrive but never match the running pill
**Failure scenario:** an adapter uses a native backend thread/item ID or a stale task ID instead of the current gateway turn ID; `useTaskActivity` filters it out and the user still sees “Working…”.
**Resolution:** fixed in this packet: require both context session ID and current turn/task ID; executor must trace them to `session.lastTaskId` and assert matching UI adaptation using deterministic fixtures.

### F4 (P1 — overlapping OpenCode work) — A92 rewrites code A91 is still changing
**Failure scenario:** A92 forks from main while A91’s server activity/telemetry changes are in flight, causing lost fixes or conflicting event mapping.
**Resolution:** fixed in this packet: hard dependency on A91 and require its merged commit/PR to be re-read before touching OpenCode server. A90 remains a separate locality job; do not edit its interface seams.

### F5 (P1 — CLI scope creep / paid smoke) — Verification changes a backend the operator excluded or invokes a live provider
**Failure scenario:** a broad “all backends” abstraction alters Claude CLI or OpenCode CLI parsing, or an integration test invokes a paid coding-agent executable.
**Resolution:** fixed in this packet: the three in-scope sources are enumerated; both CLI paths are explicit non-goals; use fakes/fixtures and offline targeted tests only.

## TYPE

Code + focused tests. Branch `feat/backend-event-normalization`; PR and self-merge at close. Do not change runtime config, database schema, public API shape, or start a worker/gateway deployment as part of this job.

## CONTEXT (reuse verbatim)

- `src/core/telemetry.py`: canonical durable `TelemetryEvent`, default-deny `EVENT_ATTRIBUTE_ALLOWLIST`, `TelemetryContext`, and `build_event`. This is the persistent telemetry contract; do not replace it with a broader raw-event envelope.
- `src/core/telemetry_adapters/codex.py` and `src/core/telemetry_adapters/claude_stream_json.py`: source-specific adapters map provider structures to typed durable events. Claude adapter runs at the `ClaudeCodeBackend` result boundary; Claude SDK live progress is a different path.
- `src/control/telemetry_sink.py`: `TelemetrySink` protocol and database/HTTP/fanout implementations. The remote worker sink already batches/spools durable telemetry.
- `src/backends/claude_driver.py::_make_activity_cb` and `ClaudeSDKClientDriver._run_turn`: Claude SDK currently emits `task_activity` directly from SDK content-block labels. Exclude `ClaudePrintResumeDriver` / Claude CLI.
- `src/backends/codex_native.py::CodexBackend._run`: Codex app-server already receives `turn/*`, `item/*`, and token-usage notifications. `CodexTelemetryAdapter` maps some item events to durable `tool.call.*`, but no live activity event is emitted there.
- `src/backends/opencode.py::OpenCodeServerBackend._read_activity_events` and `_emit_tool_telemetry`: OpenCode server parses bounded backend SSE and currently maps labels / durable tool telemetry locally. A91 owns current OpenCode server parity work; re-read its merged implementation before touching this path. Exclude `OpenCodeBackend` (CLI).
- `src/core/observability.py::emit_event` and `register_event_forwarder`: appends the established event envelope and fans out best-effort. `src/worker/agent.py::_ActivityForwarder` provides bounded remote-worker forwarding to authenticated `/events/activity`; `src/control/task_server.py::submit_activity` re-emits into the gateway-owned event stream.
- `web/src/transport/eventAdapter.ts`, `web/src/hooks/useTaskActivity.ts`, `web/src/hooks/useSessionTimeline.ts`, and `web/src/components/timeline/SessionTimeline.tsx`: UI accepts `task_activity` and only substitutes the live label when `sessionId` + `taskId` match. Otherwise “Working…” is intentionally rendered.
- `docs/LLM_TURN_OBSERVABILITY_SPEC.md` is the persistence/privacy contract; `docs/SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md` distinguishes transient live SSE hints from durable read models and warns against treating the UI event union as universal truth.
- Existing related work: A59 (`AGENT_59_ACTIVITY_FORWARDER_TESTS.md`) covers queue bounds/failure isolation; A91 (`AGENT_91_OPENCODE_BACKEND_PARITY.md`) owns OpenCode server parity; A90 (`AGENT_90_BACKEND_LOCALITY_ABSTRACTION.md`) is a separate backend lifecycle/locality refactor. Do not duplicate or broaden either.
- Correlation is constructed with `turn_id=task.id` / `session_id` at `src/orchestrator.py` and reconstituted from the same fields in `src/worker/agent.py`; the executor must still verify the complete active path rather than infer this from naming.

## ACCEPTANCE (proof, not vibes)

1. One typed, validated shared activity publication contract exists and is used by all three in-scope live producers; raw provider objects never cross it.
2. Fixture/fake tests prove the three producers emit UI-matchable session/task-correlated labels, reject unsafe/unscoped input, and do not leak backend content. An ordinary valid Codex tool event produces its existing durable tool telemetry exactly once plus the live activity signal.
3. Existing worker forwarding and gateway SSE contracts remain compatible; focused offline tests pass without invoking Claude/Codex/OpenCode executables or requiring a deployed worker.
4. Existing durable telemetry schema, retention, API, and read models remain unchanged; the implementation introduces no activity persistence table or migration.
5. The packet contains explicit service-boundary answers and evidence paths/commands. If remote live acceptance needs the operator’s worker restart, document that as deferred acceptance; do not restart it.

## RESERVED DECISIONS (surface, do not guess)

- **R1 — Canonical label vocabulary.** Use safe, user-readable labels supported by observed backend data. The executor may choose exact wording, but must preserve the existing UI string contract and document mappings; do not expose arbitrary backend-provided text.
- **R2 — Unsupported source events.** Ignore unsupported events for live activity while preserving existing durable coverage behavior. Do not add a “raw event” escape hatch or make unknown events fatal to the turn.
- **R3 — Live activity persistence.** Keep activity labels ephemeral; persist only the already-defined durable telemetry facts. Changing this requires a separate design because it changes retention, volume, and read-model semantics.

## SCOPE OUT

- Claude CLI / print-resume driver and OpenCode CLI backend.
- Replacing or broadening `TelemetryEvent`, `TelemetrySink`, the telemetry database/API, SSE envelope, `/events/activity`, worker protocol, or frontend `GatewayEvent` model.
- Durable storage/replay of every activity label, transcript streaming, arbitrary raw event retention, full backend event catalog coverage, or a generic adapter that accepts unvalidated provider dictionaries.
- Codex/OpenCode native runtime upgrades, provider smoke calls, live worker/gateway restarts, production deployment, or A90 locality/lifecycle behavior.
- Refactoring backend method signatures or changing `CodingBackend` ownership/locality semantics.

## TRAIL / EVIDENCE (fill at close)

- Dependency commit/PR and merge point: <fill at close>.
- Focused test commands/results: <fill at close>.
- Changed files and privacy/correlation review: <fill at close>.
- Service-boundary answers and any explicit deferred live acceptance: <fill at close>.

---
## Milestone (burndown)

- [x] Re-derive A91 merged state and current contracts; record dependency commit/PR.
- [x] Implement the shared validated activity value/publisher without altering durable telemetry or external APIs.
- [x] Route Claude SDK, Codex app-server, and OpenCode server activity through the shared boundary; CLI paths untouched.
- [x] Add focused fake/fixture tests for mapping, correlation, validation, privacy, and no duplicate durable telemetry.
- [x] Run targeted offline checks; record exact results and changed files.
- [x] Complete service-boundary checklist; document bounded behavior and any operator-gated acceptance.
- [ ] Adversarially review committed diff; resolve P0/P1 findings; update dispatch state/log at closure.

**Progress — 2026-09-28:** A91 re-read at merged PR #174, merge commit
`0c88c3a3f969c2a713f2d58634f814618deba2c7` (present in `main` ancestry). The live
activity context is now implemented in `src/core/activity.py`; all three in-scope
producers route through it. The gateway submission path sets `session.last_task_id` to
the returned task ID (`src/control/control_api.py`), the orchestrator sets
`TelemetryContext.turn_id=task.id` and its session ID, the worker reconstructs those
IDs from the dispatch payload/task row, and `web/src/transport/eventAdapter.ts` retains
both IDs for `useTaskActivity` matching.

**Verification:** `.venv/bin/pytest --tb=short tests/test_backend_activity.py
tests/test_codex_native.py tests/test_opencode_backend.py tests/test_activity_forwarder.py
tests/test_telemetry_contract.py tests/test_telemetry_privacy.py
tests/test_codex_telemetry_adapter.py tests/test_claude_telemetry_adapter.py
tests/test_telemetry_sink.py` — 125 passed (2026-09-28). `git diff --check` and focused
Ruff on `src/core/activity.py` + `tests/test_backend_activity.py` pass. The
`web/src/transport/adapters.test.ts` run reported 22/22 tests passed, then Vitest
exited with status 134 after its summary under Node 24.3.0; this runner shutdown
anomaly is retained as a verification caveat.

### Adversarial pass outcomes

- **F1:** source-specific parsers produce only validated category/tool values; arbitrary
  provider dictionaries and extra metadata are rejected or ignored.
- **F2:** activity only calls `emit_event`; Codex/Claude/OpenCode durable adapters and
  sinks remain unchanged. The existing diagnostic `events.ndjson` append remains part of
  that spine; no activity rows/table/migration or durable telemetry/read-model writes were added.
- **F3:** both IDs are required; `turn_id` is explicitly the current task ID. Fixtures
  assert publisher/API envelopes and the UI adapter's session/task pair.
- **F4:** A91 merged code was re-read at PR #174 / `0c88c3a` before editing
  `OpenCodeServerBackend`.
- **F5:** only Claude SDK, Codex app-server, and OpenCode server paths changed; no CLI,
  live provider, paid executable, service restart, or A90 interface change.

### Service-boundary answers

- **Concurrency / memory:** The publisher is stateless, synchronous, and adds no queue. Each
  call creates one small validated value (IDs ≤128 chars; labels are fixed); concurrent calls
  retain no per-event state. At 100 concurrent calls, publisher memory remains O(100) small
  temporary models, with no retained activity backlog. Codex already admits at most 8 channels;
  OpenCode A91 bounds concurrent event readers/turns at 8. Claude invokes the callback from its
  existing per-session SDK stream reader. The worker forwarder remains a single thread with a
  256-entry queue.
- **Request/event bounds:** The activity value is limited to two 128-character IDs, one category,
  and an allowlisted tool enum. Existing forwarded `ActivityPayload` caps label at 200 chars and
  both IDs at 128. OpenCode source frames are capped at 256 KiB by A91. Codex app-server frames
  are capped at 4 MiB, total buffered data at 16 MiB, and each event channel at 256 notifications.
  Claude SDK owns its existing stream framing; A92 does not copy content into activity.
- **Backpressure / timeout:** No activity queue is added. Local event append is the existing
  synchronous, best-effort `emit_event`; remote `offer()` is nonblocking and drops when the
  256-entry queue is full. Forwarder HTTP POST timeout is 3 seconds. OpenCode SSE reads use a
  1-second socket timeout and reconnect; Codex RPC requests use the existing 30-second default.
  `emit_event`'s local filesystem append has no explicit timeout: a stalled filesystem could
  delay its producer thread. This is an existing observability-spine limitation; bounding it
  needs a separate writer/queue design and is deferred here. Activity does not affect turn
  results if emission raises or its sink/forwarder is unavailable.
- **Malformed input:** Pydantic rejects unscoped IDs, oversized IDs, unknown categories/tools,
  and extra metadata; publication returns false without raising. Claude and Codex map only
  recognized source types; unknown types are ignored for activity. OpenCode retains A91's
  bounded frame/JSON/shape validation and maps only allowlisted tool names. Existing durable
  source adapters retain their current malformed-event and coverage behavior.
- **Backing-resource failure:** Durable telemetry remains independent and best-effort through
  the existing sinks. An unavailable local event file or gateway drops the transient activity;
  worker forwarding failures are counted/throttled by the existing forwarder. No retry or
  persistence was added. Live acceptance on the deployed worker remains operator-gated because
  A91's merged module has not been verified in the running worker; no restart was performed.

## Closure (fill on completion)

Record the implementation verdict, A91 dependency evidence, files changed, focused verification, privacy/correlation findings, service-boundary answers, F-tag outcomes, and any operator-gated acceptance. Do not mark complete while required code/test work remains.
