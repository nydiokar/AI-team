# Quota Window Coordinator Phase 1

Status: implemented observe-only baseline.

> :warning: **Warning:** check QUOTA_OBSERVE_INTERVAL_SEC addition. 

## Architecture

Phase 1 adds `src/services/quota_window_coordinator.py` as a passive service beside `SessionService`. It does not dispatch tasks, create backend sessions, resume backend sessions, send prompts, or parse provider terminal output in the coordinator.

The subsystem has four layers:

- typed quota models and `QuotaAdapter` protocol;
- provider-owned adapters, including Phase 1 unsupported placeholders for Codex, Claude, and OpenCode;
- `QuotaWindowStore`, a dedicated SQLite WAL database with transactional writes and schema versioning;
- `QuotaWindowCoordinator`, which records sanitized observations and exposes read-only status.

The coordinator is constructed during `TaskOrchestrator` initialization after config, backends, and `SessionService` exist. It is disabled by default. When enabled, it runs only observe cycles and writes sanitized quota state. The dashboard exposes the read model at `GET /api/quota-windows` using the existing bearer-protected read-only control pattern.

## Safety Boundaries

Phase 1 intentionally excludes synthetic activation, window warming, classification probes, and AUTO_ACTIVATE. There are no activation methods or placeholder commands that execute provider requests.

Observation rules:

- adapters must not send model requests during observation;
- unsupported telemetry is persisted as an explicit unavailable/unsupported snapshot;
- reset timestamps are stored only when provider telemetry supplies them;
- provider parsing belongs inside adapters;
- timestamps are normalized to UTC before storage;
- credentials, prompts, repository paths, raw provider output, account ids, and usernames are not persisted;
- unknown active-user-session state is surfaced as unknown, not treated as permission to act;
- adapter schema/version mismatch disables the adapter before observation.

## Configuration Example

```env
# Disabled by default. When true, runs observe-only quota polling.
QUOTA_COORDINATOR_ENABLED=false

# Dedicated local SQLite state. Keep separate from mesh.db.
QUOTA_DB_PATH=state/quota_windows.db

# Poll interval for observe-only adapters. Minimum: 30 seconds.
QUOTA_OBSERVE_INTERVAL_SEC=300 <--- this must be reconsidered or done better, we need to no observe every 5 min if the quota window is already running and confirmed, it's redundant - this is not mission-critical and once we confirm the window probably need to stop asking the services. Reduce constant calls to backend providers and spam. 

```

## Read Status

```http
GET /api/quota-windows
Authorization: Bearer <DASHBOARD_TOKEN>
```

Response shape:

```json
{
  "enabled": false,
  "mode": "observe_only",
  "adapters": [],
  "buckets": [],
  "latest_snapshots": []
}
```

## Unresolved Codex Telemetry Questions

- Which installed Codex versions expose quota telemetry without sending a model request?
- Is `account/rateLimits/read` available to the CLI, and what authenticated surface owns it?
- What stable schema version or CLI version should validate `rateLimitsByLimitId`, `limitId`, `usedPercent`, `windowDurationMins`, `resetsAt`, and `rateLimitReachedType`?
- Which identity fields can safely produce a stable `principal_hash` without storing raw account data?
- Does telemetry differ between ChatGPT subscription authentication, Codex access tokens, and API-key usage-based billing?

## Unresolved Claude Telemetry Questions

- Which Claude Code versions reliably expose status-line rate limit JSON without causing an API/model turn?
- Are `rate_limits.five_hour` and `rate_limits.seven_day` present before the first model response in a session?
- Which command or SDK surface identifies subscription/auth mode without persisting raw identity fields?
- What schema/version marker should disable the adapter when status-line fields change?
- Do visible `resets_at` fields correspond to subscription quota buckets, API billing, or separate credits for each auth mode?

## Verification Commands

Commands used for this implementation pass:

```powershell
python -m pytest tests/test_quota_window_coordinator.py tests/test_dashboard.py
python -m py_compile src/services/quota_window_coordinator.py src/orchestrator.py src/control/dashboard.py config/settings.py
```
