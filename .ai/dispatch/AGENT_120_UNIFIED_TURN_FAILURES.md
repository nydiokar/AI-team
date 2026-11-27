```yaml
job_id: AGENT_120_UNIFIED_TURN_FAILURES
created_at: "2026-11-27T17:24:22.022163+00:00"        # CANONICAL — set once at dispatch, never derive again
status: blocked              # ready | active | blocked | done | dead
owner: Codex
depends_on: []
results_ref: .ai/dispatch/DISPATCH_LOG.md#A120             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_codex_managed_turns.py", "tests/test_transcript_read_a81.py", "tests/test_failure_text_scope.py", "web/src/hooks/useSessionTimeline.jobs.test.tsx"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-27T17:33:27.784527+00:00"
```

# A120 — Unified turn failures

Owner: Codex. [Issue #313](https://github.com/nydiokar/AI-team/issues/313)

## Acceptance

- [x] Recover the archived draft in isolation and review against current main.
- [x] Carry attributed Codex failures through the worker, managed result commit, transcript API and UI.
- [x] Preserve partial replies, legacy consumer fields, Claude classification and failure provenance.
- [x] Record targeted verification and the merged PR.
- [ ] Activate and verify the merged revision on the gateway and both active Codex carriers after operator approval.

## Milestone

The restored draft used base `c9d795b`. The untracked normalizer matched its manifest SHA-256. Implementation started on `fa652ce`. Main already transports and persists explicit errors, partial output and the backend class. The Codex reader now accepts only well-shaped, non-retrying error notifications, and the adapter associates them with the bound native turn. The terminal status decides success or interruption. A useful terminal error takes precedence over the notification. Neither assistant output nor stdout/stderr tails provide transcript failure evidence. The messages API exposes a reason alongside the existing result, and the web timeline renders it as its own notice.

The reader retains the existing eight-turn admission, 4 MiB frame cap and 16 MiB shared event buffer per runtime. Malformed optional notifications are ignored. At 100 simultaneous transcript requests, at most four can materialize results. Other requests receive a retryable 503. Invalid query limits receive a validation error. Transport failure and uncertain execution retain the existing stop/hold behavior. Managed result ingestion retains its authenticated claim-token and streamed byte limits. The transcript route admits four concurrent reads through JSON serialization, caps requests at 1,000 rows and uses the ten-second cooperative SQLite read budget. Each failure message is capped at 1,000 characters. Failed-row compatibility checks inspect only valid result JSON up to 9 MiB and keep raw diagnostics out of the response. The existing file fallback handles DB unavailability. Full historical reply/file bytes, native I/O and network drain are deferred as recorded in context.

## Verification

- 328 targeted offline Python tests passed. The app-server fixture exercised the real JSON-RPC reader, worker serializer, authenticated task-server result ingest, SQLite and authenticated messages API. It covered bound and foreign errors, retry then success, terminal error precedence, cancellation, missing reasons, partial replies, legacy projections, sanitization, Claude provenance and transcript read admission.
- 34 related Vitest tests and the Web UI typecheck passed. The timeline test verified a separate failure notice and the presence or absence of a real assistant reply.
- The first concurrent run hit an existing Codex deadline test under load. An isolated main comparison passed 222 tests and the full targeted candidate rerun passed with two workers. No paid or end-to-end provider runs were used.

```bash
AI_TEAM_BLOCK_LIVE_CLI=1 .venv/bin/pytest --tb=short -n 2 tests/test_codex_managed_turns.py tests/test_codex_app_server.py tests/test_codex_native.py tests/test_transcript_read_a81.py tests/test_failure_text_scope.py tests/test_turn_queue_stage6.py tests/test_codex_managed_carrier_integration.py tests/test_control_api.py tests/test_claude_driver.py tests/test_proactive_turn_delivery.py
pnpm -C web exec vitest related --run src/hooks/useSessionTimeline.ts src/components/timeline/SessionTimeline.tsx src/transport/rawApi.ts
pnpm -C web typecheck
```

## Closure

[PR #315](https://github.com/nydiokar/AI-team/pull/315) merged to `main` as `804563b` at 17:41 UTC on 2026-11-27. Both GitHub CI jobs passed. The merged source, tests and Web UI matched the tested PR head. An isolated schema-generation check against the installed Codex binary confirmed the notification fields without starting a session.

Production activation requires a gateway and task-server deployment and loading the merged worker source on `kanebra` (PM2 `ai-team-worker-kanebra`) and `Horse` (PM2 `ai-team-worker`). At 17:41 UTC on 2026-11-27 the gateway reported `b87acbf`. The `kanebra` worker reported `d3b5673` and was running this Codex session as task `task_11695516`. Its service checkout carried A119 edits. The `Horse` heartbeat reported idle on `85db88c`. Recheck both nodes and verify the actual remote service process before activation. The operator must authorize the worker restarts.
