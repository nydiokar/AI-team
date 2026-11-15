# Recurring code-quality failures and prevention controls

**Date:** 2026-11-15. **Evidence baseline:** `main` at `c26c4da`.
**Status:** assessment and proposed controls; no runtime refactor or new CI enforcement delivered by this document.

## 1. Intent, scope and decision

Stop defective implementation patterns becoming the examples that subsequent agents copy. Repair concrete defects, make important architectural invariants executable, and verify that both application changes and the controls themselves fail honestly. Formatting is useful hygiene; it cannot establish database coherence or architectural equivalence.

The six failure families below were selected for demonstrated impact, recurrence in repository history, and breadth of prevention. They are not frequency estimates: there is no complete incident dataset. Evidence distinguishes current defects, historical incidents already corrected, and risks requiring further measurement. The sweep covered core Python modules and SQL entry points, session stores, orchestration, backend/worker contracts, HTTP resource boundaries, frontend transport, dependency/CI configuration, architecture tooling, and relevant dispatch/history. It is not an exhaustive security, package-vulnerability, or production-load audit.

**Recommended direction:** strengthen the existing data-access and ownership seams, then enforce narrow, tested contracts in CI. Use semantic review to discover missing contracts, not as the sole enforcement of known ones. Keep the graph explorer advisory until its candidates are verified and converted into explicit contracts.

This is a control/refactor design, not authorization to rewrite storage, retire file-only recovery, change credentials, migrate databases, or restart workers. Its execution order does not replace the project's priority board in `.ai/CONTEXT.md`.

## 2. Evidence and strengths worth preserving

The project already has durable queue admission, carrier-owned `TurnControl`, controller authority, a single agent inbox, bounded mutation permits that survive HTTP cancellation, compact read projections, shared read snapshots, strict TypeScript options, and paid-call test guards. Improve these mechanisms rather than introduce alternatives.

| Evidence | Current code or history | What it establishes |
|---|---|---|
| E1 | [`MeshDB` connection/transaction contexts](../src/control/db.py), `_conn`, `read_snapshot`, `_write`, `_managed_write` | A canonical access policy exists, with thread-local connections and different legacy/managed lock budgets. |
| E2 | `db.py:list_stale_claims`, `list_stale_managed_children` | Readers create fresh raw connections outside `_conn`; one comment explicitly cites behavior across tests as the rationale. |
| E3 | `db.py:close_case`, `update_flow_run`, `append_flow_event` | Status and required closure evidence commit separately. Injecting an event failure leaves a closed Case without its closure event; retry does not repair it. |
| E4 | `db.py:_case_has_unresolved_approval` | A database exception becomes `False`, indistinguishable from no outstanding approval. Reproduced with an injected `sqlite3.OperationalError`. |
| E5 | [`SessionStore`](../src/services/session_store.py), `save`, `_shadow_write`, `get`, `read_summaries` | Session records still have JSON-first writes, best-effort DB mirroring, and DB-first reads; some newer readers expose fallback provenance. |
| E6 | [`A104 guards`](../tests/test_a104_single_pending_source.py); A84/A104 dispatch history | Existing AST ownership/retirement checks work; competing pending-state stores previously caused repeated wake/withdraw cycles. |
| E7 | [`A102 contract`](../.ai/dispatch/AGENT_102_UNIFY_BACKEND_PATHWAYS.md); A88 authority and September contention corrections | Backend twins, worker-local controller state, duplicate telemetry writes and synchronous per-Case reads caused real operational failures; much has been corrected. |
| E8 | [`restart tests`](../tests/test_a98_restart_reconcile.py), `_patch_nodes`; `orchestrator.py:_detect_node_restarts_once` | Tests create missing `get_db` with `raising=False`. Seven tests pass while directly invoking the method reproduces `NameError`. |
| E9 | [`session routes`](../src/control/routes/sessions.py), `api_keep_session`, `api_set_effort`, `api_upload_file`; [`body-cap registration`](../src/control/control_api.py), `_preparse_byte_guard` | Whole-body/file reads precede actual byte checks on routes outside the existing streamed cap rules. |
| E10 | [`useEventStream`](../web/src/hooks/useEventStream.ts), [`eventDedupe`](../web/src/transport/eventDedupe.ts), [`eventAdapter`](../web/src/transport/eventAdapter.ts) | Ignored raw events enter the dedupe set but do not enter the rendered log whose eviction removes keys. Actual helpers retain 10,000 keys for 10,000 heartbeats and zero rendered events. |
| E11 | [`CI`](../.github/workflows/ci.yml), [`pre-commit`](../.pre-commit-config.yaml), [`pyproject`](../pyproject.toml), [`constraints`](../constraints.txt) | CI runs Python tests only; local lint/type hooks use versions different from constrained tools and configure two formatters. |
| E12 | [`response model coverage`](../tests/test_response_model_coverage.py), [`response primitives`](../src/control/routes/_responses.py), [`API client`](../web/src/transport/apiClient.ts) | Success-schema presence is tested, but direct `JSONResponse` bypasses runtime response-model validation and frontend `as T` is a type assertion. This is a coverage limit, not proof of a specific malformed response. |
| E13 | [Architecture skill](../.claude/skills/architecture-pathway-audit/SKILL.md), its scripts/tests; [`dispatch_state`](../scripts/dispatch/dispatch_state.py) | Deterministic end-state checker and graph candidate explorer exist; enforcement and freshness details are in section 5. |
| E14 | [`SessionService.set_keep`](../src/services/session_service.py), `SessionStore.save`, `db.py:upsert_session` | Real isolated service/store/DB reproduction: a keep edit saves a stale whole session and changes a concurrently closed session back to idle. Earlier upsert exclusions already protect other fields from this same class of race. |
| E15 | `db.py:list_stale_managed_children`; `orchestrator.py:_reap_lost_carriers` | Real temporary DB reproduction: 25 older healthy candidates hide a newer lost child on repeated default-size scans; increasing the limit to 26 reveals it. This proves a fairness defect at that shape, not a production incident. |
| E16 | GitHub branch/ruleset read on 2026-11-15; local `.git/hooks/pre-commit` | Branch API reports `main.protected=false` and the repository ruleset list is empty. The installed local pre-commit hook only renders dispatch state and warns; the Ruff/Black/mypy configuration is not evidence that those hooks are installed or mandatory. |
| E17 | `sessions.py:api_keep_session`, `api_set_effort`; `turn_requests.py:api_create_turn_request` | Async handlers still call synchronous session services/store reads inline. These operations can reach filesystem/SQLite work. Offloading elsewhere does not protect these call sites. |

The 2026-11-15 audit's offline checks passed: 11 A104 single-source tests, 17 scheduler tests, seven restart-reconcile tests, three frontend dedupe tests, TypeScript checking, and `pip check`. A102's current end-state contract passed across 414 files with seven waived occurrences. These results coexist with E3/E4/E8/E10: passing tests do not imply complete fault coverage. The TypeScript command needed inherited `NODE_CHANNEL_FD` and `NODE_CHANNEL_SERIALIZATION_MODE` removed from that child environment; application code was unchanged.

## 3. Six failure families

### P1. Database operations lack a consistently owned connection, snapshot and transaction scope

**Impact: high. Current defects plus repeated historical corrections.** This is a distinct failure family, not merely a symptom of a large file.

The access contract varies by caller and helper. An individual query may work while the operation around it has inconsistent snapshots, independent commits, a different busy timeout, an implicit fallback, or excessive query fan-out. Adding another connection or wrapping another call in `to_thread` can hide a symptom without repairing the operation.

**Current diagnosis:**

- `_conn` deliberately caches a connection per thread. Separate connections for different threads/processes are legitimate. Sharing one mutable connection globally would break the intended isolation and is not the proposed fix.
- `read_snapshot` owns a thread-local snapshot, progress handler and temporary busy timeout. Related reads must execute in the same synchronous worker invocation. Opening the context on the event-loop thread and issuing separate `to_thread` calls does not carry that snapshot to executor threads.
- E2's raw connections bypass `_conn`'s per-connection foreign-key, timeout, cache and transaction setup, and do not inherit its progress handler/snapshot. WAL journal mode is persistent; this is not a claim that those readers necessarily disable WAL. Explicit close occurs only on the success path; exception cleanup is not structurally guaranteed. The stated test workaround needs a real isolation/transaction reproduction rather than a production bypass.
- `_write` waits on an instance lock and retries SQLite BEGIN; `_managed_write` has a shared acquisition budget and different rollback handling. Having two contexts is not automatically wrong, but applying them by precedent rather than operation policy creates inconsistent failure behavior. An instance lock is not a cross-process mutex, and multiple `MeshDB` objects have different locks.
- E3 checks invariants before independently committing the terminal status and audit record. Concurrent changes can invalidate prerequisites; a later event failure leaves irreversible partial progress under current retry behavior.
- E14 is the recurring second-order repair pattern in concrete form: `upsert_session` already protects affiliation, role boot, lineage and enrollment fields against stale saves, but a keep metadata command still rewrites unrelated status/model/driver/task fields. In an interleaved real-store reproduction, `set_keep` returns success and overwrites a concurrent close with the previously read idle state. Protecting one more status value would leave the general lost-update pattern in place.
- E5 can acknowledge a file save after its canonical DB mirror fails. A later DB-first read can return the older DB row. This is an authority/recovery contract to resolve deliberately, not permission to delete the supported JSON fallback.
- E15 applies `LIMIT` before the shared Python staleness predicate. Repeated scans can reconsider the same healthy prefix and starve eligible later rows. There is no demonstrated fleet-wide invariant restricting candidates below the batch size. Raising the limit only moves the threshold.
- Historical N+1/per-Case event reads and telemetry/control-plane lock contention are established in the September corrections. New batched reason readers and snapshots are counterexamples to preserve. Importing `sqlite3` alone is not evidence of unauthorized access; legitimate private stores exist.

**Why agents reproduce it:** `db.py` exposes both low-level contexts and many complete operations in one large module. A caller can compose convenient public methods without seeing their independent commits. Raw access, best-effort mirrors and test-specific workarounds remain copyable examples. The correct unit of work is rarely visible in the function signature.

**Immediate work:**

1. Map actual connection creation, database identity, process role, thread ownership, close behavior and PRAGMAs. Inventory operations rather than count imports. Classify controller DB, quota cache, provider ownership store, repair tools and tests separately.
2. Investigate E2's alleged stale-read condition with a bounded file-backed reproduction attempt. If no original failure can be reproduced, record the historical explanation as unproven and test canonical freshness/isolation/cleanup directly; do not block a justified repair indefinitely on that comment. Replace the bypasses with the canonical policy or an explicitly justified fresh-read factory. Preserve each reaper's selection semantics and verify failure cleanup.
3. In A111's existing closure seam, put prerequisite checks, the conditional terminal transition and required event in one transaction. Classify inbox/wake cleanup as either part of that transaction or a durable recoverable effect. Do not call methods that start nested write transactions while holding `_write`'s non-reentrant lock.
4. Fix E4's unavailable-to-clear fallback. Return a typed unavailable result or raise; callers must refuse a correctness-sensitive transition until the prerequisite is known.
5. Characterize E5 with an injected mirror failure. Make the persistence result honest, with a durable reconciliation strategy if dual writes remain. Preserve intentional file-only mode until its authority contract is explicitly changed.
6. Give metadata commands targeted writes of fields they own; audit keep/model/effort callers of whole-session persistence. Transfer field ownership in both directions: after a targeted keep/model write, a later generic upsert must not restore stale values for those fields. Inventory writers before removing fields from upsert, and preserve insert seeds, intentional lifecycle changes and JSON export/recovery ordering. Test stale metadata → concurrent close, targeted metadata → later stale lifecycle save, and stale JSON rehydration/reconciliation. Use revision/CAS where a decision depends on previously read state, rather than rejecting all independent field edits.
7. Repair E15 through eligibility-aware selection or bounded keyset traversal with a rotating/recoverable cursor. Retain the shared staleness predicate. For a keyset design define ordering/wrap, maximum rows/bytes/queries per pass, and a maximum number of passes to inspect a fixed existing candidate set; continuing arrivals must not reset progress over that set. Test cursor recovery and changing eligibility. A cursor reset on every pass, increasing the limit, or an unbounded full scan would reproduce the original trade-off in another form.

**Prevention:**

- `DB-CONNECT-OWNER`: allow resolved connection constructors only in declared store factories and explicitly classified tools. Catch aliases; scan relevant scripts and entrypoints too. A migration/test exception must not become an exception for arbitrary code in `db.py`.
- `DB-SQL-OWNER`: restrict writes to selected authoritative tables and fields to their declared modules/operations. Reuse the architecture contract scanner where accurate; migration DDL and legitimate readers need explicit treatment.
- `DB-FIELD-OWNER`: prevent designated metadata operations from calling generic whole-record persistence; require targeted update seams. The current table-ownership checker does not yet provide column ownership: implement that increment explicitly with alias/caller fixtures, rather than claim table restrictions prove it.
- `DB-SCOPE`: review one synchronous operation as the scope for a read snapshot or write transaction. Static analysis can flag known raw calls; fault/concurrency tests establish actual atomicity and thread scope.
- `ASYNC-BLOCKING`: resolve known synchronous DB/store calls inside async handlers/timers, including E17. Offload one bounded complete operation with its snapshot, admission and serialization policy, not independent reads that can land on different threads. Aliases and wrappers need test cases; an unrecognized dynamic receiver is a coverage limitation, not a clean result. Verify event-loop responsiveness while a controlled backing-store operation stalls.
- For important list/aggregation operations, assert query growth across representative cardinalities: constant queries or bounded chunk growth, e.g. `O(ceil(N/500))`. Assert access plans only for known hot queries and representative data. Do not ban all scans, `SELECT *`, or queries inside loops by syntax alone.
- Test commit failure, error after an intermediate write, retry, competing writers, stale-object saves, snapshot consistency, cancellation and connection reuse after failure. Include `batch_size + 1` rows with healthy leading candidates, multiple passes and later eligible arrivals. Count connection creation and queries in isolated fixtures; benchmark latency separately from deterministic correctness tests.

**Later decision:** migrate query families behind coherent internal boundaries if that simplifies ownership; split a family only when it has an owner and a retirement plan. SQLAlchemy, Alembic or Postgres may address specific maintenance/contention needs, but do not automatically create correct transactions, prevent N+1 queries or make dual writes atomic. Measure before choosing a database migration or pool. A pool also needs capacity, checkout timeout, lifecycle and cancellation semantics.

**Acceptance:** named high-risk operations use one declared database identity and scope; closure failure cannot leave terminal status without required evidence; retries converge; snapshots survive related reads; legitimate thread/private-store use remains possible; query growth has an explicit bound.

### P2. New features add competing owners instead of extending or replacing an established operation

**Impact: high. Repeated and largely corrected in A102/A104/A88; regression risk remains.** Examples include backend natural/managed twins, three answers to pending work, and worker-local controller flags. Similar code is not necessarily duplication: separate providers or display projections can legitimately share downstream components.

**Root cause:** feature acceptance tests prove the new path works but do not prove the old authority is gone, that every caller migrated, or that each operation has one owner. Transitional shims and rollout flags become indefinite architecture.

**Immediate work:** persist existing A102/A104 ownership and retirement assertions as ongoing checks. For each new cutover, require a bounded map: existing operation, canonical owner, migrated callers, retired symbols/stores/flags, compatibility exceptions and their exit criteria. Read the existing implementation before creating a second service/helper.

**Controls:** `ARCH-OWNER`, `ARCH-RETIRED`, `ARCH-LAYER`; declaration-to-registration checks for backend/role/tool additions; semantic comparison of suspected duplicates before writing a prohibition. Introduce a durable contract only after deciding what is equivalent and what must differ. Graph overlap or clone similarity stays advisory.

**Acceptance:** a forbidden second writer or retired dispatch string fails CI even under a new PR unrelated to the original job. A legitimate backend variant with a shared core passes. No second queue, reviewer or state registry is created to enforce this rule.

### P3. Failure, uncertainty and degraded state are converted into success-shaped data

**Impact: high. Current E4/E5 and historical status/telemetry corrections.** Variants include exception-to-empty-list, unavailable-to-no-pending, stale fallback presented as live truth, and acceptance inferred from activity count or prose. Best-effort notification isolation is legitimate and must remain distinct from authoritative transitions.

**Root cause:** boolean and empty-container return types collapse absence and failure; broad catches are copied without considering the caller's decision. Outcome text, durable execution, UI state and review acceptance have different meanings but sometimes share convenient labels.

**Immediate work:** classify core call sites as authoritative guard, durable mutation, recoverable read, or disposable side effect. Start with approval/closure, persistence acknowledgements and reaper reads; a failed reaper query must be observable as unavailable rather than an ordinary empty scan. Reuse newer read provenance (`source`, `partial`, `unavailable`) and typed queue errors.

**Controls:** `GUARD-UNKNOWN` checks designated functions for prohibited exception/default paths; typed result handling and exhaustive tests cover consumers. Expand failure matrices where the same durable fact reaches session state, telemetry and UI. Preserve structured provider error fields; do not reclassify arbitrary agent prose as provider failure. A text linter cannot establish that a review proves the objective: A111's evidence-bound acceptance and semantic review remain necessary.

**Later work:** remove broad ambiguous return types at affected boundaries incrementally. Use Pydantic result models at validation boundaries where the repo already does so; do not migrate every internal data structure for style alone.

**Retry uncertainty:** an exception or client timeout is not proof that nothing committed or executed. Preserve operation identity, inspect durable receipts/state and distinguish retryable-before-effect from unknown-after-effect. Do not replay an entire mutation or backend call speculatively after `TypeError`/transport failure. [`call_backend`](../src/core/backend_call.py) is an existing positive example: it inspects optional keyword support before a single invocation rather than retrying a possibly executed call. Test response loss after commit and process death between action and acknowledgement.

**Acceptance:** injecting a backing-resource failure never grants approval or returns an unqualified successful mutation. Supported recovery remains visibly degraded, and notification failure does not roll back already committed work.

### P4. Bounds and cleanup protect the wrapper rather than the actual resource

**Impact: high at load; current ingress/dedupe defects and historical cancellation corrections.** E9 bounds bytes after allocation; E10 bounds rendered events while raw identities accumulate. A109 previously showed that cancelling an HTTP waiter could release a permit while the DB operation continued.

**Root cause:** the lifetime being measured differs from the lifetime consuming memory, threads, connections, processes or locks. A timeout on a coroutine is mistaken for termination of native work; a row limit is mistaken for a byte limit.

**Immediate work:** put E9 under the existing streamed body-cap policy before parsing/whole-file allocation; explicitly size upload concurrency, deadline and staging cleanup. Bound E10's raw identity retention independently of rendering. Reuse `run_turn_mutation_async` and existing admission pools when the operation belongs to that resource class; do not add a semaphore per request or a competing capacity counter.

**Controls:** `BOUNDARY-POLICY` inventories actual routes/receivers and requires explicit request-byte, read-deadline, executing-work, response/cardinality, cancellation, memory and dependency-failure policies, or a concrete reviewed deferral. A declaration is not proof: test chunked payloads, slow readers, 100 simultaneous callers, cancelled waiters, oversized historical records, ignored events and full queues. Verify maximum executing work, not just coroutine count. For changed entrypoints apply all six service-boundary checklist items.

**Later work:** size remaining historical-byte/native-I/O deferrals using measured workloads. Use bounded process/thread supervision and cleanup where relevant. Do not promise that `asyncio.wait_for`, SQLite progress handlers or socket timeouts impose a hard native wall-clock deadline.

**Acceptance:** cancellation cannot escape execution capacity; memory grows according to an explicit bound; malformed/oversized input is rejected before large allocation; cleanup follows actual completion. Per-process limits must be reported as per-process, including their aggregate across gateway/task-server processes.

### P5. Verification repairs or substitutes for production wiring, while basic checks remain optional

**Impact: high. E8 is a reproduced false-green test; E11 leaves simple errors unblocked.** Mocks can invent missing names; tests can bypass the layer they claim to verify. Documentation-only response models and `as T` assertions are useful declarations, but neither validates received bytes (E12).

**Root cause:** success of the test harness becomes the acceptance target. Permissive fakes hide imports, API shape or environment mismatches. Local hooks are treated as enforcement despite not being required CI checks.

**Immediate work:** repair the missing production dependency and make its test patch an existing attribute. Prefer constrained fakes/specs and real temporary DBs for persistence behavior. Make Ruff the one formatter, as requested; align the constrained tool version across CI and local hooks and remove Black as an active formatter in that implementation PR. Start with high-confidence correctness lint (`F821` after triage), selected bug rules, scoped typing, frontend typecheck and focused tests. Do not enable thousands of unrelated warnings as a blocking migration. E16 shows that adding a CI job alone does not make it mandatory: verify required statuses and bypass permissions at the repository boundary.

**Controls:** `TEST-NO-INVENTED-ATTR` rejects `raising=False`/`create=True` against application modules unless specifically justified; environment and optional-attribute tests remain legitimate exceptions. Add mutation fixtures proving that missing imports, bypassed guards and broken response payloads make the relevant checks fail. Test representative actual JSON responses against their declared models without silently filtering compatibility fields; evaluate runtime validation only where its behavior/cost is understood.

**Later work:** broaden typing and error rules as named modules become clean. Align project declarations, constraints, build/runtime Python, formatter and frontend toolchain. Coordinate existing dependency draft PRs #282/#286; do not start a parallel upgrade. Dependency consistency, API compatibility, vulnerability assessment and production activation are separate evidence claims.

**Acceptance:** existing missing-name wiring cannot be manufactured by a test; deliberate broken fixtures are rejected; local/CI formatter output agrees; new findings cannot hide behind a count-only baseline or broad file exclusion.

### P6. Configuration, contracts and completion claims stop before the real consumer

**Impact: high. Repeated cross-process incidents and active acceptance gaps.** A88 fixed flags read from the wrong machine's DB; A49/A53 found correct-looking gateway changes inert at the remote consumer; A98 required lost-driver recovery; A113's proof deliberately leaves actual SDK-budget consumption to A114. Stale orientation/ledger text is another way an old implementation becomes a false premise.

**Root cause:** implementation and verification follow file/layer boundaries instead of the user-visible effect. A flag's existence or green gateway test is taken as evidence of worker behavior. Revisions, process identity, deployment state and consumed configuration are not consistently part of acceptance.

**Immediate work:** for changes to execution/configuration, trace input → validated authority/value → durable record → transport → worker → backend invocation → result/projection → UI where applicable. State verified and unverified seams. Use distinct-root/controller-worker fixtures and assert the exact consumed payload/revision. Keep code merged, gateway deployed, worker loaded and natural outcome accepted as separate claims. Reconcile only the active guidance touched by the work, preserving historical records.

**Controls:** known forbidden worker DB access is deterministic; consumer payloads/revisions, restart/outage/compatibility behavior and actual response conformance need verification. Semantic review asks whether the evidence reaches the objective, whether a default changes owner intent and whether an undeclared competing pathway exists. It must produce source-backed findings, not a blanket approval score. Reuse A111/A114 and existing ownership surfaces.

**Acceptance:** changing a controller value reaches the intended consumer under the supported deployment shape; stale/unavailable state has explicit behavior; a gateway-only test cannot certify worker activation; no paid proof turn or worker restart is manufactured to satisfy a document.

## 4. Six implementation moves, in practical order

| Order | Move | Concrete first increment | Required evidence before expanding |
|---|---|---|---|
| 1 | Close the verification holes | P5: strict test targets, repair E8, constrained Ruff correctness gate; choose Ruff formatting; frontend typecheck | Broken import/mock fixtures fail; current debt is individually visible; no mass reformat |
| 2 | Restore DB operation coherence | P1: inventory connection policies; fix raw reaper bypasses; coordinate atomic closure/failure semantics with A111 | File-backed rollback/retry/race tests, restored connection state, explicit query-count bounds |
| 3 | Enforce persistent ownership and retirement | P2: harden and run existing contracts continuously | Known bad and legitimate fixture corpora; exact-revision and coverage validation; no graph service required |
| 4 | Make unknown results explicit | P3: repair guard/persistence/reaper outcomes and their consumers | Dependency failures cannot yield permission, unqualified success or hidden empty results |
| 5 | Bound actual work and state lifetimes | P4: streamed caps, upload policy, raw dedupe cap, cancellation/resource tests | N=100 executing-work and memory bounds; slow/chunked/malformed/failure tests |
| 6 | Prove changes at the consuming boundary | P6: distinct-process/root and exact-payload fixtures; active guidance checks | Controller mutation reaches worker/backend; actual response matches contract; activation limits stated |

These are incremental PRs, not a six-part rewrite. Move 1 can start immediately; confirmed high-impact DB/guard repairs should not wait for complete lint adoption. Closure behavior belongs in the existing A111 operation, and consequential budget consumption in A114. No work here requires waiting for Postgres, a new agent framework, a general registry, or new paid model calls.

## 5. Architecture skill: what is enforced, freshness and safe automation

### 5.1 Two different tools

| Tool | What it does now | Current enforcement | Recommended role |
|---|---|---|---|
| `scripts/contract_check.py` inside the skill | Reads selected source at a revision; checks declared retired names, layering bans and SQL store owners | `dispatch_state.py --set ... status done` invokes it **only if** that packet has an `arch-contract` block. Not a continuous CI gate. Existing A104 tests independently enforce selected boundaries in ordinary pytest. | Blocking only for explicit, validated contracts after coverage hardening |
| `pathway_explorer.py` + `structure.py` + CBM | Builds/queries a source graph, discovers routes and emits overlap/twin/bypass/store candidates over expanded neighborhoods | Optional CLI/skill use; no CI invocation | Advisory discovery with visible coverage, semantic verification and reviewed acknowledgements |

The deterministic checker does **not** need the CBM graph or reindexing. The graph explorer is not a whole-codebase proof, even when no candidate is reported. Its README acknowledges dynamic calls, ambiguous methods, unresolved callbacks, route limitations and expansion scope.

### 5.2 Coverage and correctness hardening before blocking

The current checker can produce no violations while a scope matches no files or files are skipped/degraded. Its literal-SQL extraction is incomplete and can confuse SQL-like documentation with actual access. A reported pass means no declared violation was found in what it recognized, not that all intended code was checked. Five offline counterexamples were reproduced using the actual checker (synthetic source loader for the source cases; repository lookup for nonexistent scope):

| Input | Current result | Required interpretation |
|---|---|---|
| Nonexistent required scope | `ok=True`, zero files | Coverage error |
| Malformed Python under a store rule | `ok=True`, parse error recorded | Not analyzable; cannot certify store ownership |
| Python source larger than 2 MB | `ok=True`, skipped source recorded | Coverage error unless explicitly excluded |
| A docstring containing `SELECT * FROM agent_inbox` | Ownership violation | False positive; no executed SQL |
| `c.execute("SELECT * FROM main.agent_inbox")` under an `agent_inbox` reader ban | No ownership violation | False negative; normalize schema-qualified table names |

Required hardening:

1. Validate each declared scope and owner target; distinguish intentionally empty sets from missing paths. Unexpected zero matches, unparseable in-scope Python and oversize/binary skips must be errors or explicitly reviewed exclusions. Report coverage per rule, not only total file count.
2. Define SQL extraction coverage: aliases, literal constants, schema-qualified/quoted identifiers, CTEs, f-strings and dynamic construction. Scan executable SQL call sites or report unresolved accesses; do not treat docstrings/log messages as table access. Unsupported SQL cannot become a silent clean bill of health.
3. Run contracts against the exact candidate revision in CI. Use full relevant scope for ownership checks; changed-line-only checks miss moved writers and untouched callers. Preserve documented negative-test/migration exceptions without waiving complete application modules.
4. Protect contract integrity: show contract/baseline/waiver changes separately, require reasons and review, detect removed or weakened invariants, and fail if tooling errors. Bootstrap new assertions against their parent revision and the candidate; do not let a PR delete its guard and call itself compliant. The trusted base revision supplies existing contracts, checker and baseline; run that checker against candidate source as data, without importing the application. Run candidate checker tests and new-rule tests separately. Changes to contracts, checker, workflow, exclusions or baseline are policy changes requiring independently recorded review, not self-certification by the modified check. The authoritative required check must not be replaceable by a same-named candidate job. Pin the trusted execution source and restrict who can change required-status/bypass settings; this is protection against routine bypass and agent mistakes, not a claim that a repository administrator cannot override it.
5. Collect the skill's own checker tests explicitly: they live under `.claude/skills/.../tests`, outside pytest's default `tests/` collection. Add fixture mutations for known bypasses and legitimate cases before promotion. Tool tests should require neither CBM nor a running app for deterministic rules.

### 5.3 Index and cache freshness

There are separate indexes: the repository ctags symbol index, CBM's graph index, and explorer neighborhood/baseline caches. Refreshing ctags does not refresh the graph or enforce contracts.

The explorer scans on startup, not continuously while its viewer stays open. It tracks source metadata/content and reindexes changed scope; its size/mtime fast path can miss same-size edits with preserved timestamps. An offline reproduction changed `def before` to `def after_` while retaining file size/mtime: `sync_files()` reported no changes and retained the old AST definition. This is a cache-validity edge case, not a claim ordinary editor saves usually fail.

Validate deletion, rename and newly introduced edges as well as modified files. For CI or an authoritative assessment, fingerprint the complete scoped source contents plus tool version, configuration and graph schema; emit that fingerprint and revision with coverage. Reindex failure must not publish new freshness metadata over an old graph. Verify cache reuse is observationally equivalent to a cold run on those inputs. Use `--refresh` as an explicit operational option, not as a substitute for a trustworthy freshness contract. Worker/non-HTTP roots require `--roots all`; record the roots, sweep depth and caps in every graph report.

### 5.4 False positives and promotion policy

Do not fail PRs because two methods have similar bodies, share a table, share callees or diverge/rejoin. Legitimate adapters, projections, validation gates and read/write variants can have those structures. A candidate becomes a blocking rule only after source comparison establishes the prohibited behavior and a positive/negative fixture pair demonstrates it.

Replay independently labeled historical cases (A102/A104 are available seeds), add unrelated legitimate examples, and retain unknown/unresolved labels rather than force a binary verdict. Evaluate misses as well as noise. A reviewed baseline acknowledges a specific fingerprinted finding; it must expose stale entries and changed evidence. The current `--baseline-update` bulk-acknowledges all current candidates with a generic reason/blank owner; that is bookkeeping, not evidence of individual review. Rebaseline is not an automatic acceptance operation.

The independent tooling sweep ran 53 focused offline unittest cases: contract 15, pathway 22, medium 6, sibling 4 and twins 6. All passed; the counterexamples above are missing coverage to add. No current whole-repo graph sweep or real-CBM integration test was run, so this assessment does not certify the installed CBM version or graph completeness.

**Answer:** the explicit contracts can be enforced properly after targeted hardening; the full graph candidate list should not be made a blocking linter. Enforce known invariants deterministically and use the graph to propose the next ones.

## 6. Building controls without reproducing the same failures

- **One implementation per control.** CI, local hooks and agent skills call the same checker and configuration. Do not duplicate a rule in a shell grep, an AST scanner and an agent prompt with different exceptions. Existing behavioral tests can exercise the same invariant independently.
- **Precise baselines.** Identify debt by rule, symbol/location and evidence fingerprint; normalize moves carefully. A PR cannot remove one old warning to make room for a different new one. Avoid blanket `exclude`, whole-file ignores and “all findings currently present” acknowledgements.
- **Check-only CI.** Ruff checks and `ruff format --check` do not edit the checkout. Migrate formatting in bounded owned slices; choose a scoped adoption policy and enforce that it cannot shrink silently. Do not turn a correctness patch into a repository-wide reformat.
- **Required means required.** Verify required check identities, candidate/merge revision coverage, failure behavior and bypass permissions with an intentionally failing PR before claiming enforcement. The current docs-only direct-to-main convention conflicts with a blanket required-PR rule: resolve that policy in the CI implementation job. Do not solve it with a broadly exempt agent/bot that can also push code around the checks. Repository-setting changes are not delivered or authorized by this design document.
- **Test the checker adversarially.** Each rule needs a rejected defect, a legitimate near-match, a bypass/alias/rename case, malformed input, and a bounded failure path. Coverage failures and stale evidence must be distinguishable from rule violations.
- **No uncontrolled tool execution.** Static checks read source and temporary fixtures, not `.env`, live databases or paid agents. Bound traversal, file bytes, parser work and report size. CI failure output is file:line plus rule/evidence; place large diagnostics in artifacts.
- **Scope authority explicitly.** A field/table owner is not necessarily the only reader; a process lock is not fleet capacity; a private SQLite store is not a competing controller DB. Make these distinctions part of each contract.
- **Keep semantic review targeted.** Ask an adversarial reviewer to find a bypass, incorrect assumption, false positive, unverified consumer or simpler existing mechanism. Require evidence and revise the proposal; do not grant a model authority to silently rewrite rules or bless its own implementation.
- **Agent orientation stays short.** Link to the canonical operation/contract and verification command when dispatching related work. Describe replacement/retirement and required evidence in the task. The prompt or skill is guidance; required CI/runtime invariants carry enforcement.

The existing [build-vs-buy plan](backend/BUILD_VS_BUY_MODERNIZATION.md) overlaps CI/storage work but is a proposal from a different baseline. Its examples must be revalidated: imports are not proof of direct controller DB access, and a library/database migration does not eliminate operation-level invariants. A105 is now merged; do not follow the older plan's instruction to pause or re-scope it as if it were unimplemented. This document supplies prevention contracts and acceptance conditions, not a competing modernization roadmap.

## 7. Verification plan and decision boundaries

Before implementing a control, reproduce the target defect on an offline fixture and document what its check cannot observe. After implementation, run only the targeted tests selected through the repository's `running-targeted-tests` workflow. Include checker tests explicitly when their location is outside default discovery. No full/e2e suite or paid proof turns are required by this design.

For DB work use disposable file-backed databases, separate connections/processes and deterministic synchronization. In-memory SQLite alone cannot prove cross-process locking or file-backed WAL behavior. Avoid brittle millisecond performance assertions: query counts, actual concurrent work and state transitions are deterministic; benchmark timings separately.

For every changed handler, verify concurrency, N=100 memory, request-size bounds, timeout behavior, malformed input and backing-resource failures. Written deferrals must remain linked to the relevant task in CONTEXT; a policy file alone does not satisfy the checklist.

Unresolved decisions are intentionally narrow: scope of JSON fallback authority, measured need for database migration/telemetry separation, absolute bounds for native I/O/large history, and semantic criteria for architectural equivalence. Do not make any of these silently as a prerequisite for adding lint.

## 8. Adversarial review record

An independent reviewer checked the draft against source on 2026-11-15. All six substantive challenges were accepted and incorporated:

| Challenge | Revision made |
|---|---|
| Targeted setters leave the reverse stale-save race | P1 now requires field ownership across all writers and both interleavings, JSON export and fallback replay tests. |
| Candidate-controlled CI/contracts can bless their own weakening | P5/section 5 now distinguish required repository enforcement from job presence, require trusted-base evaluation and independent policy-change review; E16 records actual remote/local enforcement state. |
| Existing async DB calls remain an event-loop regression vector | E17 and `ASYNC-BLOCKING` require bounded whole-operation offloading, shared snapshots and a stalled-store responsiveness test. |
| Reaper “eventual” progress is too vague | P1 requires a bound over a fixed candidate set, stable cursor/wrap/recovery behavior and continuing-arrival tests without unbounded scanning. |
| Exception-to-retry can duplicate effects | P3 now specifies ambiguous outcomes, durable receipts, preserved operation identity and response-loss/crash tests; reuses the positive `call_backend` pattern. |
| Reproducing an undocumented “potential” stale-cache issue can become an indefinite prerequisite | P1 now limits that investigation and allows a justified canonical repair with direct isolation/freshness/cleanup tests and an honest unproven-history note. |

The reviewer re-read the revised document and found all six challenges addressed, with no remaining material contradiction blocking staged implementation of the design. The reviewer also independently confirmed E16's remote/local enforcement state.

Reviewer agreement does not prove all unknown patterns are exhausted. Remaining limits are the lack of current real-CBM graph validation, unmeasured production frequency/load, unimplemented checker hardening, and unresolved runtime/authority decisions listed above. This review approves the design's reasoning only; it does not certify runtime fixes or authorize policy changes.
