# Read-path engineering burndown

Programme accepted by the operator on 2026-10-28. This is the durable detailed
queue; `.ai/CONTEXT.md` links here for forward priority. The original temporary
proposal is an input, not the backlog of record. Evidence baseline: main
`4f792cd`, independent review and source revalidation on 2026-10-28.

Statuses: TODO / IN PROGRESS / VERIFIED / BLOCKED / DEFERRED. VERIFIED means the
named layer has evidence, not that it is merged or running in production.
Implementation, merge and production verification are recorded separately.

## Closure execution — 2026-10-31

Operator authorized implementation, CI/self-merge and gateway/task-server deployment;
no worker restart, historical replay or unnecessary paid workload. Mandatory work
is failure honesty and demonstrated recovery, not arbitrary pagination/index/cap changes.

| Gate | Acceptance / evidence owner | Execution state |
|---|---|---|
| 1 requirements | Original J1–J5 acceptance below + this record; exact deployed revision recorded at close | Baseline `bfd01d4`, live health `ee59596`; PRs #261–266 confirmed merged |
| 2 availability | Real SQLite failure tests → HTTP/client/cache → Sessions; reason failures cannot prove idle; recovery files retained | Implemented on `feat/read-availability-close`; six new regressions failed before correction |
| 3 completion recovery | Existing reliability/protocol tests plus isolated process death/restart around durable effects; natural production observation separate | Pending process acceptance / production observation |
| 4 resource envelope | Re-measure representative copy + mixed requests/RSS before promoting optional caps; execution cancellation tested if introduced | Existing measurements below are historical, not absolute guarantees |
| 5 affiliation growth | Complete newest/orphan/title semantics and all consumers; scoped API only if measurements justify promotion | Whole-index compact preserved; legacy now shares four admission slots |
| 6 consistency/compatibility | Concurrent-writer tests; no second inbox/executor; retain explicit federation and historical cutover | Pending closure verification |
| 7 ship/certify | Targeted tests, adversarial review, CI, self-merge, gateway/task-server deploy, authenticated functional observation | Pending; no full production certificate claimed |

Availability correction: `read_summaries` preserves JSON recovery and malformed-row
skip behavior, returning typed provenance. Healthy list JSON is unchanged; fallback
or partial lists carry `read_state` and remain available to recovery consumers.
The UI rejects them as current inventory, preserves React Query's failed state,
and offers Retry; generic exceptions now return sanitized 503 instead of empty 200.
Queue overlay failure also yields 503 on this API path. Failed reason evidence
marks affected waiting sessions `unavailable` (medium confidence), including failed
chunks without hiding successful chunks. Primary state and lifecycle readers stay
unchanged. Compact and default legacy affiliation success schemas stay unchanged;
both now reject unavailable DB (503) and excess reads (429), through serialization.

Service boundary: existing authenticated GET parameter limits remain; no body read,
new store, background producer or worker code. Existing four-slot gates cover the
new paths; missing/failed backing reads are explicit per-request states. Deadlines,
byte limits and native RSS remain subject to the measurement gate, not silently
certified. Self-review targets initial/cached-empty/cached-positive failure, malformed
rows, recovery, serialization exceptions, source failure and shared admission.

Availability checks: focused ten-module regression **298 passed, 8 existing
warnings** (38.52 s); final queue/API additions **16 passed, 2 existing warnings**
(4.79 s). Related frontend **143 passed** (15 files); production build passed.
Typecheck passed after correcting the test import. New real-query/client cache integration
check **1 passed** (1.46 s), exercising successful empty→fallback/partial error→
recovery through existing prefix invalidation. Final API/affiliation rerun **62
passed, 2 existing warnings** (11.98 s). No paid backend or full/e2e suite run.
Legacy lint debt compared by diagnostic code/message with `bfd01d4`; no new
findings retained. Deliberate self-review traced DB→service→API→client→cache→UI:
recovery records remain intact, unavailable evidence has no routing side effects,
and healthy success payloads/default legacy fields remain compatible.

### Snapshot, cancellation, process recovery and sizing closure

Availability PR [#267](https://github.com/nydiokar/AI-team/pull/267) merged
`61ba4fb`; CI run `41204034001` passed (3m26s). Deployment follows final code closure.

Four new snapshot regressions failed before implementation, including a committed
writer immediately before jobs' second SELECT yielding old/new rows in one response.
Controller jobs arrays and the HTTP session page/reason/queue overlays now share
one read snapshot. Compact retains its snapshot using the same scoped helper.
Legacy affiliations and completion pages also use it. No routing, admission or
write authority changes. The helper applies a 10-second monotonic SQLite VM
budget (progress check every 1,000 operations) and at most a 10-second per-lock
busy timeout. Ten seconds leaves headroom under the compact browser's 15-second
read timeout and is far above measured normal reads. It is **cooperative**, not a
hard native IO/Python/serialization/socket deadline. Nested reads inherit the
outer scope; handlers/timeouts/savepoints clear on errors, including interruption,
without contaminating cached connections or subsequent managed writes. Session
SQL failures within this scope do not turn cancellation into an unbudgeted JSON
scan. Every HTTP surface returns structured 503 on budget exhaustion and releases
admission; existing serialization/100-call tests remain applicable.

J2 fresh-interpreter tests terminate the isolated consumer with exit 73 before
discovery, after durable claim, after accepted notice and after admission commit.
Recovery opens the same real SQLite/session state in a new interpreter, executes
through the real scheduler/claim/start/complete seams, then replays in another
interpreter. Each yields one deterministic completed continuation and done effect
receipts. The actual NotificationService and TelegramInterface run against a
fsynced local transport sink: one notice normally, two after send-before-ack
uncertainty. Only test leases are advanced instead of waiting 120 seconds. Backend
spawn guards remain installed; subprocess.run owns/reaps every interpreter.
This proves process persistence, not a real Telegram receipt or production turn.

Fresh online backup measurements (2026-10-31, all mutations isolated): 2,120
sessions, 94 jobs, 5,676 links, **zero completion receipts**. Five samples including
JSON encoding: jobs limit 50 median 2.34 ms / 88,595 bytes; session views with
reasons/queues limit 1000 median 155.52 ms / 1,149,599 bytes; compact median 29.96 ms /
271,599 bytes; legacy median 54.62 ms / 5,148,201 bytes. These use stdlib default
JSON separators; compact live bytes use tighter separators. No p95/absolute bound
is inferred. Sixteen read/materialization threads (four jobs, four sessions, four
compact, four completions), bodies held simultaneously: 6,039,304 encoded bytes;
RSS 210,141,184→790,265,856 bytes, PSS 198,075,392→260,203,520 bytes. PSS accounts
for repeated mappings of the same SQLite file; do not describe the RSS difference
as private heap growth. Native allocations are included; allocator/page-cache
warmup and 100 slow readers are not certified. Available host memory was about
5.66 GB. Existing independent 100-request gate tests prove excess admission;
this experiment measures the aggregate at configured active capacity.

On the same isolated copy enlarged to **10,820 affiliations / 940 jobs**, complete
compact encoding was 2,685,396 bytes / 125.43 ms; jobs filled both 50-row arrays in
4.50 ms. Plans use `idx_flow_links_entity`, link primary keys and the case primary
key; newest-link ordering retains a temporary B-tree. These measurements do not
justify a new scoped API, global gate, byte truncation or jobs index migration.
S1/S4 remain optional; explicit federation is retained. Historical byte caps and
slow-reader/native IO guarantees remain outside the authenticated operator
workload certification; they are not re-labelled as implemented. Local measurement
artifacts `/tmp/read-closure-measure.json` and `/tmp/read-closure-scale.json` are
supplemental; the durable values and limitations are recorded here.

Snapshot/recovery verification:
`.venv/bin/pytest tests/test_read_snapshot.py tests/test_jobs_read_authority.py tests/test_watched_jobs.py tests/test_session_reason_batch.py tests/test_session_list_projection.py tests/test_session_affiliations.py tests/test_job_completion_process.py tests/test_job_completion_reliability.py tests/test_job_completion_protocol.py tests/test_control_api.py tests/test_control_api_write.py --tb=short -n 4`
— **350 passed, 8 existing warnings in 43.70 s**. New modules + jobs helper pass
Ruff; modified source adds no diagnostic code/message against `61ba4fb`.
Self-review covered WAL writer races, nested scopes, handler/timeouts restored after
interruption/error, thread isolation, next-write recovery, structured 503/admission
release on all five HTTP paths, preserved query budgets and actual process exits.
Test DB doubles were extended with the new snapshot interface; no production
fallback to an unsnapshotted reader was introduced. No paid workload or live failure.

### Federation availability closure

Snapshot/recovery PR [#268](https://github.com/nydiokar/AI-team/pull/268)
merged `ce765d0`; final head `d753c71` CI run `41205174257` passed (3m16s).
The four process tests also passed with assertions of the exact persisted state
at each death boundary (16.60 s), preventing a failed admission from masquerading
as an admission-before-ack crash.

Eight tests-first federation regressions failed: unreachable remote returned 200
empty; partial running success was cached after recent failure; failed local SQL
was hidden by a remote result; expired-cache contention served stale as current;
strict transport shape/error distinction was absent. Correction preserves the
explicit mode, successful union/dedupe and gateway-relative ownership, but returns
`jobs_unavailable` on failed coverage. Healthy empty and fresh two-second cache
remain success; the remote-only UI topology with deliberately absent local DB
remains supported (J2 effects still need receipts). No successful effect is replayed.

Adversarial review found a client-only strict check could still trust an old
server's false-empty response. One existing `/jobs` handler now offers explicit
`format=verified-v1` with a versioned envelope; strict UI clients reject old-server
arrays, malformed rows, over-limit arrays and missing format confirmation. Default
successful arrays and unrelated TaskServerClient best-effort defaults remain.
Task-server must be upgraded before dependent federated gateways; no federation
retirement or cross-server atomic-snapshot claim. The deployed gateway was read
inside its container: `jobs_read_mode=controller`, so no compatibility mode is
silently activated. No dependent installations were modified.

Service boundary for the existing task-server `/jobs`: worker-token auth; limit
1–1000 (gateway uses <=50), node/session IDs <=128, validated status/ownership;
no body consumed. Four active reads held through JSON serialization, 429 before DB
otherwise, 503 on DB acquisition/query/encoding/budget failures. Both formats'
100-call tests block four encoders and reject 96 extra reads; malformed input and
failure/recovery/permit cleanup are tested. Same scoped cooperative SQL budget as
other HTTP read surfaces; no new service/queue/cache authority. On the isolated
940-job enlarged copy at limit 1000, four simultaneous encoded arrays were each
1,045,949 bytes; traced Python peak 12,340,526 bytes; RSS 39,239,680→98,123,776,
PSS 29,300,736→79,878,144 bytes (native included). At 100 calls only four such
materializations run. These measured values are not absolute historical-field
bounds. The legacy urllib transport retains two-second socket-operation timeout
and unbounded historical body bytes, not a total network deadline; authenticated
operator/federation trust remains required. S3's native IO/slow-reader/oversized
history policy remains explicitly deferred, not a reason to invent truncation.

Federation regression command:
`.venv/bin/pytest tests/test_jobs_federation_availability.py tests/test_watched_jobs.py tests/test_jobs_read_authority.py tests/test_task_server_client.py tests/test_job_completion_protocol.py tests/test_job_completion_reliability.py tests/test_read_snapshot.py tests/test_job_completion_process.py tests/test_control_api_write.py --tb=short -n 4`
— **171 passed, 8 existing warnings in 23.97 s**. Includes facade→strict client→
real in-process authenticated task-server, healthy union, remote outage and recovery;
no socket/network or worker/agent execution. Source lint diagnostic comparison
against `ce765d0` and new-module Ruff/diff checks pass. Self-review preserves remote-
only UI support while requiring DB for effects, successful default arrays, fresh
cache, ownership/filter/dedupe and controller zero-HTTP behavior. Old server cannot
silently satisfy the versioned contract. Deployment/live checks remain pending.

## Ordered mandatory jobs

### J1 — Jobs authority and enrichment

- Priority: P1, execute first. Status: VERIFIED (implementation, merge and observed
  production reads); audit correction for UI availability merged/deployed in PR #266.
  Remaining production-grade limits and unverified cases are recorded in the audit below.
- Classification: confirmed defects; compatibility change explicitly accepted.
- Baseline evidence (before J1): `TaskOrchestrator.list_watched_jobs` always merged local and remote
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

- Priority: P1. Status: VERIFIED (implementation, targeted checks, review, CI, merge
  and deployment). New terminal delivery/recovery is NOT production-verified.
  Depends on J1; operator authorized review, corrections and merge on 2026-10-29.
- Classification: both correctness defects deterministically reproduced, correction
  implemented. Discovery/retry/restart guarantees are bounded as specified below.
- Baseline evidence (before J2): `_job_completion_poller` advanced `_last_job_poll` after SELECT;
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
- PR: [#262](https://github.com/nydiokar/AI-team/pull/262), implementation `adfc2f4`
  (base `27081a0`), main's documentation-only `618cd4c` merged as `77e2260`.
  Final implementation/test head `f9c213d` passed CI
  ([run 40731253503](https://github.com/nydiokar/AI-team/actions/runs/40731253503)).
  Review correction `d0526eb` passed CI run `40858265943`. PR #262 merged as
  `d52a83b` on 2026-10-29. Deployed on 2026-10-30 in `8bb4422`, retained
  in final build `ee59596`; empty completion protocol read verified, external
  notification/admission/recovery not observed in production (see audit).

#### J2 correction contract (2026-10-28, before implementation)

Source tracing also found two swallowed Telegram error layers: NotificationService
and TelegramInterface. Push was explicitly best-effort/fire-and-forget already.
A moved in-memory mark cannot observe real Telegram failure or recover a restart.

Smallest coherent correction: use the existing controller SQLite store for a
specialized job-effect receipt table, plus the existing managed turn admission
identity (`watched:<job_id>`). A receipt is NOT another agent inbox: it owns only
job side-effect delivery and never answers what an agent is waiting for. Reusing
mesh task rows as discovery receipts would fabricate duplicate conversation turns
for notify-agent jobs; existing managed effects consume already-terminal agent
turns and do not own watched process discovery/admission. No new broker/service.

- Discovery uses eligible terminal rows absent from receipts, independent of any
  timestamp/start time. Migration 47 enrolls existing running jobs and future jobs;
  already-terminal pre-migration jobs stay historical (no notification storm).
  Recovery of those old outcomes requires an explicit operator backfill decision.
- One immutable bounded snapshot per globally identified job, first observation
  wins (same job-ID dedupe contract as the old local/remote union). Durable notice
  and action receipts are independent: successful notice is not repeated merely
  because admission retries. Notify-only action is an idempotent terminal audit;
  notify-agent action is the existing permanently idempotent managed admission.
  The retired best-effort/session-save helper and old watermark/processed-set
  branches have no remaining callers and are removed from the live path.
- Five attempts/effect, serial bounded batches (25), 120 s token-fenced lease;
  20 s notice and 30 s action wait bounds. Expired leases recover after a restart;
  stale holders cannot acknowledge a newer claim. SQL failure preserves pending
  intent; unknown sends may be repeated. Exhaustion/permanent refusal is recorded
  dead with a bounded error and an essential log, never reported as successful.
- Notifications: opt-in strict Telegram error propagation for watched jobs only.
  Other callers retain best effort. A successful receipt means the notification
  call completed, not human receipt. Web Push remains its existing best-effort
  channel; it is not promoted to durable per-subscription delivery. Retries after
  a timeout, partial Telegram send or send-before-ack crash can duplicate notices.
- Continuations: no unmanaged execution fallback. Supported sessions are born
  managed; un-enrolled/closed/gone sessions end in explicit refusal/audit. Transient
  carrier/storage/capacity failures retry. Commit-before-ack replays the same turn;
  admission is distinct from execution/completion of that turn. Operator holds and
  existing Case/lineage rules remain authoritative.
- Federation gets a bounded authenticated completion-page read (compact projection,
  25 rows, bounded cursor/body), not the old newest-50/start-time window. A bounded
  rotating keyset scan resets at EOF; a terminal transition behind its cursor is
  found on a later sweep. Receipts survive restarts; scan cursors need not. An old
  server without the new protocol fails explicitly, never silently falls back to
  lossy listing. A local controller DB is required for receipts even in federation;
  absent DB pauses effects. Deploy authority/controller before compatible gateways.
- No exactly-once external notification claim. One durable continuation identity
  is guaranteed by existing admission constraints; an exhausted/withdrawn/refused
  action is not described as delivered execution. J3–J5 and S1–S4 stay untouched.

#### J2 implementation and verification record

Reproductions were run before implementation: delayed writer timestamps at :10,
blocks before write acquisition, another completion is read at :11, poll advances
past :12, delayed writer commits, timestamp reader loses it. The same test then
requires durable polling to find BOTH jobs. A raising notifier is called once by
the old processed-set path despite two deliveries; the regression requires two
attempts and a successful durable notice receipt. Both tests failed on the old
implementation (recorded in `/tmp/ai-team-j2-red.txt`), not merely hypothetical races.

The new tests use real file-backed SQLite and the real admission/scheduler seams,
with fake transports/carriers and the paid-backend spawn guard. Cases include:

- Delayed commits and old start/update timestamps; completion before gateway start.
- Known send failures; real NotificationService → TelegramInterface error propagation;
  non-job callers retain best effort; actual sends are replaced by async spies.
- Notification failure with successful admission and the converse; retries never
  repeat an acknowledged independent effect. Five failures become durable dead
  state without starving later jobs. Backing-store contention refunds known attempts.
- Send-before-ack loss may duplicate notice; admission-before-ack loss, a real DB
  reopen and a fresh gateway instance still yield one turn. Cancellation leaves
  a lease; expiry recovers. Last-attempt committed action is reconciled before
  budget exhaustion. Stale lease acknowledgements are rejected.
- Ten simultaneous competing deliveries cannot send/admit twice while a lease is
  held. A 20 s soft pass budget leaves unattempted work durable for later passes
  (one current effect can overrun it up to its own timeout; not a hard SQL deadline).
- Full poll → durable managed admission → real scheduler → fake carrier claim →
  real completion → fresh gateway poll preserves one identity. This proves the
  controller seam, not a paid backend turn or external human receipt.
- Closed/un-enrolled continuation refusal, operator-stop holds, Case affiliation,
  Case interrupt/session close withdrawal, notify-only transcript/audit semantics.
  Failed actions leave an explicit failure explanation in the audit reply.
- 131 remote completions, starts before gateway startup, a terminal transition
  behind a saved cursor, protocol 404/oversize/truncation/invalid status/cursor,
  and previously persisted effects draining even during remote discovery outage.
- Invalid legacy owner IDs are never truncated into another recipient: they
  become unroutable/dead snapshots, retain original evidence in jobs, and cannot
  poison a valid completion on the same page.
- Real migration 46→47 skips historical terminals, retains running/future jobs;
  fresh DBs converge. J1 read budgets, response semantics and zero-HTTP guarantees
  remain covered. Internal eligibility metadata is excluded from public job reads.

Final targeted commands:

```bash
/home/cifran/dev/AI-team/.venv/bin/pytest tests/test_job_completion_reliability.py tests/test_job_completion_protocol.py tests/test_turn_queue_4d.py tests/test_watched_jobs.py tests/test_jobs_read_authority.py tests/test_task_server_client.py tests/test_push_notifications.py tests/test_output_truncation.py tests/test_telegram_session_flow.py tests/test_turn_queue_a84_effects.py tests/test_control_api_write.py --tb=short -n 4
/home/cifran/dev/AI-team/.venv/bin/pytest tests/test_a104_single_pending_source.py tests/test_flow_schema_extension.py --tb=short
```

Results: **268 passed, 8 existing dependency warnings in 34.51 s**; **21 passed in
18.53 s**. The latter checks the single inbox authority and migration convergence.
The final seam run includes the public completion-page OpenAPI schema assertion
and the guard that avoids re-entering acknowledged effects.
Ruff passes on the new model/test modules; comparison by diagnostic code/message
against main found no introduced diagnostics in modified legacy modules.
`git diff --check` passes. PR #262 is published; implementation head `f9c213d`
passed CI run 40731253503. Subsequent evidence-only head checks are attached to the
PR. Those pre-merge gates subsequently passed; final merge/deployment evidence
is in the engineering audit below.
No local full/e2e suite, external notification, paid backend, worker restart or J2
production deployment is part of this verification.

CI follow-up: runs 40729618645 and 40730413127 exposed three historical-schema
fixtures that rewound the version ledger without removing migration-47 objects.
The schema-42/44 fixtures now remove those objects before testing migration replay;
production migration semantics are unchanged. The first run also exposed a
cost-alert test that drained Tasks but did not wait for its trailing timer. The
same failure was reproduced on current main with a 2 ms timer delay; the test now
waits for the second check's completion event and retains its concurrency assertion.
That test passes with the same timer delay. The three affected files pass together:
`pytest tests/test_agent_inbox.py tests/test_turn_queue_stage8a.py tests/test_cost_alert_push.py --tb=short`
— **70 passed, 2 existing warnings in 16.46 s**. Production cost-alert logic is
unchanged. Final PR-head CI is required separately from these targeted checks.

Isolated-copy measurements: SQLite backup of the pre-J1 snapshot (source opened
`mode=ro`) into `/tmp`; only that copy migrated/changed. Migration 46→47 + MeshDB
open took 0.0081 s, integrity check `ok`; all four running/recent × all/unowned
public shapes equalled the pre-migration rows, historical completion page empty.
Twenty-five synthetic jobs with maximum Unicode label/tail and a 64 KiB command
produced a 211,064-byte page, 913,948-byte traced Python peak (~3.66 MB for four),
maximum persisted snapshot 8,440 bytes. Thirty warmed read+serialization samples:
1.899 / 1.931 median / 2.190 ms max. These are isolated observations, not production
latencies or total RSS. Discovery uses ONE data SELECT + one executemany transaction;
EXPLAIN uses `idx_jobs_completions` and the receipt PK for NOT EXISTS, no temporary
sort. Complete response equality and indexed work matter in addition to query count.

#### J2 service-boundary and operational contract

- `GET /jobs/completions` keeps the existing worker-token authentication. A literal
  route precedes `/jobs/{job_id}`. Cursor is ASCII ID syntax, max 128; limit 1–25,
  no request body consumed. Invalid inputs are 422, invalid/missing auth 401/403,
  unavailable/query-failing DB 503, four busy readers 429. No failure detail leaks.
  Semaphore includes compact projection and JSON serialization; tests hold it in
  serialization and verify rejection/release. The legacy jobs endpoint is unchanged.
- One compact snapshot is capped at 16 KiB in SQLite; 25 entries/page, four pages
  scanned serially/pass, at most 25 pending effect IDs loaded. Client rejects bodies
  over 1 MiB and malformed/nonadvancing pages. At N=100 incoming calls, four active
  page materializations and 96 small rejections replace 100 responses; synthetic
  active Python estimate ~3.66 MB, excluding native SQLite/connection caches.
  Existing S3 SQL-deadline/native-RSS limitations remain explicit, not closed.
- DB mutations use existing five-second managed-write acquisition deadlines.
  Reads retain SQLite's 15 s lock timeout, not a hard execution deadline. Remote
  sockets retain two-second per-operation timeout, not a slow-drip total deadline.
  Awaited notice/action have 20/30 s bounds; cancellation does not undo SQLite
  work already running in a thread or a Telegram message accepted externally.
- One serial consumer/pass, token-fenced 120 s leases, process-independent receipts.
  After a crashed holder, retry waits until lease expiry (up to 120 s plus poll
  scheduling). A statement/IO stall beyond the lease can leave external outcomes
  uncertain; stale acknowledgement cannot mark a new lease done. Managed admission
  idempotency still fences duplicate continuation intent. No exactly-once send claim.
- Public jobs APIs retain process status semantics. Receipt dead/error state is
  visible through the durable table and essential structured warning logs; this
  PR does not add a dashboard delivery-status badge or per-subscription push receipts.
  Configured Telegram errors propagate only for watched jobs. Push stays best effort;
  no configured Telegram target means call completion, not a claim of human delivery.
- Receipt identity keeps J1's global job-ID contract: registration allocates UUID
  job IDs; local/remote duplicate IDs are one first-observed job. This is not a
  new cross-controller namespace or multi-tenant federation guarantee. Receipt rows
  are retained to preserve replay history; no unsafe TTL/prune is introduced.

#### J2 rollout and explicit operator recovery

Authorized gateway/task-server rollout completed on 2026-10-30 (migration 47
additive; backup and exact builds in the audit). Workers were not restarted.
The federation upgrade and historical recovery procedures below remain explicit
operator decisions for dependent installations; no historical backfill was performed.
For an older federated installation, upgrade its authoritative task-server protocol
before its gateway and ensure local MeshDB receipts are enabled/available. Terminals
observed by an old gateway in between upgrades can produce duplicate external notices
on cutover; existing managed turn identities still collapse admission replay.

Pre-migration terminal jobs deliberately stay historical: the old process-local set
cannot prove which notices were sent. Automatic replay could send old notices or
start previously unadmitted work. Historical recovery is an explicit operator
backfill decision, not a silently closed defect or a promise of historical delivery.
Inspect a SPECIFIC job and its deterministic turn before authorizing recovery:

```sql
SELECT j.id, j.session_id, j.status, j.completion_enrolled,
       e.notification_state, e.notification_attempts, e.notification_error,
       e.action_state, e.action_attempts, e.action_error, e.lease_until
FROM jobs j LEFT JOIN job_completion_effects e ON e.job_id=j.id WHERE j.id=:job_id;
```

Only after that authorization, enroll that specific historical terminal with
`UPDATE jobs SET completion_enrolled=1 WHERE id=:job_id AND status IN ('done','failed','lost');`.
For an exhausted receipt, reset ONLY the selected effect's state to `pending`,
attempts to zero and error to NULL, clearing its lease token/deadline only if no
live lease exists. Do not reset the other successful effect or change its snapshot.
Notice recovery can duplicate a send whose prior outcome is unknown; action
recovery reconciles an already-admitted deterministic turn before attempting again.
These are operator procedures, not production mutations performed in this dispatch.

### J3 — Session-reason N+1 and running-job truncation

- Priority: P1, MUST-FIX after J2. Status: VERIFIED (implementation/review/merge).
  PR [#263](https://github.com/nydiokar/AI-team/pull/263), merge `af4e008`;
  CI run `40859652586` passed. Deployed in final build `ee59596`;
  ordinary reason/queue payloads observed, stress/failure cases verified in isolation.
  J2 review/correction/merge and gateway rollout are complete; external delivery
  verification remains separate.
- Baseline defects (before J3): `build_reason_batch` did per-case
  `get_flow_run`, `case_quota_pause`, `transient_pause`; its N+1 test permitted 30
  per-case calls for ten cases. `list_jobs_for_sessions` limited all statuses to 200
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
- Implementation: `feat/session-reason-batch`, based on merged J2 `d52a83b`.
  Tests/evidence below. Reviewed and merged via PR #263 as `af4e008`;
  deployment/read observations and remaining acceptance limits are in the audit below.

#### J3 implementation evidence — 2026-10-29

Tests-first real-SQLite reproductions on merged J2 measured **8 / 65 / 605 / 1508**
reason data SELECTs for **1 / 20 / 200 / 501** distinct waiting Cases. A separate
regression created one old running job plus 201 newer terminal jobs: the old reader
returned only terminals and incorrectly derived `open_case_idle`.

`MeshDB.running_job_sessions` now returns distinct requested running owners without
historical-row limits. `case_reason_evidence` returns a frozen typed projection of
Case status and two independent newest relevant event types; both readers deduplicate
and chunk at 500 IDs. The reason builder keeps inbox and live-task predicates, chunks
their IN lists, and replaces only per-Case status/pause reads. Existing single-Case
pause helpers and their payload enrichment are unchanged. No migration, timer, new
inbox, admission or scheduler policy is introduced.

Real SQLite tracing now proves **5 / 5 / 5 / 10** reason SELECTs and **7 / 7 / 7 / 13**
complete list SELECTs (base list + cached enrolled queue overlay) at those sizes.
The existing test that permitted 30 per-Case calls now requires zero. Tests cover
shared/missing Cases, eventless and closed/cancelled/blocked/NULL/normalized statuses,
all primary session states and manager/worker roles, independent pause/resume/decline/
exhaustion/respawn sequences, quota precedence, and event-ID order with reversed
wall-clock timestamps. The old running job now remains `waiting_job`. EXPLAIN uses
the existing flow-events index for both bounded event probes and the jobs-session
index for running owners, with no temporary Case sort or payload/objective reads.
Repeated input IDs issue one query, empty ID lists issue none.

```bash
/home/cifran/dev/AI-team/.venv/bin/pytest tests/test_session_reason.py tests/test_session_reason_view.py tests/test_session_reason_batch.py tests/test_session_service.py tests/test_session_service_lifecycle.py tests/test_db_write_resilience.py tests/test_turn_queue_stage6.py tests/test_a104_single_pending_source.py tests/test_control_api.py tests/test_case_quota_resume.py tests/test_case_transient_resume.py --tb=short -n 4
```

**308 passed, 8 existing dependency warnings in 47.69 s.** The direct five-file
reason/view/service run passed 140 tests in 9.63 s before the final indexed-plan
test was added (that test passed separately). New tests pass Ruff; modified legacy
files add no diagnostic code/message versus base main. `git diff --check` passes.
No paid backend, full/e2e suite, external notification or production write was run.
Independent lead review reran the reason/batch/view files: **115 passed in 9.05 s**.
The queue/API/pause/inbox seams were tested locally; ordinary production reads
are now observed, while pause/old-job/failure scenarios remain isolated evidence.

Boundary review: this adds no entry point. The existing authenticated sessions GET
validates limit 1–1000 and consumes no body; evidence materialization is bounded by
requested IDs and 500-ID chunks, without history/payload reads. At the J3 checkpoint, full Session/history materialization and aggregate memory
were deferred to J4. J4 subsequently added projection/four-slot admission and
measured Python memory; native memory, historical bytes and deadlines remain S3
before higher-volume/untrusted use (also in CONTEXT).
SQLite lock timeout is not a statement deadline; S3 remains open. Backing-read
failures retain presentational degradation; no API availability-policy change or
new startup probe is claimed. No architecture/DB authority or worker changes.

### J4 — Session-list projection and indexing

- Priority: P2, REQUIRED after J3. Status: VERIFIED (implementation, review, CI,
  merge and ordinary production reads). PR [#264](https://github.com/nydiokar/AI-team/pull/264),
  merge `3eab87e`, CI `40861342633`; final deployed build `ee59596`.
- Classification: confirmed overfetch; index benefit is a measured hypothesis,
  not an established production latency promise.
- Baseline evidence (before J4): `MeshDB.list_sessions` selected full rows ordered by `updated_at`;
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
- Implementation: `feat/session-list-projection`; implementation/copy evidence below,
  deployed list/pinned observations and explicit limitations in the audit.

#### J4 implementation evidence — 2026-10-29

Tests-first checks failed on J3 main: the list path constructed full mutable
Sessions, and unfiltered/pinned/unpinned shapes all needed temporary ordering sorts
(**4 failed, 6 deselected in 1.88 s**). `MeshDB.list_session_summaries` now selects
explicit view/reason columns only; `SessionStore.list_summaries` constructs a frozen
Pydantic projection. Full `get`/`save`/`list_all` and JSON history remain intact.
Both adapters share the exact old normalization and the same pure SessionView
conversion. `model_construct` intentionally preserves the legacy dataclass parser's
non-coercing tolerance; status/cache-count rejection, origin/list defaults,
healthy-empty, per-row skip and DB-error/JSON fallback behavior are unchanged.
The cache-count scalar remains selected only to preserve invalid-row rejection.

Migration 48 adds `sessions(updated_at DESC)` and
`sessions(COALESCE(keep_pinned,0), updated_at DESC)` indexes after EXPLAIN on an
isolated online backup (2112 sessions). Before indexes all three shapes scanned
and used a temporary ordering B-tree; compact SQL now uses the appropriate index
without a sort. Ordering remains `updated_at DESC`; synthetic equal timestamps
preserve prior rowid/insertion ties, with no new tie-breaker. A legacy nullable
fixture retains COALESCE semantics; a pinned session older than 219 newer sessions
remains retrievable. Fresh migration, explicit 47→48 and reopen preserve all rows.

Focused command:

```bash
/home/cifran/dev/AI-team/.venv/bin/pytest tests/test_session_list_projection.py tests/test_view_models.py tests/test_session_origin.py tests/test_session_service.py tests/test_session_service_lifecycle.py tests/test_session_reason.py tests/test_session_reason_view.py tests/test_session_reason_batch.py tests/test_control_api.py tests/test_control_api_write.py tests/test_model_picker.py tests/test_flow_schema_extension.py tests/test_agent_inbox.py tests/test_turn_queue_stage8a.py --tb=short -n 4
```

**362 passed, 8 existing warnings in 39.34 s.** The independent review rerun of
projection/view/reason-batch files passed **129 tests in 12.69 s**; the final worker
rerun passed **129 tests in 10.19 s**. Full view
mapping (all primary statuses, configured model, malformed/default origins, last
result/previous native ID, reasons and enrolled queue overlay) matches the full
reader. The 100-call API test holds four serializers, proves 96 requests receive
429 before materialization, and verifies permit release on success, read failure
and serialization failure. New modules pass Ruff; modified legacy modules add no
diagnostic code/message relative to J3 main; `git diff --check` passes.
No paid CLI, full/e2e suite or production mutation was run.

Isolated-copy, checkout-config measurements compare BOTH readers with the same indexes, no reasons
or queue overlay, 200 rows and 30 warmed read+view+JSON samples: complete output
equality (217666 JSON bytes); source-row JSON 3669784→309421 bytes; traced Python
peak 10697541→1451630 bytes. Full reader min/median/max 32.506/33.414/80.546 ms;
compact 18.951/21.930/25.285 ms. These are fixture/copy observations, not production
latency or RSS guarantees. A separate compact 1000-row run WITH reasons/overlay
produced 1149855 JSON bytes and a 7759002-byte traced peak (~31.0 MB for four).

Service boundary: existing authenticated GET limit 1–1000 and pinned validation
precede reads; no body consumed. A local four-slot nonblocking semaphore is held
through materialization AND JSONResponse serialization; excess returns structured
429 `sessions_busy`. At N=100 only four materializations execute, 96 reject. No
new endpoint or generic gate framework; thread execution/inbox/scheduler patterns
remain unchanged. Backing DB failure retains the existing JSON fallback; exhausted
read errors retain the old empty response contract. SQLite's 15 s busy timeout is
only a lock bound, with no hard statement/server execution deadline (S3). Historical
scalar text/JSON-fallback files remain unbounded in bytes; absolute byte handling
and cancellation need S3's separate compatibility contract before high-volume or
untrusted use. No hard RSS/output bound; successful rollout is now recorded separately below.

### J5 — Compact affiliations

- Priority: P2, REQUIRED after J4. Status: VERIFIED (implementation, targeted checks,
  independent adversarial review, CI, merge and all three ordinary UI consumers).
  PR [#265](https://github.com/nydiokar/AI-team/pull/265), merge `8bb4422`; J4 merged as `3eab87e`.
- Classification: confirmed redundant objective payload; compact transport required.
- Evidence: `list_session_case_links` joins full objectives before builder dedup;
  `build_session_affiliations` emits objective per session. Sessions, Session Detail
  and Cost share `useSessionAffiliations`. The baseline hook exposes an error but
  defaults to an empty map; Detail ignores that error and asserts Standalone before
  success. Sessions omits the chip; Cost already discloses errors but not loading.
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
  <750 KB for the same 1,082-entry dataset is measured below, not a unit-test timing gate.
- PR #265 implementation `cffd8d9`, merge `8bb4422`; CI
  [41094605454](https://github.com/nydiokar/AI-team/actions/runs/41094605454) passed.
  Deployed first as `8bb4422`, then retained in `ee59596` with the J1 UI correction;
  positive Sessions/Detail/Cost observations and remaining limits are in the audit.

#### J5 implementation, adversarial review and boundary evidence — 2026-10-30

`format=compact` explicitly selects titles; absent/`legacy` preserves the exact old
fields and behavior. OpenAPI documents both shapes; separate TS compact/legacy types,
client selection and adapter agree. Case detail still returns the full objective.
Two data SELECTs under one savepoint select newest link IDs and stream relevant
case rows. Missing case rows survive as links with NULL status/fallback title;
newer closed links win. No global cap or per-case SQL; obsolete case objectives
are not loaded. Only one objective row at a time is retained while computing
one bounded title/status per case. Link rows, titles, output and JSON still scale
with the whole index. Existing indexes are reused; no migration.

The shared hook uses `["work-affiliations", "compact-v1"]`; existing event/mutation
prefix invalidation still works. Only successful data establishes absence. Loading,
disabled/no-token and failed initial/refetch reads are unknown; failed cached data
is hidden until recovery. Sessions and Cost disclose loading/unavailable; Detail's
shared link renders loading/unavailable rather than Standalone. During a healthy
background fetch the last successful snapshot remains available. These are observed
snapshots, not a real-time membership guarantee; no affiliations are persisted.

Verification:

```bash
.venv/bin/pytest tests/test_session_affiliations.py tests/test_work_read_model.py tests/test_control_api.py --tb=short
env -u NODE_CHANNEL_FD -u NODE_CHANNEL_SERIALIZATION_MODE pnpm --dir web exec vitest related --run src/transport/workAdapter.ts src/transport/apiClient.ts src/hooks/useWork.ts src/screens/SessionsScreen.tsx src/screens/SessionDetailScreen.tsx src/screens/CostScreen.tsx src/components/work/SessionAffiliationLabel.tsx
env -u NODE_CHANNEL_FD -u NODE_CHANNEL_SERIALIZATION_MODE pnpm --dir web typecheck
env -u NODE_CHANNEL_FD -u NODE_CHANNEL_SERIALIZATION_MODE pnpm --dir web build
```

**109 Python tests passed** (2 existing warnings, 42.89 s); **120 frontend tests
passed** (12 files, 13.02 s); typecheck and production build exited 0. Inherited
Node IPC environment (`NODE_CHANNEL_FD=3`) caused the previous exit 134; clearing
those variables resolves it without changing dependencies. Common Python/TS
fixtures cover tag priority/empty tags, markup, JS-specific whitespace, fallback
IDs, Unicode and truncation through a surrogate pair. Expected UTF-16 units avoid
Vite's rejection of lone-surrogate JSON fixture literals; real API JSON transport
is tested separately. Tests also compare 232 complete memberships against legacy,
concurrent WAL writes between reads, consumer-failure transaction release, full
case objective retention, schema, bad format, DB acquisition/query/serialization
failure, real QueryClient cached-positive/empty failure→recovery and prefix invalidation.
Ruff diagnostic code/message comparison with base adds no diagnostics; diff check passes.

Independent review found a material P2: directly porting the browser markup regex
made unclosed `<` input quadratic on the server (16 KiB: 413 ms). The correction
excludes unmatched suffixes and searches each tag's first opener/closer once.
Review rechecked **88,573 exhaustive markup strings** and **2,000 adversarial
Python/actual-TypeScript title pairs** without mismatch; 16/128 KiB malformed
markup measured 1.53/4.16 ms. Independent reruns: 43 affiliation tests and 51
frontend tests passed. No remaining material finding in the reviewed path;
these probes do not establish a hard execution bound.

Read-only live-source SQLite backup, then all initialization/stress on isolated
`/tmp` copies; same JSON encoding for both readers. Results:

| Dataset | Entries | Legacy → compact JSON bytes | Legacy → compact traced total peak | Compact intermediate build peak |
|---|---:|---:|---:|---:|
| Production backup | 1,082 | 5,137,379 → 260,777 | 21,216,995 → 2,064,138 | 863,997 |
| 80 cases, shared 64 KiB objectives | 1,082 | 71,066,565 → 116,579 | 214,407,860 → 1,617,805 | 678,093 |
| Same + 262,144 emoji/objective | 1,082 | compact 116,579 | compact 7,295,190 | 7,295,190 |

After link materialization, traced live bytes were 424,656 / 389,465 / 388,632.
Warm compact read+build+serialization min/median/max: production copy
28.62/31.34/36.11 ms (30 samples); 64 KiB fixture 10.94/12.39/13.66 ms (30);
large-Unicode 158.40/159.17/181.17 ms (3). No production latency/RSS claim.
EXPLAIN: both SELECTs use covering `idx_flow_links_entity`, rowid link lookups and
case PK; newest links materialize twice and final link order uses a temporary
B-tree. No objective JOIN fanout or N+1. The historical <750 KB target is met on
the copied 1,082-entry membership (260,777 bytes), not an absolute output limit.

Service boundary (compact option):

- **Concurrency:** authenticated sync handler, four nonblocking process-local
  permits held through title projection AND JSON encoding; 96 excess calls at
  N=100 return 429 before DB work. Tests hold four serializers and verify release
  on success, DB acquisition/read failure and serialization failure.
- **Memory:** four active materializations estimate ~8.26 MB traced Python for
  this production copy or ~29.18 MB in the Unicode fixture, plus rejection bodies.
  These are four-times-single-call estimates, not concurrent/native RSS bounds.
  Permits end at encoded Response creation, not socket drain; slow readers can
  retain additional completed bodies outside these active-materialization estimates.
  SQLite caches (configured 8 MiB/thread connection), temporary B-trees, allocator
  retention and arbitrarily large historical fields are excluded. S1/S3 remain
  required before materially larger/untrusted use; no arbitrary membership cap.
- **Request size/malformed input:** GET consumes no body; the only selector is
  exact `legacy|compact` (422 otherwise), auth precedes reads. Corrupt backing
  values/query/encoding errors fail the complete compact read with sanitized 503,
  rather than inventing an empty successful index. No startup DB probe added.
- **Timeouts:** new browser compact read has a 15 s AbortSignal deadline. Server
  SQLite retains 15 s lock wait, no statement/whole-response deadline. Browser
  abort does not cancel a running synchronous query. Hard cancellation and
  oversized-history contracts remain S3; objective size/CPU is not absolutely bounded.
- **Backing store/compatibility:** absent or failing DB returns structured 503
  `affiliations_unavailable`. Legacy default deliberately retains its old empty-on-
  absent-DB behavior and ungated materialization; legacy admission/size/deadline
  hardening needs a separate compatibility decision under S3. No DB writes,
  new ownership path, worker restart, paid backend or local full/e2e suite.


## Engineering audit — 2026-10-30

Audited **after J5 merged** against the original scope/acceptance above, current
source at `8bb4422` and corrective merge `ee59596`, PR histories #261–#266 and
focused tests. No P0/P1 defect was demonstrated. One P2 UI availability defect was
reproduced, corrected, reviewed, merged and deployed separately (#266). VERIFIED
below applies to the named evidence, **not blanket production-grade certification**.

| Job | Implementation / merge | Deployment | Direct functional observation | Remaining work / limits |
|---|---|---|---|---|
| J1 | Met: controller/federated selection, shared two-read composition, SQL ownership/filter-before-LIMIT, API 503/admission/off-loop polling; [#261](https://github.com/nydiokar/AI-team/pull/261) `e0e2877`; UI correction [#266](https://github.com/nydiokar/AI-team/pull/266) `ee59596` | `ee59596` | all/unowned job arrays equal authoritative SQL; healthy-empty; System renders rows; both UI errors/retry tested as described below | S3 text/SQL/native-memory limits; federated partial/stale behavior deliberately retained; no fresh production running job in this audit |
| J2 | Met within declared bounded contract: timestamp-free discovery, independent durable effects, fenced leases/retry, permanent admission identity, bounded federation pages; [#262](https://github.com/nydiokar/AI-team/pull/262) `d52a83b` | `ee59596`, migration 47 | authenticated completion page 200/empty; receipt table/index present; 94 historical terminals excluded, zero receipts | **NOT live-verified:** new terminal discovery→notice/admission/execution, failed send, crash/restart recovery. Controlled fake-carrier/transport tests prove those seams. Historical backfill remains operator-owned; notices may duplicate; Push is best effort |
| J3 | Met: deduplicated 500-ID evidence, independent latest pause facts, all running owners, preserved reason precedence/queue/inbox; [#263](https://github.com/nydiokar/AI-team/pull/263) `af4e008` | `ee59596` | actual session reason/queue payloads; production-copy pre-J3 reason equivalence; frontend adapter/grouping checks | Old-running-job >200 and pause/event-order combinations are isolated fixtures, not current live examples. Existing silent evidence degradation remains a compatibility risk; no atomic snapshot across every reason/queue SELECT |
| J4 | Met: projection/full-reader equivalence, history retained, JSON/null/tie behavior, migration/index plans and pinned retrieval; [#264](https://github.com/nydiokar/AI-team/pull/264) `3eab87e` | `ee59596`, migration 48 | 200 latest / 200 unpinned / 4 pinned; **3 pinned outside newest 200** still retrieved; 200 queue overlays; actual Sessions UI rendered | Deliberate old fallback/error-to-empty semantics remain; malformed-row/fallback/migration and 100-call tests are isolated. Scalar/fallback bytes, native RSS and execution deadlines remain S3 |
| J5 | Met: explicit compact/default legacy, complete newest/orphan mapping, once/case equivalent titles, schema/client/cache/UI integration and reviewed boundary; [#265](https://github.com/nydiokar/AI-team/pull/265) `8bb4422` | `ee59596` | 1,082 mappings agree with legacy IDs/role/status; 260,777-byte compact response; full detail objective; Sessions title, Detail link and **30-day Cost case usage 200 + rendered breakdown** | S1 whole-index growth, S3 historical bytes/native/SQL deadline; default legacy path intentionally ungated. Snapshot races/Unicode/malformed/100-call/error paths proved in isolation, not by damaging live data |

### Evidence and architecture assessment

- **History/build:** GitHub recheck confirms #261 `e0e2877`, #262 `d52a83b`,
  #263 `af4e008`, #264 `3eab87e`, #265 `8bb4422`, #266 `ee59596` merged;
  each relevant PR-head CI passed. #266 implementation `a714ed5`, CI
  [41096668574](https://github.com/nydiokar/AI-team/actions/runs/41096668574)
  passed. Final gateway **and task-server** report `ee59596`; image
  `49a707139f0c`, tag `ai-team:prod-ee59596`, schema 48, two nodes online,
  coverage true, no startup traceback. Worker PID `2711734` and restart count 5
  unchanged across both deployments. Node build mismatch is expected: worker
  code was not part of this programme and workers were not restarted.
- **Rollout:** initial preflight GO at `8bb4422`, live schema 46→48. Online backup
  `/home/cifran/ai-team-data/backups/mesh-pre-8bb4422-20261030T1702Z.db`,
  316,407,808 bytes, mode 0600, integrity `ok`; rollback image
  `ai-team:pre-8bb4422` preserves J1. Corrective preflight GO at `ee59596`,
  48→48; rollback `ai-team:pre-ee59596` preserves `8bb4422`. Both Compose
  services rebuilt/recreated together. Compose ran from clean main with its host
  env; build context was a `git archive` of the exact commit, excluding preserved
  untracked analysis scripts that the normal Docker allowlist would include.
  Pushed deploy tags `deploy/20261030T1702Z-8bb4422` and
  `deploy/20261030T1717Z-ee59596`. No rollback/data restore used.
- **J1 authority/UI:** `list_controller_jobs` is shared by the facade and API
  fallback; controller mode performs no remote union/session reconciliation.
  Live deployed config is explicitly `controller`; all/unowned arrays (20/50)
  reconcile with read-only SQLite, and nonexistent-owner filter returns true
  empty. Original zero-HTTP packet capture remains historical J1 evidence; it
  was not repeated for the final build. Audit caught both UI consumers discarding
  the API error: #266 adds jobs-only unavailable/stale notice + retry and a
  parent availability summary. Cached rows stay visibly stale; only successful
  empty establishes absence. This closes a demonstrated cross-layer defect.
- **J2 integration:** receipts own watched-job effects, not agent waiting; A104's
  inbox remains authoritative. Existing `watched:<job_id>` managed admission,
  scheduler and audit paths are reused, without a second executor or inbox.
  Migration excludes historical terminals intentionally to avoid notification/
  continuation storms; the observed 59 done/32 failed/3 lost jobs are all excluded.
  No pending completion existed to observe a new live effect. Receipt `done`
  never means human receipt or completed agent execution. Five-attempt dead state,
  unknown-send duplicates, lease expiry and operator recovery are explicit
  contract decisions, not silently certified exactly-once delivery.
- **J3/J4 integration:** `list_views` now uses `list_summaries`, then the existing
  reason and queue overlays; full get/save/list_all remain lifecycle authority.
  Shared parsing/conversion preserves configured model/origin/previous ID/result
  semantics. J3 reads inbox waiting/own-task liveness and latest relevant ledger
  event types; single-case enriched pause helpers remain unchanged. Query counts
  are not constant SQL cost: event-type probes can scan irrelevant case history
  using the existing `(flow_run_id,id)` index. The copy's largest case had 667
  events; no additional index was justified by a measured regression. Per-case
  work/deadlines at larger histories remain a risk to size under S3.
- **J5 integration:** one versioned shared hook feeds all three consumers; no
  per-case HTTP fanout, new ownership store or cached objective pathway. Legacy
  default and case detail retain full objectives deliberately. The stronger
  isolated race probe injected a committed writer **immediately before the second
  SELECT** via SQLite tracing: current request retained old links/status/title,
  next request saw the commit, and the read transaction released.

### Verification coverage and production-grade follow-ups

Post-merge audit command:

```bash
.venv/bin/pytest tests/test_jobs_read_authority.py tests/test_job_completion_reliability.py tests/test_job_completion_protocol.py tests/test_session_reason_batch.py tests/test_session_list_projection.py tests/test_session_affiliations.py --tb=short
```

**206 passed, 2 existing warnings in 57.39 s.** J1–J5 module selection covers
SQL counts/authority, delayed commit, independent effects, competing leases,
restart and last-attempt reconciliation, 131 remote completions/protocol bounds,
all-state/pause ordering, old running-job truncation, projection/history/fallback/
migration/index equality, admission serialization/error paths and affiliation
compatibility/Unicode. Original broader seam commands above remain useful history;
a green unit test is not claimed as a paid turn, external receipt or live failure.

Additional frontend audit: `vitest run src/transport/sessionReason.test.ts
src/transport/adapters.test.ts src/lib/sessionGroups.test.ts src/lib/turnQueue.test.ts`
— **64 passed** (4 files, 719 ms). #266's actual System/Detail components plus
ownership/invalidation/affiliation checks: **17 passed** (4 files, 2.86 s), with
initial/cached-positive/cached-empty error→retry/recovery and collapsed summary.
Typecheck and production build exited 0, including inside the Docker build.
Independent J5 adversarial review and golden/probe details remain above.

On an isolated migrated copy of the pre-deploy online backup, complete SessionView
outputs (including reasons/queue) matched full readers for latest 200, pinned 1000
(4 rows), unpinned 200 and latest 1000. Reasons matched the actual pre-J3 function
from `d52a83b` for these same rows. Copy SELECTs were 6/3/6/8 (fewer waiting Cases
than the maximum-budget fixtures); it contained no old-running-job or active-pause
example, so equality here does not prove those specific fixes. Checkout model
configuration was used; exhaustive configured-default cases remain fixture evidence.

Final-build Chromium observations: real authenticated Sessions (closed section
expanded), Detail and 30-day Cost rendered the compact affiliation path; Cost
returned case usage 200 and rendered manager/worker breakdown. Default 7-day top
spenders had no affiliated row; 30-day had ten. System rendered jobs. Zero page
errors; browser/runner exited 0, children reaped. For #266 only the test browser's
jobs response was intercepted as 503: both surfaces displayed unavailable without
false absence, Retry recovered to the **real healthy API 200**, and collapsed
System header stayed reachable. This is synthetic client behavior, **not a real
production DB outage/recovery**. Real backing failures/concurrency/Unicode races
were exercised only on isolated data. Local artifacts are under `/tmp/j5-*.png`,
`/tmp/j5-live-results.json`, `/tmp/jobs-ui-probe.json`,
`/tmp/read-path-live-api.json` and `/tmp/read-path-copy-audit.json`; durable findings
are recorded here rather than depending on those temporary files.

Remaining before broader production-grade certification:
1. **P2, deliberate S3 deferral:** hard SQL/server deadline/cancellation,
   oversized historical-field policy and native/concurrent/slow-reader RSS sizing.
   Separate four-slot gates bound active materialization per surface, not all
   endpoints together or encoded bodies queued behind slow clients. No absolute
   request-memory/latency guarantee exists. Keep authenticated/operator workload
   assumptions until these are sized; browser timeouts cannot stop server threads.
2. **P2 reliability compatibility decision:** sessions retain silent per-evidence
   degradation, JSON fallback and final error-to-empty contract; legacy affiliations
   retain absent-DB empty and ungated reads. These are observed code behaviors,
   not new J3–J5 defects or successful failure-handling certification. A separate
   availability contract is needed to distinguish unknown from healthy-empty end
   to end on those legacy paths. #266 fixes the already-distinguishable jobs path.
3. **J2 live acceptance remains not verified:** observe a genuinely requested new
   watched job through terminal receipt/notification/admission and subsequent
   execution, plus controlled recovery in an authorized staging environment.
   Do not manufacture a paid turn or replay the 94 historical terminals to mark
   this green. Historical recovery and dependent federation inventory/rollout
   remain operator decisions; external exactly-once delivery is not the contract.
4. **S1/S2/S4 remain optional/deferred:** scoped complete affiliation retrieval
   when whole-index growth warrants it; federation retirement only after inventory;
   jobs indexes only after larger-cardinality measurements. Existing two authority
   modes are deliberate compatibility, not an accidental duplicate current path.

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
difference between modes. At J1 closure the watermark/processed-before-notify risks awaited J2; J2 later
corrected those seams as documented above. No exactly-once external-send claim.

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
  At that J1 checkpoint J2 was unimplemented; its current status is in the audit.
  S1–S4 remain deferred; historical text/SQL deadlines are unchanged.

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

Current programme state (2026-10-30): J1–J5 are merged and deployed in `ee59596`;
J1 UI availability received separate audit correction PR #266. Ordinary read/UI
functionality is observed; J2 new terminal delivery/recovery is not live-verified.
The engineering audit below supersedes the old J4-pending/J5-next dispatch claims.
S1–S4 remain deferred, not closed.

### J2 lead review — 2026-10-29

Reviewed PR #262 against the original J2 acceptance criteria and traced discovery,
notification propagation, durable receipts, managed admission and restart recovery.
The pre-correction focused seam run passed **289 tests, 8 existing warnings in
60.52 s** (the recorded 11-file command plus the single-inbox and schema tests).

Review found a refused/exhausted continuation audit inherited the successful job
exit: its reply described action failure but task status was `completed` and
`result.success=true`. A new assertion reproduced this on PR head. Set success
false only for failed-action audits; ordinary job-outcome audits keep job status.
Two existing refusal tests were corrected to require failed audit status.
Notification/admission identity, retries and migration are unchanged.

Correction verification: `pytest tests/test_job_completion_reliability.py tests/test_turn_queue_4d.py --tb=short` — **59 passed, 2 existing warnings in 8.22 s**.

### J1 audit correction — jobs availability reaches the UI (2026-10-30)

Phase-2 source tracing found a demonstrated P2 cross-layer gap outside J1's API
checks: both System `JobsPanel` and Detail `SessionJobsSection` ignored query
errors. An initial 503 rendered “No watched jobs”; cached rows had no failed-
refresh notice, and a collapsed empty System section could disappear entirely.
Two tests reproduced the false absence before the correction (both failed).

Minimal correction: shared jobs-only unavailable/stale notice with retry; only a
successful empty read renders absence. Cached rows remain explicitly stale.
System summary carries availability so an unavailable collapsed section keeps a
reachable header and warning instead of disappearing. No API, query authority,
read cadence, DB schema, queue or worker changes. Review includes initial errors,
cached-positive/empty failures, retry/recovery and the collapsed summary signal.
Tests exercise both actual components. PR #266 merged as `ee59596`, CI passed,
and deployment plus browser-only failure/retry observations are recorded in the
engineering audit above; the original false-absence finding is corrected.
