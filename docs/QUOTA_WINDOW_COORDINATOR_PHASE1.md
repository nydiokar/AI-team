# Quota Window Coordinator Phase 1

Status: A61 finalized observe-only baseline for Claude; activation/warming/classification remain out of scope.

## Architecture

Phase 1 adds `src/services/quota_window_coordinator.py` as a passive service beside `SessionService`. It does not dispatch tasks, create backend sessions, resume backend sessions, send prompts, or parse provider terminal output in the coordinator.

The subsystem has four layers:

- typed quota models and `QuotaAdapter` protocol;
- provider-owned adapters: Claude status-line observation plus unsupported placeholders for Codex and OpenCode;
- `QuotaWindowStore`, a dedicated SQLite WAL database with transactional writes and schema versioning;
- `QuotaWindowCoordinator`, which records sanitized observations and exposes read-only status.

The coordinator is constructed during `TaskOrchestrator` initialization after config, backends, and `SessionService` exist. It is disabled by default. When enabled, it runs only observe cycles and writes sanitized quota state. The in-process Control API exposes the read model at `GET /api/quota-windows` using the existing bearer-protected read-only control pattern. There is no separate `src/control/dashboard.py` route owner in this tree.

## A61 Audit Verdict

Verified against the tree on 2026-07-31:

- Claude is no longer an unsupported placeholder. `ClaudeGetUsageQuotaAdapter` issues a Python Agent SDK control request with subtype `get_usage`, records the server-reported `five_hour` / `seven_day` buckets, and never starts a model turn. (The earlier `ClaudeStatusLineQuotaAdapter` was removed once `get_usage` proved canonical — status-line scraping was a local estimate, `get_usage` is server-backed.)
- Codex remains `UnsupportedQuotaAdapter("codex", "codex_quota_telemetry_not_validated_phase1")`; the Codex telemetry surface is still unverified.
- OpenCode remains unsupported as a quota owner; provider-specific adapters must own provider quota telemetry.
- `GET /api/quota-windows` is wired in `src/control/control_api.py`, bearer-protected, and reads the live in-process coordinator when constructed. With the coordinator flag off, it returns the explicit disabled observe-only shape without constructing the quota store.
- Observe cadence is adaptive. When snapshots contain a known reset boundary, the loop sleeps until the reset-probe lead window, bounded by `QUOTA_OBSERVE_MAX_INTERVAL_SEC`, instead of unconditionally polling every five minutes. Limit-reached or unknown telemetry falls back to the tight observe interval.
- The Web UI is the primary operator surface: System -> Backends -> Quota windows reads `GET /api/quota-windows` and renders disabled, unavailable, and observed bucket states honestly.
- The temporary Telegram digest is separate from the coordinator in `src/services/quota_digest.py`, gated by `QUOTA_DIGEST_TELEGRAM_ENABLED`, and delivered through `NotificationService`. The coordinator contains no Telegram code, and Telegram is not the primary product surface.
- There is still no activation, warming, or reset-semantics classification.

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

# Tight poll interval for unknown/unavailable/limit-reached telemetry. Minimum: 30 seconds.
QUOTA_OBSERVE_INTERVAL_SEC=300

# Maximum adaptive sleep once reset telemetry is known.
QUOTA_OBSERVE_MAX_INTERVAL_SEC=21600

# How long before reset to resume tight probing.
QUOTA_RESET_PROBE_LEAD_SEC=900

# Timeout for the canonical quota read (SDK control request, subtype get_usage).
CLAUDE_GET_USAGE_TIMEOUT_SEC=60

# Stable local account label for principal hashing; no raw account id is stored.
CLAUDE_QUOTA_PRINCIPAL_KEY=claude-max-nyd

# Temporary operator digest. Separate flag; default off.
QUOTA_DIGEST_TELEGRAM_ENABLED=false
QUOTA_DIGEST_INTERVAL_SEC=3600

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

## Web UI Surface

Quota windows are shown in the System tab, directly after backend token-usage cards. This placement keeps provider capacity next to backend/account telemetry instead of mixing it into session work or settings. The panel has no activation controls; it shows:

- observer off: no quota DB, polling loop, or provider calls;
- adapter status summary;
- provider/bucket usage percentage when observed;
- reset boundary when the adapter observed one;
- telemetry quality and unavailable reason.

## Unresolved Codex Telemetry Questions

- Which installed Codex versions expose quota telemetry without sending a model request?
- Is `account/rateLimits/read` available to the CLI, and what authenticated surface owns it?
- What stable schema version or CLI version should validate `rateLimitsByLimitId`, `limitId`, `usedPercent`, `windowDurationMins`, `resetsAt`, and `rateLimitReachedType`?
- Which identity fields can safely produce a stable `principal_hash` without storing raw account data?
- Does telemetry differ between ChatGPT subscription authentication, Codex access tokens, and API-key usage-based billing?

## Claude Telemetry Notes

- Quota telemetry has exactly one source: a Python Agent SDK control request with subtype `get_usage`
  (`src/services/claude_usage_control.py`). It is server-backed subscription quota, not a local estimate,
  and it never starts a model turn.
- Status-line capture (`scripts/claude_statusline_capture.py`, `CLAUDE_STATUS_LINE_JSON_PATH`) is **not** a
  quota source. It exists only to render the operator's terminal status line, and the coordinator never
  reads its output. The script owns a sync check for the user-scope Claude Code `statusLine` setting:
  `.venv/bin/python scripts/claude_statusline_capture.py --check-statusline-settings` fails on drift, and
  `--sync-statusline-settings` repairs it idempotently.
- The adapter reports `authoritative` only when both `utilization` and `resets_at` are present for a bucket.
  Missing or empty `rate_limits` becomes an explicit unavailable snapshot.
- Snapshot freshness is judged from each stored snapshot's own `observed_at`, not from a file mtime.
- `resets_at` is observation only. It is not treated as proof of anchored windows.
- Principal identity uses `CLAUDE_QUOTA_PRINCIPAL_KEY` when configured and stores only a hash plus a human-safe label.

## Verification Commands

Commands used for this implementation pass:

```bash
.venv/bin/pytest tests/test_quota_window_coordinator.py tests/test_control_api.py -q
.venv/bin/python -m py_compile src/services/quota_window_coordinator.py src/services/quota_digest.py src/services/notification_service.py src/control/control_api.py src/orchestrator.py config/settings.py scripts/claude_statusline_capture.py
```
