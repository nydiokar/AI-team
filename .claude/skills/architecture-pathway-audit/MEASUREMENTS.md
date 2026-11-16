# Architecture Pathway Audit — measurements and field experience

This file owns evaluation results, reviewed findings and operational experience.
Usage and requirements belong in [README.md](README.md). Machine-readable data:
[2026-11-16.json](benchmark/measurements/2026-11-16.json). The historical contracts remain
frozen; no detector, contract, application code or baseline was changed for this audit.

## 2026-11-16 — does it still work, and does it catch real defects?

**Verdict:** useful for finding architectural candidates and enforcing explicit
static contracts. It still runs with CBM 0.11.0 and catches real failures, including
two present source defects reproduced below. It is not a reliable autonomous
correctness gate: coverage can fail open, SQL extraction creates both false
positives and false negatives, and the benchmark scorer overstates its meaning.

Audited revision: `49c4dfa62d34e84d891c5730897d2b72cd0155d9` (`origin/main`).
The caller's quota feature branch and its open PR were excluded. Linux ARM64;
primary execution used Python **3.12.16** in the existing
`ai-team:dependency-closure-tests` image, with networking disabled and the source
mounted read-only. CBM **0.11.0** was downloaded into `/tmp` from the official
release; the release archive passed its published SHA-256 check. No resident
installation, gateway deployment, worker restart, paid CLI turn or live DB write.
CBM 0.12.1 was not tested. This is a scoped recheck, not a blind precision/recall study.

### Verification and performance

| Check | Observed result |
|---|---|
| Skill tests, Python 3.12 + real CBM | **54 passed in 22.47s** |
| Skill tests, host Python 3.11 + real CBM | **54 passed in 22.21s**; host interpreter cannot parse all current application code |
| Python 3.12 without CBM | **53 passed, 1 skipped**, 1 warning in 1.84s; the real integration is skipped |
| Current A102 maintained packet | **Met**, 423 files, 0 parse errors, 7 explicit negative-test waivers, 1 docstring note |
| Frozen A102 example on current main | 7 violations, all old environment-flag literals in negative tests; not surviving runtime pathways |
| Frozen A104 example on current main | 41 violations: 7 retired-name occurrences and 34 SQL reader occurrences; not 41 verified defects |
| A102 historical revision `c64a4a7` | 343 contract occurrences; includes four `OpenCodeBackend` occurrences in two test files after deletion of its definition |
| A84 historical revision `645ab38` | 29 occurrences of three declared DELETE names, including live definitions and calls |

The targeted test command was:

```bash
CBM_BIN=/evidence/bin/codebase-memory-mcp python -m pytest .claude/skills/architecture-pathway-audit/tests --tb=short -p no:cacheprovider
```

It ran inside the existing Python 3.12 test image, with the checkout at `/audit`
and scratch artifacts/binary at `/evidence`. The binary-backed fixture tests
expansion, upstream callers, competing-path detection, error propagation and cache
invalidation. Passing these tests does not establish full repository coverage.

The repository sweep used `--scope src --sweep-depth 6 --sweep-cap 600 --roots all`:

| Measurement | Cold run | Reused explorer cache |
|---|---:|---:|
| Total CLI time | 152.3 s | 21.7 s |
| CBM index time | 9.4 s | 7.5 s |
| Sweep time | 122.3 s | 1.5 s |
| CBM calls | 4,539 | 10 |
| Expansions fetched | 1,988 | 0 |
| Sweep cache hits | 10,466 | 12,454 |

The second run used a fresh disposable container: the explorer cache persisted,
but CBM's container-local index did not, so it reindexed. **21.7 s is not a
measurement of a fully warm persistent CBM deployment.** Both runs found 141
routes, visited 766 roots and reported zero root-cap truncations. The loaded graph
had 2,112 nodes and 6,299 edges; the CBM index had 3,433 nodes and 15,061 edges.

There were **280 emitted findings**. Before output caps the rules counted 698
patterns. Four kinds exceeded the 40-per-kind cap: BYPASS (154), CONVERGENCE
(203), DIVERGE_REJOIN (175), SIBLING_OVERLAP (46). There were no Python parse
errors or incomplete adjacency entries on Python 3.12. Coverage still reports
2,709 ambiguous-builtin-name, 1,386 dynamic, 270 ambiguous and 244 unresolved
call observations. These are observations in expanded entries, not unique bugs.
Two routes are conditional; `/assets` is an unenumerated mounted sub-app. All 140
AST HTTP method/path keys matched CBM; the 141st route is WebSocket. The graph
analyzer has 141 route roots even though `--roots all` loads extra public-callable
neighborhoods. It does not turn every private loop into a route root.

### Confirmed current defects exposed by the explorer

**1. Latest spec review can become stale after 5,000 matching events.**
`TRUNCATED_WINDOW` on `flow_events` led to
`src/control/db.py:list_flow_events_of_types` (line 7281), then its caller
`latest_spec_review` (7494), and the downstream acceptance gate in
`decompose_case` (7541). The query defaults to oldest-first and `limit=5000`.
`latest_spec_review` folds only those returned rows and does not request the newest
row. Its `spec_id` filter runs after that SQL limit.

Offline reproduction used the unchanged methods against in-memory SQLite: rows
1–5000 approved the spec; row 5001 rejected it. The latest read still returned
`{passed: true, n: 5000}`. `decompose_case` advanced past its approval gate and
returned `invalid_task_keys` for a deliberately invalid task, where the current
rejection should have returned `spec_not_approved`. Inverting the last two verdicts
also reproduced a missed approval. This proves a conditional correctness defect,
not that any live Case has reached the threshold. The final close/rework guard is
**not** affected by this particular window: it explicitly requests newest-first,
limit 1. The older Manager advancement fold is another consumer of the bounded
reader, but was not independently reproduced and is already slated for A111 retirement.

**2. Case session links silently truncate at 200.**
`TRUNCATED_WINDOW` on `flow_links` led to `list_flow_links` (6763), then
`api_work_detail`, `api_work_roster` in `src/control/routes/work.py` (344/409)
and role lookup in `src/control/cost_read_model.py:assemble_case_usage` (220).
All use the reader's default oldest-first, limit 200 result.

With 201 distinct Case session links, the unchanged reader returned 200 and omitted
session 201. The roster constructs its session IDs from exactly that list, so later
sessions are absent from its rows/jobs/token queries. Detail receives the same
truncated links. Case cost totals come from a separate query and are **not proved
missing**; the defect there is role attribution: a later linked session falls back
to `member` and leaves the manager/worker breakdown. No live frequency or production
impact was measured. The source data and all tested methods were left unchanged.

**A false positive on the same table:** `resolve_dispatch_requester` uses `LIMIT 2`
to distinguish exactly one requester from ambiguity, with filters before the limit.
The actual method returned the single session for one candidate and `None` for two.
This is correct; adding ORDER BY DESC would not fix a defect.

### Recursively checked candidates that are not established application bugs

| Candidate | Source trace and disposition |
|---|---|
| Orphan tables `queue`, `activity`, `ranked` | `control/home_status.py:node_activity` defines them as CTEs over real tables. SQL regex mistakes CTE aliases for persistent stores. |
| Orphan `newest` | `db.py:compact_session_case_data` defines a CTE and joins it to `flow_links`; another alias artifact. |
| Orphan `completion_outbox` | `_get_migrations` reads the retired table during inbox migration. Historical migration work, not a missing live producer. |
| Orphan `sqlite_master` | SQLite owns its writes; application code intentionally inspects the catalog. |
| Orphan `system_alerts` | `list_recent_system_alerts` documents an external writer outside the `src` application scope. Not enough evidence for a missing writer. |
| Write-only `principals` | `QuotaWindowStore.upsert_principal` persists identity metadata, with no detected consumer. Unused persisted metadata is a maintenance observation, not proof that quota readings fail. |
| Write-only `snapshots_archive` | `QuotaArchive.archive_snapshots` retains full-fidelity pruned data. No normal reader is required for archival purpose. |
| Multiple writers of scheduler `_ACTIVE` | `_register` and `_unregister` share `_ACTIVE_LOCK`; unregister checks object identity. Intentional lifecycle pair. |
| Multiple writers of task-server `app` | API construction and `EmbeddedControlServer.start` configure shared app state in different launch paths. No conflicting live write was demonstrated. |
| Quota config divergence | `observe`/`discover_buckets` identify the principal, which reads `CLAUDE_QUOTA_PRINCIPAL_KEY`, then read the snapshot. Identity and utilization have different inputs; the branches are not equivalent configuration paths. |
| Cost `_price` bypass | Session usage constructs totals and invokes `pricing.estimate_cost` directly; case analytics invokes it through `_price`. Sharing the pricing function is intentional. No skipped authorization/pricing gate was demonstrated. |
| Behavioral twins | Examples include Codex archive/unload RPC wrappers with different method strings, task IDs with distinct namespace strings, and directory helpers for different roots. Fingerprints normalize strings, so identical fingerprints do not establish equivalent behavior. |
| Split readers | Inbox lifecycle/status reads and task/event audit reads serve different questions. Co-written tables do not imply every consumer must join both. No new split-authority failure was reproduced. |

This review triaged emitted kinds and investigated consequential candidates through
callers and consumers. It did **not** semantically validate every one of 280 findings,
uncapped output, every package cycle, every sibling or every copied helper. Those
remain candidates, not secretly accepted defects or evidence of a clean system.

### Historical catches and contract interpretation

At `c64a4a7`, `src/backends/opencode.py` defines `OpenCodeServerBackend` but no
`OpenCodeBackend`; `tests/test_turn_queue_4b_carrier.py:369` still imports the deleted
name and uses it. The other reported test also references that deleted class. This
is a real broken import contract, independently confirmed in source. The other 339
A102 occurrences include work not yet cut over at that historical stage; they are
not 339 independent defects. This known historical example is not a blind benchmark.

At A84's declared-done `645ab38`, the checker finds `reconcile_worker_waits` still
defined and `boot_reconcile_case` still called at Manager boot, while the frozen
design ledger says DELETE. This establishes incomplete declared retirement, not
by itself a reproduction of the later wake-loop incident.

On current main, the maintained A102 packet passes with seven narrow, explained
waivers for tests proving retired flags cannot disable execution/correlation. The
frozen example intentionally has no later waivers and reports those same literals.
Do not weaken the frozen example or delete useful negative tests to get a green run.

The A104 example means "only pending_for may SELECT agent_inbox", whereas the
implementation centralizes inbox ownership in a module with lifecycle, recovery,
status and audit queries. Its PENDING_SQL constant is owned at module scope and
executed by `pending_for`; literal extraction cannot connect the definition to its
execution. Tests and the migration/seed tool legitimately inspect the same table.
The 41 current violations therefore do not prove a second pending-state authority.
An enforceable end-state contract must reflect those responsibility distinctions.

### Confirmed limitations and defects in the audit tool

| Priority | Reproduction / root cause | Consequence |
|---|---|---|
| High for automated closure | `Contract(scope=['typo'], retired=['old_send'])` scans 0 files and returns `ok=true`; `check` makes success depend only on violations | A misspelled scope can produce a vacuous green gate. |
| High for automated closure | Syntax-invalid Python containing a forbidden SQL read is text-scanned, loses all store accesses, and returns `ok=true` | Coverage errors do not prevent a passing ownership check. Binary/oversize skips likewise do not fail closure. |
| High for batch consumers | Inject `CBMError` from `ensure_started`, invoke default `--no-serve` without a sweep: CLI returns normally with zero findings | `ready_error` is logged, but omitted by `persist` from JSON; pending analysis can be mistaken for success. Sweep mode instead raises when it needs CBM. |
| High for evaluation claims | Give `score.py` one new `CONVERGENCE` fact mentioning the changed function | Output is `CAUGHT`, `recall 1 / 1`, despite no defect evidence. It counts new entity-touching facts/candidates, not semantic recall. |
| Medium | `SELECT * FROM main.secret` with `stores.secret.readers=[]` passes; unqualified `FROM secret` correctly fails | The SQL regex records `main` instead of `secret`; ownership enforcement has a schema-qualified-name hole. |
| Medium | A function docstring containing `SELECT * FROM secret` violates `stores.secret.readers=[]` despite executing no SQL | `_strings` does not exclude docstrings. CTE alias artifacts above are another SQL false-positive class. |
| Medium | Start on Python 3.11, then reuse the same cache under 3.12 with unchanged files | Cache identity/schema does not include Python grammar version; `sync_files` can retain old parse-error facts. The incompatible host run actually reported `turn_admission.py` unparseable. A fresh 3.12 cache had no parse errors. Cross-interpreter reuse was source-traced, not separately timed. |
| Coverage | `--roots all` seeds only unreferenced **public** callables; `analyze` receives route roots; four kinds hit output caps | A successful bounded run cannot certify every private worker loop or show every candidate. |
| Contract quality | `stores: {secret: {}}` is accepted and constrains neither readers nor writers | A declared store does not imply an enforcement rule. An empty rule is permitted input, not a useful guarantee. |

`score.py` additionally normalizes project names with a narrow regex, then checks
function/table names by substring in serialized finding text. A changed project
name or coincidental substring can change apparent recall. The repository contains
runner/scorer scripts but no tracked blind `GROUND_TRUTH.json` corpus; no honest
new blind-corpus score can be reproduced from this checkout alone. The runner also
prints child return codes without aggregating them into a failing top-level exit.
These are source-traced limitations; only the convergence-only false catch was
executably demonstrated here.

No application fix or detector change is included in this documentation task.
For using this as an automated gate, address fail-open coverage/index outcomes
first, then make evaluation distinguish facts, candidates and verified defects.
For application follow-up, preserve the two failing threshold fixtures above and
fix the specific latest-review and Case-link consumers; a blanket newest-first
change would break intentional oldest-first/ambiguity behavior.

### Evidence and reproduction notes

The committed JSON records the exact revision, environment, coverage, rule counts,
query statistics, contract totals, expected/observed fixture results, reviewed
TRUNCATED_WINDOW evidence, checker probes and browser observations. Large HTML
snapshots, the downloaded binary, transient logs and in-memory fixtures were kept
under `/tmp/pathway-audit-20261116/`, not added to the repository. The released
archive checksum and extracted binary hash serve different purposes; the JSON
records the extracted binary hash.

To repeat the graph run, use README's batch command on the recorded revision with
CBM 0.11.0 and Python 3.12+, a fresh cache and no acknowledged baseline. Re-run the
three frozen `benchmark/history/*.yaml` contracts against their recorded revisions,
then compare the maintained A102 packet at the recorded current revision. Do not
compare finding IDs across different CBM project names: qualified names feed IDs.

The application reproductions parsed `db.py`, compiled the unchanged method ASTs
into a fixture class, and supplied `_conn()` backed by in-memory SQLite. No method
body was rewritten and no application import or service startup was needed. Seed:

```sql
CREATE TABLE flow_events (
  id INTEGER PRIMARY KEY, flow_run_id TEXT, event_type TEXT,
  entity_id TEXT, payload_json TEXT
);
CREATE TABLE flow_links (
  id INTEGER PRIMARY KEY, flow_run_id TEXT, entity_type TEXT,
  entity_id TEXT, role TEXT, created_by TEXT
);
```

For the review case, insert 5,001 `spec.review_scored` rows for one Case/spec;
1–5000 use `{"passed":true,"n":<id>}`, and 5001 uses `{"passed":false,"n":5001}`.
Call `latest_spec_review(case, spec)`, then `decompose_case(case, spec,
[{"task_key":""}])`. Repeat with rows 5000 and 5001's `passed` values inverted.
For links, insert 201 distinct session IDs with role `worker`, then call
`list_flow_links(flow_run_id=case, entity_type='session')` without overriding limit.
For the intentional requester control, add running protocol-1 task rows with
`sender_session_id=NULL` for one and then two linked sessions and call
`resolve_dispatch_requester(case, target)`.

The browser opened the generated Python 3.12 HTML snapshot, selected the truncated
review finding in the 2D graph, displayed Coverage and enumerated 280 finding rows
with no page errors. This checks snapshot rendering; live HTTP expansion/source
fetching was not exercised. The final browser process exited 0 after clearing inherited Node IPC environment
variables for that subprocess, matching the startup sanitation in `worker_main.py`.
Two initial attempts had completed page assertions but aborted during Node shutdown;
those were not counted as passing runs. No worker environment was changed. No new HTTP handler, DB migration,
queue consumer or production deployment was introduced by these documentation changes.

## Historical measurement retained from the previous README

Reported on **2026-10-09** for `src/`: 130 routes, 3,077 CBM nodes, about 13.7k
edges. Original machine/revision/raw artifacts were not recorded in that README,
so these values are historical reports, not revalidated comparable measurements:

| Case | Originally reported time |
|---|---|
| Fresh index + depth-4 sweep, 1,216 CBM calls | ~96 s; index 12.8 s, sweep 79 s |
| Fully cached sweep, 1 CBM call | ~3.5–4 s |
| Route selector/server ready | ~0.1 s; background warm stats ~1 s |
| Repeated expansion | Local cache, 0 CBM calls |

These differ in graph size, depth, roots, operating environment and index lifetime
from the 2026-11-16 run. They do not establish a performance regression or gain.
