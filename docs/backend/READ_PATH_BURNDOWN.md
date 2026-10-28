# Read-path engineering burndown

Programme accepted by the operator on 2026-10-28. This is the durable detailed
queue; `.ai/CONTEXT.md` links here for forward priority. The original temporary
proposal is an input, not the backlog of record. Evidence baseline: main
`4f792cd`, independent review and source revalidation on 2026-10-28.

Statuses: TODO / IN PROGRESS / VERIFIED / BLOCKED / DEFERRED. VERIFIED means the
named layer has evidence, not that it is merged or running in production.
Implementation, merge and production verification are recorded separately.

## Ordered mandatory jobs

### J1 — Jobs authority and enrichment

- Priority: P1, execute first. Status: VERIFIED (implementation, merge and production).
- Classification: confirmed defects; compatibility change explicitly accepted.
- Evidence: `TaskOrchestrator.list_watched_jobs` always merges local and remote
  reads; `_normalize_job_ownership` repeats session lookups for both arrays.
  `MeshDB.list_jobs` already computes SQLite ownership and filters before LIMIT.
  Commit `41e34f8` intentionally added remote gateway compatibility; it is not dead
  code. Compose gateway/task-server mount the same controller database.
- Affected behavior: jobs latency, redundant HTTP timeouts, per-job SQL, empty
  versus unavailable results, completion-fetch event-loop stalls.
- Dependencies: verified main; no worker restart. Scope: explicit controller mode
  by default and explicitly selected federated compatibility mode, one shared
  local running/recent composition, controller SQL ownership, structured API 503,
  bounded HTTP concurrency, serial offloaded completion reads. No delivery redesign.
- Contract: recent includes every status, running may also appear in recent;
  per-array limits and session/ownership filters stay intact. Controller ownership
  means existence in SQLite, including closed sessions. JSON-only sessions are
  orphaned here but remain recoverable through existing session fallback readers.
  Empty/whitespace IDs follow the existing SQL predicate, not Python stripping.
  Compatibility mode retains gateway-relative reconciliation and legacy union.
- Acceptance: real SQLite tracing proves two data SELECTs for 1/20/50 jobs, zero
  controller HTTP/session loads, including healthy-empty. Validate ownership edge
  cases and filtering before LIMIT; both API paths return 503 on DB failure.
  Existing union/cache/reconciliation tests pass under explicit compatibility mode.
  Blocked completion reads do not block a heartbeat or overlap another poll;
  connections are obtained in the fetching thread. Excess HTTP requests fail before
  DB work and permits release on success/error. Measure plans, bytes, memory and
  repeatable local latency; distinguish fixture evidence from production evidence.
- Implementation/verification: local checks passed; full evidence below.
- PR: [#261](https://github.com/nydiokar/AI-team/pull/261); implementation commit
  `1d23442` (base `4f792cd`). Implementation CI passed (run `40691307468`).
- Merge: authorized by the operator on 2026-10-28; PR #261 merged as `e0e2877`.
- Production: VERIFIED `e0e2877`, image `bb712a2a5647`, tag `prod-e0e2877`;
  deployment evidence and rollback below. No workers restarted.

### J2 — Completion reliability

- Priority: P1, NEXT MUST-FIX. Status: TODO. Depends on J1's selected-source seam.
- Classification: code-established correctness risks; deterministic reproductions
  required before selecting a fix.
- Evidence: `_job_completion_poller` advances `_last_job_poll` after SELECT;
  `complete_job`/`fail_job` compute timestamps before `_write` obtains its lock.
  Capturing the boundary before SELECT alone therefore still misses delayed commits.
  `_process_terminal_job` adds to `_processed_terminal_jobs` before notification,
  session lookup or admission succeeds. Initial watermarks are process-start times.
- Affected behavior: missing completions, notification failures suppressed until
  restart, uncertain restart recovery. Remote polling also reads only the newest
  50 jobs and filters on start time; evaluate these as part of the source contract.
- Scope: reproduce each failure deterministically; define terminal discovery,
  retry/recovery, notification versus continuation idempotency, permanent failure
  and restart semantics before implementation. Reuse existing durable authority;
  no speculative broker, second inbox or exactly-once claim.
- Acceptance: force a writer to timestamp before a poll and commit after it;
  prove eventual discovery. Inject failures before/after notification/admission,
  retry and restart at each boundary, prove the declared duplicate/loss behavior.
  Test >50 remote completions and jobs started before gateway startup; establish
  disposition with evidence if outside the supported recovery contract. Do not
  equate in-memory deduplication with durable delivery. If a risk is disproven,
  record the reproducer and reason instead of implementing a speculative fix.
- PR/commit/tests: none. Merge/production: not implemented / not verified.

### J3 — Session-reason N+1 and running-job truncation

- Priority: P1, MUST-FIX after J2. Status: TODO. Depends on J2 closure and fresh main.
- Classification: confirmed defects. `build_reason_batch` does per-case
  `get_flow_run`, `case_quota_pause`, `transient_pause`; its N+1 test permits 30
  per-case calls for ten cases. `list_jobs_for_sessions` limits all statuses to 200
  before the reason layer selects running jobs.
- Affected behavior: page cost grows per distinct waiting case; old running jobs
  can disappear from waiting reasons.
- Scope: deduplicated, 500-ID chunks of case status and independent latest relevant
  quota/transient event types; dedicated running-session existence query. Preserve
  pause helper payloads and event definitions, inbox authority, reason precedence,
  live-task evidence, confidence/detail, queue overlays and single-case callers.
- Acceptance: five reason-evidence data SELECTs within one chunk (jobs, two inbox,
  live tasks, case evidence); seven including base page and enrolled queue overlay.
  Counts constant at 1/20/200 cases, growing only by chunks above 500. Full output
  equivalence for shared/missing/closed/cancelled/blocked/null-status cases, every
  primary session state, independent pause/resume/decline/exhaustion/respawn events,
  quota-over-retry precedence and event-ID ordering despite reversed timestamps.
  An old running job survives >200 newer completed jobs. No full event-log folds.
- PR/commit/tests: none. Merge/production: not implemented / not verified.

### J4 — Session-list projection and indexing

- Priority: P2, REQUIRED after J3. Status: TODO. Depends on J3 and revalidation.
- Classification: confirmed overfetch; index benefit is a measured hypothesis,
  not an established production latency promise.
- Evidence: `MeshDB.list_sessions` selects full rows ordered by `updated_at`;
  `SessionStore.list_all` parses mutable Sessions including history/messages.
  `SessionService.list_views` only needs view/reason/overlay fields. Existing
  server limit is real: CONTEXT's old claim that it is client-only is stale.
- Scope: dedicated typed read-only projection with explicit columns; shared pure
  view conversion, retain full lifecycle readers and JSON fallback. Add numbered,
  additive indexes only after EXPLAIN on a representative copy for unfiltered and
  `COALESCE(keep_pinned,0)=?` shapes. Preserve null semantics and ordering; document
  any tie-breaker change rather than silently changing visible order.
- Acceptance: no history/message blobs in list SQL; complete SessionView equality
  including configured model, origin defaults, last result/previous backend ID,
  reasons and queue overlay. Full get/save/list_all preserve history; fallback
  matches existing empty/error/malformed-row behavior. Pinned sessions older than
  the newest 200 remain retrievable (UI pinned limit 1000). Fresh/existing migrations
  pass; all three query shapes avoid an unnecessary sort with measured query plans.
- PR/commit/tests: none. Merge/production: not implemented / not verified.

### J5 — Compact affiliations

- Priority: P2, REQUIRED after J4. Status: TODO. Depends on J4 and contract revalidation.
- Classification: confirmed redundant objective payload; compact transport required.
- Evidence: `list_session_case_links` joins full objectives before builder dedup;
  `build_session_affiliations` emits objective per session. Sessions, Session Detail
  and Cost share `useSessionAffiliations`. That hook exposes no error state and
  defaults to an empty map: error/unknown can appear standalone. This is pre-existing.
- Scope: explicit compact option, legacy default preserved; emit IDs, normalized
  role, case status and equivalent bounded title, no raw objective. Compute once
  per case. Preserve newest-link semantics (including closed/missing cases and
  LEFT JOIN), complete membership, all three consumers and loading/error honesty.
  Update response docs/types/client/adapter together and retain prefix invalidation.
- Acceptance: identical complete mapping beyond 100/200 cases, newest closed link
  wins over older open link, unknown case survives. Common Python/TS golden fixtures
  for tag priority, whitespace, markup, fallback IDs and Unicode truncation using
  current JS UTF-16 semantics (`slice(0,117) + ellipsis`). Equivalent visible titles;
  detail retains objective. Legacy clients pass; loading/error never asserts known
  standalone membership. Versioned format key retains event invalidation. Note:
  affiliations are NOT persisted (`main.tsx` only persists session queries).
  Measure peak intermediate memory/concurrency and query plan. Historical target
  <750 KB for the same 1,082-entry dataset is unverified, not a unit-test timing gate.
- PR/commit/tests: none. Merge/production: not implemented / not verified.

## Separately evaluated improvements and compatibility decisions

- S1 (P3, DEFERRED, optional scalability): scoped affiliation retrieval/cursor
  pagination, after J5. Whole-index growth remains unbounded; evaluate workload and
  all three consumers before choosing an API. Acceptance for promotion: bounded
  per-request memory/output and complete requested membership, with unknown/error
  distinguished from absent. No arbitrary global cap. No PR/tests yet.
- S2 (P3, DEFERRED, compatibility decision): retirement of federation. Historical
  intent is proven; current deployment dependence is not. J1 retains an explicit
  mode. Retirement requires operator decision and deployment inventory; acceptance
  is migration of dependent gateways without losing visibility/continuations.
- S3 (P2, DEFERRED to boundary sizing): hard SQLite execution deadlines and absolute
  response-byte bounds. Existing busy timeout (15 s) bounds lock waits, not query
  execution; cancelling an await does not cancel its thread. Row limits do not bound
  historical text. J1 must bound concurrent materialization and record measured
  memory; evaluate absolute byte caps separately because truncating fields changes
  the API. Acceptance: explicit deadline/cancellation and oversized-history contract
  with deterministic tests, or concrete workload-based documented deferral.
- S4 (P3, DEFERRED, optional optimization hypothesis): jobs ordering/status indexes.
  J1 EXPLAIN on the 94-job production copy shows `SCAN jobs` and a temporary ordering
  B-tree; ownership checks use the session primary-key covering index. Application
  N+1 removal does not eliminate this scan. No index migration is justified by the
  current copy's sub-3 ms median reads. Re-evaluate with materially larger jobs
  cardinality or observed local read latency; acceptance for promotion is measured
  benefit across filtered/unfiltered shapes without changing order/ownership.

Historical timings (4 s jobs, 172–179 ms sessions, 5.08 MB affiliations) came from
small-sample probes, not p95s. Reproduce before using them as performance evidence.
No telemetry split, cache, ORM, global endpoint framework, worker changes or polling
cadence changes are part of this programme.

## J1 verification and service boundary record

Implementation uses `jobs_read_model.list_controller_jobs` from the orchestrator
and monitoring fallback; MeshDB owns ownership/filtering. No schema, worker,
session lifecycle, pause/inbox, task-server API or notification changes. Missing
local DB no longer prevents an explicitly selected remote completion fetch.

Tests-first baseline: 11 selected regressions failed before implementation
(unexpected remote reads, accepted invalid config, successful empty/failing DB).
Focused implementation tests then passed; final command/results recorded below.

### Read measurements (2026-10-28, offline copy)

Opened the live controller SQLite source with `mode=ro` and used SQLite backup
to an isolated `/tmp` copy (313,176,064 bytes, 94 jobs, 2,109 sessions). All benchmark
mutations, including MeshDB initialization and large-tail stress, used that copy.
Baseline is `list_watched_jobs` extracted from `4f792cd`, with its remote cache
stubbed empty: this intentionally compares local work, not network timeout cost.
New controller reads used the same copy/connection. Warmed read + JSONResponse
serialization, 30 samples per shape, no hardware-sensitive test thresholds:

| Shape | Baseline SELECTs → controller | Baseline median ms | Controller min / median / max ms | JSON bytes |
|---|---|---|---|---|
| all, limit 20 | 22 → 2 | 4.601 | 1.180 / 1.192 / 1.260 | 33,297 |
| unowned, limit 20 | 42 → 2 | 1.932 | 1.183 / 1.196 / 1.370 | 39,017 |
| all, limit 50 | 79 → 2 | 7.731 | 2.418 / 2.440 / 2.575 | 86,123 |
| unowned, limit 50 | 98 → 2 | 4.321 | 2.730 / 2.884 / 13.081 | 96,594 |

Complete response equality held for all four shapes. The copied dataset had zero
running jobs; dedicated fixtures test running/recent overlap and mixed statuses.
Real SQLite trace tests cover 0/1/20/50 returned running jobs with exactly two
SELECTs, no SessionStore or per-job get_session and no HTTP even with CONTROLLER_URL.
EXPLAIN for each running/recent, all/unowned shape: jobs scan + temporary ORDER BY
B-tree; ownership uses `sqlite_autoindex_sessions_1` covering primary-key lookups.
These are indexed correlated subqueries within two statements, not constant total
SQL work. Jobs indexing remains optional S4, not silently included in J1.

### Explicit service-boundary checklist

- Concurrency: four active `/api/jobs` handlers per gateway process, semaphore
  held through materialization AND JSONResponse serialization. Test blocks four
  serializers, submits 96 additional reads (all 429 before DB work), then releases
  the four; success and SQLite error paths release permits. Authentication and
  validation precede DB work. Background polls await each fetch serially in the
  existing executor; no new executor, polling interval or overlapping fetch loop.
- Memory at N=100: maximum four active materializations plus 96 small 429 responses,
  rather than 100 full responses. Controller traced incremental Python peak was
  658,049 bytes for the largest normal shape (~2.63 MB for four). On the copy,
  setting each historical tail to 64 KiB yielded a 3,342,873-byte response and
  20,125,459-byte traced peak (~80.5 MB for four). A separate stress on the same
  copy marked jobs running, filling BOTH arrays (50 + 50 entries): 6,685,956-byte
  JSON and 40,265,456-byte traced peak (~161.1 MB for four). This covers the
  overlapping-arrays case absent from the sampled live data. These are active-memory estimates,
  not measured concurrent RSS; tracemalloc excludes SQLite/native/page-cache memory.
  MeshDB config allows an 8 MiB page cache per thread-local connection; existing
  threadpool connections can remain warm after a request. No absolute byte bound
  on historical text is claimed. S3 remains explicitly deferred: truncating existing
  fields would change this response contract; choose byte limits with a separate
  oversized-history compatibility contract before broad untrusted/high-volume use.
- Request size: GET consumes no body. Existing limit 1–50, session ID max 128 and
  ownership vocabulary/conflict checks apply. Malformed/oversized parameter tests
  reject before invoking the reader; authentication remains required.
- Timeout: local SQLite busy timeout remains 15 s for lock contention, not query
  execution. No hard SQL deadline; explicitly deferred S3 as above. Compatibility
  remote client retains its 2 s per-socket-operation timeout, not a total request
  deadline. Cancelling a polling await cannot stop a running thread; existing
  executor shutdown may wait for the synchronous read. No cancellation/retry or
  completion guarantee is introduced by this PR.
- Malformed input: 422 for invalid limit/ownership/ID length and 400 for conflicting
  session/unowned selectors, existing auth denial. No DB details in structured errors.
- Backing resources: controller `get_db() is None` or SQLite failure yields 503
  `jobs_unavailable` through facade and fallback; healthy-empty remains 200. In
  explicit federation mode, missing facade yields 503 instead of switching source;
  legacy remote failures still produce partial/empty or stale cached responses
  because TaskServerClient conflates failure and empty. Compatibility behavior is
  retained, not presented as newly reliable federation. Missing mode credentials
  fail configuration loading. DB initialization remains lazy; no new startup DB probe.

Event-loop tests block a fake fetch while an independent heartbeat advances, assert
DB acquisition and local/remote fetches run off-loop, preserve serial processing,
and test a real SQLite connection differs between loop and fetching thread.
Existing compatibility tests retain union/dedupe, scoping, ownership reconciliation
and stale-cache behavior. Real JSON-only files demonstrate the intentional ownership
difference between modes. Completion watermark/processed-before-notify risks remain
open in J2; no exactly-once, restart recovery or successful delivery claim.

### Commands and verification results

From the feature worktree, using the repository virtual environment:

```bash
/home/cifran/dev/AI-team/.venv/bin/pytest tests/test_jobs_read_authority.py tests/test_watched_jobs.py tests/test_control_api_write.py tests/test_settings_env_file.py tests/test_turn_queue_4d.py tests/test_docker_runtime_config.py tests/test_claude_auth_api.py --tb=short
```

**184 passed, 2 warnings in 27.61 s.** Warnings are the existing Starlette/httpx and
AnyIO API deprecations. No paid backend or local full/e2e suite was run. New tests
cover the controller helper despite the selector not recognizing its new filename;
control API, watched jobs and managed producer targets exercise the actual seams.

`ruff check src/control/jobs_read_model.py tests/test_jobs_read_authority.py` and
`git diff --check` pass. Existing modified files contain lint debt; comparison by
diagnostic code/message against `4f792cd` found no introduced diagnostics. No broad
autoformat/refactor. No web changes, so web tests/build are not part of J1.

Production baseline before the authorized deployment was `4f792cd`.

### Authorized J1 deployment and production verification — 2026-10-28

- PR #261 reviewed against unchanged main `4f792cd`; final head `6f05218` CI passed
  ([run 40691972065](https://github.com/nydiokar/AI-team/actions/runs/40691972065)).
  The same 184 acceptance tests passed again in 26.09 s (two existing warnings).
- Merge SHA and deployment SHA: `e0e2877`. Main checkout used for build:
  `.worktrees/login-ux`, clean tracked files, `.env` already symlinked to the host
  env. Unrelated untracked ctags temporary files were preserved and excluded from
  the Docker allowlist. Deployment preflight: GO, schema 46 → 46, no migrations.
- Online SQLite backup:
  `/home/cifran/ai-team-data/backups/mesh-pre-e0e2877-20261028T1332Z.db`, mode 0600,
  `PRAGMA integrity_check = ok`. Rollback image `ai-team:pre-e0e2877` retains old
  image ID `85b1900cf3d9` / revision `4f792cd`; no rollback or data restore used.
- Built shared image `bb712a2a5647`, recreated only `gateway` + `task-server`.
  Both healthy, both report `build.git_sha=e0e2877`; gateway coverage true,
  task-server schema 46, two nodes online, no stale/pending/claimed tasks at probe,
  no gateway/task-server tracebacks. First immediate startup curl reset before
  readiness; subsequent repeated checks healthy. Production image tagged
  `ai-team:prod-e0e2877`; pushed annotated git deploy tag
  `deploy/20261028T1332Z-e0e2877` (preflight timestamp).
- Native worker unchanged: `ai-team-worker-kanebra`, PID 2576526, restart count 4,
  online before and after. No worker or unrelated service restart command used.
- Read deployed config inside gateway: `jobs_read_mode=controller`. Compose selects
  it explicitly; mode is not inferred from CONTROLLER_URL, empty rows or failures.
- Real Chromium UI loaded `/system`, authenticated through the normal localStorage
  credential flow (loopback correctly shows TokenGate without a supplied token).
  Populated jobs: 200, zero running / 20 recent; first actual job row rendered.
  Empty-state check forwards only that browser's jobs URL to the REAL API with a
  nonexistent session filter; response 200 with both arrays empty, UI rendered
  `No watched jobs.`. No mocked response body, DB write, paid turn or DB-failure
  simulation. Screenshots/report retained under `/tmp/ai-team-j1-ui-*.png` and
  `/tmp/ai-team-j1-live-ui.json` (not repository artifacts).
- Browser-authenticated real API probes: 10 samples each, all 200. Second run:
  all jobs limit 20: 16.6–65.0 ms; unowned limit 50: 10.7–21.0 ms; nonexistent
  session: 6.3–18.3 ms. Cold first run had a 240.5 ms maximum; these are sample
  ranges, not p95s or latency guarantees. Zero browser page errors.
- Tooling limitation: browser assertions and JSON/screenshots completed, then the
  host Node process aborted during teardown (exit 134), reproducible with installed
  Node 24 and Node 22. No browser child processes remained. This does not establish
  a clean browser-runner exit; it is recorded rather than presented as a green
  browser command. UI assertions/API responses themselves passed on both runs.
- During populated/empty UI reads, 30 API probes and a background poll interval,
  a 40 s capture in the live gateway network namespace recorded ZERO TCP SYNs to
  task-server port 9002, zero dropped packets. Task-server access log recorded zero
  `GET /jobs` calls. Together with deployed mode/code and offline zero-HTTP tests,
  this verifies removal of redundant remote jobs reads for the observed window;
  it does not claim every gateway operation is forbidden from contacting that port.
- Structured 503 and contention/malformed-input behavior remain covered by isolated
  acceptance tests; production DB availability was not destructively manipulated.
  No running jobs existed at probe, so new completion reliability is not claimed.
  J2 and all S1–S4 limitations remain open; historical text/SQL deadlines unchanged.

Rollback procedure if needed: tag `ai-team:pre-e0e2877` back to `ai-team:local`,
then `docker compose up -d --no-build gateway task-server` from this main checkout.
No schema changed. Restoring the DB backup would discard later writes and still
requires a separate operator decision; it is not part of automatic code rollback.

### PR closure and next dispatch

J1 is VERIFIED, merged and deployed after explicit operator authorization.
Implementation CI passed:
[run 40691307468](https://github.com/nydiokar/AI-team/actions/runs/40691307468)
on `1d23442`, 3m24s. Subsequent commits only update programme evidence; current
head check status is available on PR #261. Production closure is recorded above.

Next independently executable dispatch: J2 completion reliability, based on the
approved/merged J1 selected-source seam. First commit deterministic reproductions
for late commits and notification/admission failures. Specify retries, duplicate
effects and startup/restart recovery before choosing a persistence change. Verify
restart boundaries, failed backing resources, repeated polls and permanent failure;
record disproven allegations with evidence. J3–J5 remain mandatory TODO in order.
