# Architecture Pathway Audit — measurements and field experience

This file owns evaluation results, reviewed findings and operational experience.
Usage and requirements belong in [README.md](README.md). Machine-readable data:
[2026-11-16.json](benchmark/measurements/2026-11-16.json). The historical contracts remain
frozen; no detector, contract, application code or baseline was changed for this audit.

## 2026-11-16 — detection attribution and approved follow-up

Execution handoff: [issue #290](https://github.com/nydiokar/AI-team/issues/290) and
[A116](../../../.ai/dispatch/AGENT_116_TRUSTWORTHY_ARCHITECTURE_AUDIT.md).
A116 is ready and approved for autonomous implementation, review, verification,
shipping and certification within its stated scope. No fixes or final certification
are implied by authoring that handoff.

### Which work was done by the tool?

The two confirmed current application defects began with **two candidates emitted
by the tool itself** in an untargeted `src` sweep. The relevant detector is the
**AST SQL shared-medium layer**, not a demonstrated CBM duplicate-execution-path
catch. `pyast_adapter.data_access` mechanically extracts SQL table accesses and
window shapes; the explorer builds table-access edges; `structure.analyze` emits
`TRUNCATED_WINDOW` for limited oldest-N reads against apparently insert-only media.
CBM supplies graph symbols/navigation; source/consumer review supplies semantics.
No agent-created graph edges or known defect names were supplied to the sweep.
This was not a blind experiment: the project already has historical window scars.

| Origin | Exact machine evidence | Additional agent work | Justified conclusion |
|---|---|---|---|
| Tool | `cf15dbf85a47`, `TRUNCATED_WINDOW`, `table::flow_events`; AST READS_TABLE edge at `control/db.py:7289`, reader `list_flow_events_of_types` | Followed its caller `latest_spec_review`, optional-spec filtering and `decompose_case`; seeded 5,001 rows and inverted last verdicts | Real stale-review authority defect. The tool localized the risky reader; it did not know that row 5001 should reject decomposition. |
| Tool | `3a7bd241ebdb`, `TRUNCATED_WINDOW`, `table::flow_links`; AST edge at `control/db.py:6799`, reader `list_flow_links` | Followed default limit 200 into work roster/detail and cost-role projection; seeded 201 links and observed omission | Real incomplete-membership projection defect. The tool did not independently prove UI consequences or lost role attribution. |
| Tool candidate, agent rejection | Same flow_links finding also points to `resolve_dispatch_requester`, line 7923 | Inspected exact-one semantics; tested one and two executing candidates | Correct LIMIT-2 ambiguity detection, not a bug. One candidate can contain both useful and benign leads. |
| Contract checker output, agent interpretation | Frozen A104 example reports 41 occurrences on current main | Compared lifecycle/migration/test readers with the actual pending-state authority | Contract intent/encoding mismatch, not 41 application bugs or a newly proven duplicate inbox authority. |
| Agent adversarial investigation | Persisted checker/index/scorer probes in the baseline JSON | Read implementation and injected missing scope, malformed Python, index failure, qualified SQL and convergence-only scorer input | Real audit-tool reliability/evaluation defects; not automatically detected by the graph rules. |

Both machine candidates explicitly say `level: candidate` and
`semantic_verification: not performed (requires source review by an agent or human)`.
That is the original output, not a later reclassification. The generic sweep
produced neither a certified defect count nor a clean-architecture verdict.

### Original output, not just selected observations

The original full report is now preserved as
[2026-11-16.findings.json.gz](benchmark/measurements/2026-11-16.findings.json.gz).
It is 1,190,254 uncompressed bytes, archived losslessly as 56,501 bytes. The original
run identity, scope, coverage, stats, route list and all 280 emitted findings are
preserved. It does not recover the 418 patterns omitted by the detector's caps;
those omissions were counted in `analysis_scope`, not serialized as full findings.

- Original JSON SHA-256: `02945b003b3517e1e4dd6ce82faf4d7f95004540455227dded9e5a7e03f8a24a`.
- Compressed file SHA-256: `bf040e320eff66630b2caa3b6039a781b6bbad25bb35a17ca58091a071a81ab9`.

Inspect the original output without CBM, a gateway or any paid model call:

```bash
python -c 'import gzip,json; from collections import Counter; p=".claude/skills/architecture-pathway-audit/benchmark/measurements/2026-11-16.findings.json.gz"; d=json.load(gzip.open(p,"rt")); print(Counter(f["level"] for f in d["findings"])); print(json.dumps([f for f in d["findings"] if f["kind"]=="TRUNCATED_WINDOW"],indent=2))'
```

Expected level counts: **200 candidates, 80 facts**. The two window IDs and source
edges above must be present. The 698 uncapped structural patterns are not 698 bugs;
280 emitted items are not 280 bugs; the two confirmed defects divided by 280 is
not a measured precision because the full denominator was not semantically reviewed.

Exact configuration, executed from the isolated `/audit` checkout:

```bash
python .claude/skills/architecture-pathway-audit/scripts/pathway_explorer.py --repo . --scope src --project ai-team-audit-py312 --cbm /evidence/bin/codebase-memory-mcp --cache-dir /evidence/cache-py312 --out /evidence/evidence/pathways-py312.html --report /evidence/evidence/findings-py312.json --no-serve --sweep-depth 6 --sweep-cap 600 --roots all
```

Relevant original stdout:

```text
[pathway] CBM index ready in 9.4s: 3433 nodes, 15061 edges
[pathway] Sweep depth 6: 766 roots, 12454 node visits, 1988 expansions fetched, 10466 cache hits, 0 truncated, 122.3s
[pathway] Wrote /evidence/evidence/pathways-py312.html and /evidence/evidence/findings-py312.json: 2112 nodes, 6299 edges, 280 findings, 4539 CBM calls, total 152.3s
```

**Biggest demonstrated value of this run:** a generic mechanical scan localized
bounded durable-ledger readers whose downstream decision/projection consumers
were incorrect. That produced two reproducible application defects and a benign
control, rather than only diagrams or a summary of already-known symbols. The
historical deleted-import contract is separate evidence of deterministic cutover
checking. Neither demonstrates calibrated detection of every architecture class.

### Repeat execution during the issue/dispatch handoff

The tool was run again during this follow-up on source/detector code unchanged
from the recorded baseline. A fresh network-isolated Python 3.12 container reused
the explorer cache and reindexed its disposable CBM store. It completed in **22.6 s**
(1.5 s sweep), fetched zero expansions, used 10 CBM calls and again emitted **280
items: 80 facts and 200 candidates**. The entire normalized finding payload, route
inventory, coverage and analysis scope matched the original report, including both
window candidate IDs. This establishes observed repeatability on the same input;
it does not repair the known false-green/scoring counterexamples.

[2026-11-16.recheck.json](benchmark/measurements/2026-11-16.recheck.json) records
the exact command, source-tree IDs, timestamps, result hashes, equality assertions,
stats and limitations. The full original findings are in the compressed report
above; the new report only changes run-specific metadata/statistics.

### What can be trusted now, and what must A116 establish?

Currently the tool is functional as an evidence-producing candidate generator and
explicit static-contract checker. Its blanket success/clean-result interpretation
is not dependable: the baseline records false-green and false-catch counterexamples.
A successful cold run and 54 passing tests do not remove those counterexamples.

A trustworthy future result must independently report: did the requested checks
execute; what source/capabilities/limits were covered; how many facts/candidates
exist before/after caps; which items were source-verified, benign, known or still
unreviewed; and whether required evidence is unavailable. A complete successful
zero means no matching findings for those declared checks. A partial zero, failed
index or stale report must be unmistakably noncertifying. A 101-item result must
remain attributable/countable with its omissions/review state visible, never be
presented as 101 defects by default. A116's C1–C12 matrix makes these properties
executable acceptance conditions and separately requires repairing the two
application readers and proving their downstream behavior.

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
| High for automated closure — **fixed 2026-11-26** | `Contract(scope=['typo'], retired=['old_send'])` scanned 0 files and returned `ok=true`; `check` made success depend only on violations | A misspelled scope could produce a vacuous green gate. Now `status=incomplete`, exit 2. |
| High for automated closure — **fixed 2026-11-26** | Syntax-invalid Python containing a forbidden SQL read was text-scanned, lost all store accesses, and returned `ok=true` | Coverage errors could bypass a passing ownership check. Binary/oversize skips likewise did not fail closure. Now both are required-analysis errors, exit 2. |
| High for batch consumers — **fixed 2026-11-26** | Inject `CBMError` from `ensure_started`, invoke default `--no-serve` without a sweep: CLI returned normally with zero findings | `ready_error` was logged but omitted by `persist` from JSON; pending analysis could be mistaken for success. Now JSON/HTML/CLI carry one `run` status and exit 2. |
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

### Stage fix verification — 2026-11-26 (A116 P2, T1/T2 slice)

The frozen executable witnesses now live in `tests/test_failure_honest.py` plus
`tests/test_dispatch_state.py::test_done_is_refused_when_the_contract_scope_is_incomplete`.
They cover the vacuous-scope gate, unparseable and skipped required inputs, the
index-error `--no-serve` path, row-capped expansions, corrupt/mismatched cache and
the preserved 0/1 contract checker meanings. The old defects above remain recorded
as history; the first three rows are fixed and merged to `main` as `8d5d2b7` via PR #304
(branch `feat/a116-failure-honest-audit`).

- Baseline, detached worktree at `49c4dfa` with only those witness files copied in:
  `python -m pytest .claude/skills/architecture-pathway-audit/tests/test_failure_honest.py
  tests/test_dispatch_state.py --no-header` → **14 failed, 9 passed in 3.16s**. The
  dispatch witness printed `CONTRACT MET` after scanning 0 files and flipped the job
  to `done` — the original vacuous-green gate, reproduced.
- Candidate (merged commit `8d5d2b7`): `python -m pytest .claude/skills/architecture-pathway-audit/tests
  tests/test_dispatch_state.py --no-header` → **76 passed, 1 skipped in 2.97s**
  (Python 3.12.3, isolated venv with project `test` extras). No CBM binary, worker
  action, paid turn or production data was used.
- Not certified by this slice: F1/F2 application repairs (P1), T3 SQL extraction,
  T4 semantic cache/result parity, T5 scoring/corpus, live browser C7 and C1–C12
  as a whole. Preserve the threshold fixtures above for that follow-up; a blanket
  newest-first change would break intentional oldest-first/ambiguity behavior.

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
