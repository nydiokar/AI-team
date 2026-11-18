# Codex quota observation — design and implementation

Date: 2026-11-15. Reviewed: 2026-11-18. Status: PR #288 merged as `8a026a2`;
gateway/task-server deployed and verified. Worker activation and natural
production observation remain separate rollout gates.
Scope: the Home quota panels and the provider-neutral observation path behind
`/api/quota-windows`. No model turn, no new database, no UI layout change.

## 1. Problem

On 2026-11-15 the Home quota card showed Claude's 5-hour and 7-day windows
continuously, while Codex's windows appeared only while a Codex turn was running
and vanished after about 15 minutes. The operator asked why, and why no check had
caught the asymmetry.

Observed live at `2026-11-15T12:11Z`:

| Provider | Adapter state | Last check |
|---|---|---|
| Claude | `ready` | `2026-11-15T12:11:04Z` |
| Codex | `unavailable` / `codex_quota_telemetry_not_validated_phase1` | unchanged since `2026-09-25` |

The latest Codex `model.session_usage` event was at `2026-11-15T11:28:47Z`, on
node `kanebra`, reporting primary `36%` of a 300-minute window and secondary
`21%` of a 10,080-minute window. At 12:11Z that event was 43 minutes old, so
`codex_usage()` returned `[]`.

## 2. Diagnostic — why Claude had data and Codex did not

These are two different acquisition mechanisms feeding the same read model:

**Claude (continuous).**
`src/worker/agent.py:2300 _quota_observe_loop` runs on the worker that owns the
Claude harness. `_observe_quota_once()` calls the free Agent SDK `get_usage`
control request, POSTs a typed body to `/telemetry/quota-observation`, and the
controller persists it with `ingest_worker_quota_observation()` into
`QuotaWindowStore`. That is an explicit observer on a timer; it never starts a
model turn and is independent of whether a Claude session is active.

**Codex (turn side-effect only).**
`src/backends/codex_native.py:987` emits
`consume_token_count({"rate_limits": client.rate_limits, ...})` when the
app-server sends `thread/tokenUsage/updated`. That event becomes
`model.session_usage`. `src/control/home_status.py:93 codex_usage()` reads those
events with `event_name='model.session_usage'` and `event_time >= now-15min`
(bounded to 2000 rows and 16 KiB), so the windows exist only as a side-effect of
an active turn and expire 15 minutes later. There was no Codex equivalent of the
worker's timer loop.

**The single choke point.**
Storage and reads are unified: both paths land in `QuotaWindowStore`, the
coordinator, `/api/quota-windows`, and the Home adapter. Acquisition was not.
`build_default_quota_adapters()` carried a silent
`UnsupportedQuotaAdapter("codex", "codex_quota_telemetry_not_validated_phase1")`
placeholder, and nothing tied the backend registry to a quota-capability
decision. A backend could therefore be fully supported for turns while its quota
telemetry was an untested placeholder, with no failing test. Three independent
Claude-only assumptions reinforced it:

1. `src/worker/config.py` enabled quota observation only when `"claude"` was in
   the worker's backends.
2. the worker observation body hardcoded `provider: "claude"`.
3. controller ingest only accepted the Claude payload shape.

The fix is not "make Codex look like Claude." It is to make provider coverage an
explicit, testable contract and give Codex its own native observer.

## 3. Goals and non-goals

Goals:

- Codex 5-hour and 7-day plan windows are observed on a timer, including while
  no turn is running, without sending a model request.
- Claude behaviour is unchanged.
- A provider can never be silently omitted from quota telemetry: it is either
  observable or explicitly unsupported with a reason.
- Unavailable/partial data stays honest: no fabricated zero, no overwriting the
  last good snapshot with a transient error.
- Both providers use the existing store, coordinator, API, and Home model.

Non-goals:

- Estimating Codex quota from token counts, local logs, or screen scraping.
- Supporting API-key usage billing as if it were a plan window.
- Adding a second quota store, a new migration, or a controller-side Codex
  process.
- Changing Claude's adapter or the Home layout.

## 4. Design

### 4.1 One provider-capability registry

`src/services/quota_provider_registry.py` is the single source of truth:

- `CLAUDE_OBSERVER = "claude_get_usage"` and
  `CODEX_OBSERVER = "codex_account_rate_limits"` name the observer kind.
- `QUOTA_PROVIDER_SPECS` maps each provider to either an observable observer kind
  or an explicit `unsupported_reason`.
- `BACKEND_TO_QUOTA_PROVIDER` maps every registered backend name (including
  `opencode-server -> opencode`).
- `coverage_gaps(backends)` returns any backend with no quota decision.
- `observable_providers_for_backends(backends)` is what the worker enables.
- `local_observable_providers()` is what a local-execution process may observe
  without a worker.

`tests/test_quota_provider_coverage.py` walks the backend registry and fails if a
backend has no quota decision, or if an observable spec names an observer kind
outside `SUPPORTED_OBSERVERS`. That is the control that was missing.

### 4.2 Worker observes each provider independently

`src/worker/config.py` now enables quota observation when any of the node's
backends maps to an observable provider. `WORKER_QUOTA_OBSERVE` still forces it
on or off per node; `QUOTA_COORDINATOR_ENABLED` and
`QUOTA_OBSERVE_INTERVAL_SEC` keep their existing meaning.

`src/worker/agent.py`:

- `_quota_observation_providers()` logs `quota_provider_coverage_missing` for any
  unmapped backend and returns the observable providers.
- `_observe_quota_once()` loops providers and isolates failures: one provider's
  exception posts an error observation for that provider and does not suppress
  the other.
- `_build_claude_quota_observation()` preserves the previous Claude body.
- `_build_codex_quota_observation()` reads Codex account rate limits and sends
  `provider: "codex"` with a `limits` object.

### 4.3 Codex observer — account metadata, not a turn

`src/backends/codex_app_server.py` adds
`CodexAppServerClient.read_rate_limits(timeout=5.0)`. It sends the app-server
request `account/rateLimits/read` on the existing client/runtime and normalizes
the result through the same allowlist as turn telemetry
(`src/core/telemetry_adapters/codex.py: native_rate_limits`). It does not start a
thread or send `turn/start`.

`src/backends/codex_native.py` exposes
`CodexBackend.read_account_rate_limits(timeout=5.0)`, which reuses `_runtime()`
and therefore the worker's owned app-server. If Codex is not runnable it raises;
the worker ships an explicit error observation instead of a fake window.

Mapping in `src/services/quota_window_coordinator.py`:

| Codex bucket | Window | Duration |
|---|---|---|
| `primary` | `codex_five_hour` | `window_minutes == 300` → 18,000 s |
| `secondary` | `codex_seven_day` | `window_minutes == 10,080` → 604,800 s |

The provider reports **used** percent. The store keeps `used_percent`; the UI
derives "remaining". A bucket that is absent stays absent — it is not a zero.

### 4.4 Controller ingest is provider-neutral

`QuotaObservationPayload` gains an optional `limits` object. The endpoint and
`ingest_worker_quota_observation()` dispatch through the registry:

- declared observable provider → its implementation;
- explicitly unsupported provider → recorded as `unsupported_provider` with the
  declared reason;
- undeclared provider → `quota_capability_not_declared`, visibly ignored.

The controller still never spawns a provider. Codex ingest validates the
allowlisted payload, maps known buckets, and persists through
`QuotaWindowCoordinator.observe_once()` with no local adapter list. On an error
or missing payload the adapter is marked unavailable; the last good snapshots
are left intact. A partial result (one known bucket) is `PARTIAL`, not a zero and
not a ready two-window state.

`build_default_quota_adapters()` deliberately omits Codex. A local controller
placeholder would overwrite the worker-ingested Codex status on every local
observe cycle. Codex is observed by the worker and ingested; the local adapter
list only contains locally observable providers plus explicit unsupported ones.

### 4.5 Home precedence

`web/src/lib/homeUsage.ts` builds coordinator windows from numeric
`window_states` / `latest_snapshots` first. `observed_usage` is a fallback for
the same provider and label, and an unknown coordinator state does not suppress a
fresh turn observation. This keeps the 15-minute turn event useful while the
coordinator has no value, and prevents an old event from replacing a numeric
coordinator window.

## 5. Adversarial review

- **RPC contention with an active turn.** `account/rateLimits/read` shares the
  owned app-server with turns. Mitigation: one request per provider per interval,
  5 s timeout, errors isolated; no new process. If the app-server is busy the
  observation is unavailable, not a fabricated window. Accepted residual risk;
  observe the worker for latency before enabling on a heavily used Codex node.
- **Partial windows.** Codex can report primary without secondary. Mitigation:
  map only present buckets, persist `PARTIAL`, show the known window and an
  unknown for the other. Do not copy a previous value into a current one.
- **Stale reset.** A window whose `resets_at` is already in the past is marked
  unavailable (`reset_at_stale`) rather than displaying a percentage that no
  longer applies.
- **Multi-account / principal ambiguity.** Codex observation is tied to the
  worker's runtime account. `CODEX_QUOTA_PRINCIPAL_KEY` exists so a worker can
  label that account; it defaults empty. Until populated, the UI must not claim
  the identity is authoritative. A future multi-account rollout needs one
  principal per observed runtime.
- **Rollout masking.** The old turn-event fallback could make a working
  coordinator look healthy, or hide a broken one behind 15-minute data.
  Mitigation: coordinator numeric values win; adapter status and reason remain
  visible in `/api/quota-windows`; the coverage test is the structural guard.
  The 2026-11-18 adversarial review reproduced stale numeric coordinator data
  suppressing a fresh turn reading. Only current Codex coordinator numbers now
  take precedence; stale/partial/expired readings become unknown and permit the
  fresh fallback. Claude's display behavior is preserved. Worker-only providers
  use the existing 15-minute age bound, independently of the controller's
  adaptive local-observer schedule; reading status cannot renew their freshness.
- **No-overwrite on error.** A transient app-server failure must not look like
  "no quota". Mitigation: error observations update adapter status and do not
  overwrite the last good snapshots.
- **Unsupported ≠ absent.** OpenCode remains explicitly unsupported. The
  registry makes that a declared reason, not an empty provider list.
- **Payload trust.** The controller accepts only the allowlisted Codex shape;
  unknown fields are dropped, malformed windows reject, and the provider string
  must be declared.
  Counters and durations use strict numeric types; reset timestamps are bounded
  to the representable UTC range. Invalid input preserves the last good rows.

### 5.1 Ingest service boundary (2026-11-18)

- **Concurrency / memory:** four permits cover body reading, parsing and actual
  database completion. Excess concurrent requests get 429. Cancelling the HTTP
  waiter cannot release a running mutation's permit. At 100 concurrent calls,
  at most four execute; admitted raw body buffers total at most 64 KiB before
  parser, ASGI and native SQLite overhead. Regression tests exercise 100 refused
  calls while a cancelled waiter's mutation retains its permit.
- **Size / timeout:** 16 KiB streamed body cap (including chunked requests),
  five-second total body deadline, fixed small response, worker HTTP socket
  timeout 15 seconds. Codex metadata RPC timeout is five seconds; lazy runtime
  initialization retains the existing bounded startup/retry lifecycle and is
  outside that individual RPC deadline.
- **Malformed input:** invalid JSON/schema returns 422; invalid Codex windows
  return `accepted=false` / `invalid_limits` without replacing snapshots.
- **Resource failure:** SQLite/filesystem errors return 503 per request. Database
  work runs off the event loop, with existing five-second SQLite busy waits.
- **Deferred:** no hard native database/COMMIT or Python write-lock deadline,
  exact native RSS bound, or per-node account authentication is claimed. Four
  permits remain held through actual completion, containing stalled work. Retain
  the existing trusted-worker/shared-token assumption; A71 owns node credential
  isolation. Worker loss is handled by snapshot expiry, not process restarts.

## 6. Implementation map

| File | Change |
|---|---|
| `src/services/quota_provider_registry.py` | new coverage registry |
| `src/backends/codex_app_server.py` | `read_rate_limits()` (`account/rateLimits/read`) |
| `src/backends/codex_native.py` | `read_account_rate_limits()` reusing `_runtime()` |
| `src/worker/config.py` | registry-derived observation enablement |
| `src/worker/agent.py` | per-provider loop, Claude/Codex bodies, error isolation |
| `src/control/task_server.py` | `limits` payload + provider-neutral ingest dispatch |
| `src/services/quota_window_coordinator.py` | Codex adapter, ingest dispatch, local adapter list |
| `config/settings.py` | `QuotaConfig.codex_principal_key` |
| `web/src/lib/homeUsage.ts` | numeric coordinator precedence |
| `docs/backend/QUOTA_WINDOW_COORDINATOR_PHASE1.md` | as-built note |
| `docs/INDEX.md` | catalog row |

## 7. Tests

Targeted only; no full suite, no e2e, no paid provider turn.

- `tests/test_quota_provider_coverage.py` — every backend has a quota decision;
  observer kinds are declared.
- `tests/test_worker_quota_observation.py` — Codex body shape, provider order,
  failure isolation, no Claude body regression.
- `tests/test_quota_window_coordinator.py` — Codex mapping, partial/unknown,
  stale reset, successful ingest, error does not overwrite, undeclared and
  unsupported providers, no local Codex adapter.
- `tests/test_codex_app_server.py` — `read_rate_limits()` allowlisted shape.
  A timed-out metadata request cannot poison the shared runtime or its next RPC;
  late replies are drained and all fake processes/reader threads are cleaned up.
- `tests/test_codex_native.py` — `read_account_rate_limits()` reuses the runtime
  without a turn.
- `tests/test_docker_boundary.py` — Codex-only enables observation; an
  unsupported-only node does not.
- `web/src/lib/homeUsage.test.ts` — coordinator beats turn event; unknown does
  not.
- Existing `tests/test_control_api.py` and Home screen tests remain green.
- `tests/test_quota_observation_boundary.py` — authenticated HTTP to real quota
  persistence, partial windows, invalid/chunked/slow bodies, dependency failure,
  cancellation and retained concurrency permits. No provider process is spawned.

Review verification on 2026-11-18 (Python 3.12.16, current main integrated):

```bash
.review-venv/bin/python -m pytest tests/test_quota_provider_coverage.py tests/test_worker_quota_observation.py tests/test_quota_window_coordinator.py tests/test_quota_observation_boundary.py tests/test_codex_app_server.py tests/test_codex_native.py tests/test_docker_boundary.py tests/test_control_api.py tests/test_codex_managed_turns.py tests/test_codex_managed_carrier_integration.py tests/test_worker_managed_turns_default.py tests/test_a102_carrier_contract.py tests/test_backend_activity.py tests/test_model_picker.py tests/test_turn_queue_sender.py --tb=short -n 4
# 351 passed, 1 skipped in 73.74s
.review-venv/bin/python -m pytest tests/test_quota_observation_boundary.py tests/test_quota_window_coordinator.py --tb=short
# 81 passed in 27.83s after the final HTTP-schema preservation change
pnpm --dir web exec vitest run src/lib/homeUsage.test.ts src/screens/HomeScreen.test.tsx
# 24 passed
pnpm --dir web build
# TypeScript check and production build passed
```

The isolated review environment used the declared `dev,test,dispatch` extras and
`constraints.txt`; `pip check` passed. Sandbox asyncio thread wakeups stalled, so
Python results above were obtained outside the sandbox with bounded runner
lifetimes. No paid turn or e2e suite was run. Ruff passes for the new provider
registry and HTTP-boundary tests; the legacy coordinator/task-server modules
retain existing lint debt and are not claimed lint-clean.

The installed Codex CLI 0.169.0 generated the matching account-rate-limit JSON
schema. A real metadata-only `CodexBackend.read_account_rate_limits()` probe
returned primary 300-minute and secondary 10,080-minute windows. Its isolated
owned app-server and reader threads were verified closed afterward; model turns
sent: zero. This proves the native read, not scheduled worker activation or
natural production refresh after deployment.

Final main refresh: integrated `8fdf5a0` (A117 telemetry delivery) without source
conflicts or changes to its telemetry/session economics code. The same targeted
pytest command with `-n 2` passed **355 tests, 1 skipped in 70.06s**; Home tests
still passed 24/24 and the typecheck/production build passed. During an earlier
parallel build/four-worker run, the unchanged concurrent SQLite test exceeded its
ten-second wait (354 passed, 1 failed, 1 skipped). Clean main's quota module
passed 63/63; the full final two-worker selection passed without changing that
test or relaxing its timeout. This is recorded as an observed intermittent wait
timeout, not asserted to be a pre-existing failure.

PR #288 closed on 2026-11-18 after both CI jobs passed on final head `eea01d0`
(run `44296899146`, including the isolated native-control smoke). Controllers
were rebuilt/recreated from merge `8a026a2`; both report healthy, schema 49 is
unchanged, and both workers reconnected. Authenticated `/api/quota-windows`
returns 200 with observation enabled. Live ingest rejects missing authorization
with 401, invalid JSON with 422 and a 16,385-byte body with 413. No accepted test
observation was sent. Codex's persisted adapter remains unavailable until a
promoted worker supplies observations; natural timer-driven refresh is not
claimed. No worker was restarted by this review. Rollback image:
`ai-team:pre-8a026a2`; verified deploy tag: `deploy/20261118T1213Z-8a026a2`.

## 8. Acceptance criteria

- With `QUOTA_COORDINATOR_ENABLED=1` and a Codex-capable worker, Home shows
  Codex 5-hour and 7-day windows without an active Codex turn, refreshing on the
  observation interval.
- With Claude only, the Claude panels and observation body are unchanged.
- Removing Codex from the registry makes the coverage test fail; adding a new
  backend without a quota decision makes it fail.
- A Codex read error shows unavailable and leaves the previous good snapshot in
  place.
- Missing secondary shows one window and an honest unknown, not `0%`.
- `/api/quota-windows` exposes the Codex window states and the adapter reason.
- No controller-side provider process is spawned by ingest.

## 9. Open questions

- Does every supported Codex CLI/app-server version expose
  `account/rateLimits/read`, and are `primary`/`secondary` always 300/10,080
  minutes? The adapter rejects other durations rather than guessing.
- Should a future multi-account Codex node observe each account separately
  (requires a principal per runtime), or is one account per worker the contract?
- Should the 15-minute `model.session_usage` fallback be removed once the
  coordinator has run for a full release cycle? It is retained now as a
  low-risk bridge.
