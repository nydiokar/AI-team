```yaml
job_id: AGENT_88_DATABASE_AUTHORITY_UNIFICATION
created_at: "2026-09-26T16:09:11.750309+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: claude-session-2026-10-02:feat/database-authority-unification
depends_on: []
results_ref: DISPATCH_LOG.md#A88             # -> DISPATCH_LOG.md section with the verdict prose
evidence: docs/backend/DATABASE_AUTHORITY.md,tests/test_database_authority.py,tests/test_database_authority_process.py,scripts/db_authority_report.py                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-01T22:10:17.101887+00:00"
```

# DISPATCH — A88 · Controller/worker database authority unification

**Level:** 3 (cross-process state authority, configuration propagation, migration) · **Type:** investigation + one coordinated remediation
**Authored:** 2026-09-26 · **Status:** ready
**Depends on:** —. Coordinate file overlap with A82/A84 before editing shared task/control paths.
**Branch:** `feat/database-authority-unification` + PR. No production restart, data copy, database deletion, flag activation, or worker deployment without explicit operator approval.

> The Docker controller persists its state under `DOCKER_DATA_ROOT/controller/state/mesh.db`; the native worker currently resolves its relative `MESH_DB_PATH` from its checkout, normally `state/mesh.db`. The worker’s live quota-prewarm loop therefore reads a different runtime-flag registry from the controller/API. Both files can look canonical. This is split authority, not a missing environment variable. Solve the whole boundary in one reviewed change: map every read/write first, establish one owner per datum, migrate every affected configuration/control path together, and remove the unlawful direct access—not merely the flag that exposed it.

## Outcome / authority contract

The intended end state is one canonical controller-owned control-plane and runtime-configuration authority. A worker obtains controller-owned state through authenticated, versioned/bounded control-plane APIs and never opens, reads, or writes the controller’s `mesh.db` by filesystem path or maintains a second canonical copy. A worker may retain only explicitly classified node-private state (for example local process recovery artifacts or a bounded outbox/spool) with a named owner, retention, replay/idempotency, and failure policy. Do **not** create a separate worker configuration database unless the inventory proves a controller API cannot satisfy a concrete requirement; such an exception needs an operator decision with a written authority, synchronization, and recovery contract.

## TASK

1. **Inventory before design or edits.** Trace the deployed controller (gateway + task-server Docker processes), every native/remote worker, bootstrap/PM2 scripts, Compose volumes, `.env`/config loading, `get_db()`, direct `sqlite3` use, `MeshDB`, runtime-flag registry, telemetry sinks, session/task/flow/node registry, artifact and activity paths. Search the entire repository including `scripts/`, `deploy/`, tests, and service definitions. For each access, record: process/node, exact resolved path or endpoint, table/file/key, read/write, data class, current authority, consumers, and behavior when disconnected.
2. **Verify the running topology without assuming.** Collect non-mutating process/env/path evidence for the host worker and controller container(s), including resolved `MESH_DB_PATH`, `MESH_SHADOW_WRITE`, mounts, controller URL, and actual SQLite identities/schema versions. Never print secrets. Establish whether any apparent duplicate DB is a legacy mirror, a legitimate node-private ledger, or a competing state authority.
3. **Write the evidence-backed authority map and migration design** in a focused durable document. It must classify all state as controller-canonical, worker-private, cache/projection, or deprecated; name the sole writer and every reader; identify all dual writes/reads; specify the API contract for worker-needed config/control reads and writes; and include a migration/cutover/rollback sequence. Reject “same schema in two files” as evidence of synchronization.
4. **Choose the smallest complete standardization based on that map.** Reuse authenticated control-plane patterns where possible. Runtime flags/config needed by workers must remain dynamically mutable without deployment/environment changes, have one controller writer path, a bounded worker fetch/cache/refresh policy, an explicit last-known-good/disconnected behavior, and a version/revision or equivalent change detector. Do not add polling that performs an unbounded DB scan or an N+1 request pattern.
5. **Implement the complete affected-path cutover in one branch/PR.** Replace every worker direct `get_db()`/SQLite access to controller-owned state found in the inventory—not only `QUOTA_PREWARM_ENABLED`. Eliminate controller-side writes to a worker-local database except for deliberately classified node-private transport. Use one shared client/seam rather than feature-specific flag plumbing. Preserve local execution only where it truly shares the controller authority; make that mode explicit and covered by tests.
6. **Migrate safely.** Define and implement the one-time reconciliation from existing divergent registries with a deterministic source selection, dry-run/report mode, idempotency, conflict handling, audit trail, and rollback boundary. Never silently overwrite a live flag/config value. If the live files disagree or no trustworthy precedence exists, stop at the recorded conflict and obtain operator direction rather than guessing.
7. **Test the real boundary, not mocks alone.** Add integration coverage with distinct controller and worker filesystem roots/SQLite files proving that a controller/API config mutation is observed by the worker within the documented bound; a worker cannot mutate/read controller-owned DB state directly; worker failure/disconnect uses the documented safe behavior; and restart/retry cannot resurrect a stale duplicate authority. Cover every inventory data class changed by the cutover, plus existing telemetry/task/registration behavior that shares the transport.
8. **Apply the service-boundary checklist to every new or changed API/IPC seam.** Record concurrency at 100 workers, request-size bounds, auth/node authorization, timeout/retry/backoff, malformed response handling, memory/cache bounds, controller outage behavior, and how stale cache/revision is surfaced. Reuse A71’s per-node credential direction; do not broaden that credential implementation unless necessary for this job’s safe API contract—coordinate rather than duplicate it.
9. **Document operations honestly.** Update the configuration/deployment documentation and `.ai/CONTEXT.md` with the final authority map, exact deployment sequence, rollback, what is operator-gated, and a concrete note for every deferred boundary risk. Update `DISPATCH_LOG.md` on closure.

## ACCEPTANCE (proof, not vibes)

1. A committed authority inventory covers all controller/worker DB and state-file accesses, with resolved deployment paths and read/write classification; no unexplained direct database access remains.
2. A single canonical owner is defined and enforced for every controller/config/control datum. The worker has no second canonical configuration registry.
3. Every worker feature that formerly consulted controller-owned `mesh.db` state—including quota prewarming—uses the standardized authenticated transport; repository search and tests prove no flag-specific bypass remains.
4. End-to-end tests use genuinely distinct controller and worker DB paths and prove dynamic controller config propagation, bounded convergence, restart behavior, stale-cache/disconnect policy, authentication/authorization, and no cross-filesystem DB access.
5. Existing control-plane behaviors (registration, claim/result, session/flow state, telemetry/activity forwarding, and runtime flags) retain tested semantics or have an explicit reviewed migration assertion. No new cross-process SQLite writer contention is introduced.
6. One-time migration/reconciliation is dry-runnable, idempotent, conflict-safe, and cannot silently choose between divergent live values.
7. Targeted test suite and relevant full integration tests are green; the PR includes the authority map, operational rollout/rollback, and service-boundary checklist evidence. Production cutover remains operator-gated.

## CURRENT EVIDENCE (orientation only — re-derive)

- `compose.yaml` mounts `${DOCKER_DATA_ROOT}/controller/state` at `/app/state` for controller services.
- `config/settings.py` defaults `MESH_DB_PATH` to `state/mesh.db`; `src/control/db.py:get_db()` resolves a relative path from the checkout containing the imported source, not from the caller’s deployment role.
- `src/worker/agent.py`’s quota-prewarm supervisor re-reads `runtime_flag_enabled("QUOTA_PREWARM_ENABLED")` every cycle. This preserves dynamic behavior but currently selects the worker process’s local resolved DB.
- `src/control/db.py` itself documents that the worker reads the DB it is pointed at. This documentation is a symptom to replace, not the desired long-term boundary.
- Prior #138 work removed one co-located telemetry mirror because cross-process writes to the same SQLite file caused lock contention. Preserve that lesson: HTTP/API transport is the control-plane crossing; a second direct SQLite path is not a harmless fallback.

## SCOPE / NON-GOALS

- This is explicitly **not** a quota-prewarm-only patch, an environment-variable workaround, or a new worker config DB by default.
- Do not alter unrelated session queue semantics (A82), completion delivery (A84), or per-node credential issuance (A71). Coordinate shared seams and make compatible, narrow changes only.
- Do not silently deploy/restart native workers or containers, delete either existing DB, copy production data, or flip runtime flags. Provide exact operator commands and verification instead.
- Do not turn a cache or local spool into a hidden authority. It must be removable/rebuildable from the named canonical source.

## RESERVED DECISIONS

- **R1 — divergent live values:** do not pick a winner. Produce the diff/audit report and ask the operator to select or approve a documented precedence rule.
- **R2 — required worker-local durable state:** only introduce it after the map demonstrates why API retrieval is insufficient. It needs owner, schema/version, retention cap, encryption/permissions if sensitive, idempotent sync/replay, and a test proving it cannot become canonical.
- **R3 — API placement/auth:** prefer an existing authenticated control-plane route/client. If new API capability would conflict with A71’s credential migration, coordinate the contract and sequence the implementation rather than reintroducing shared-token assumptions.
- **R4 — migration affects live controller data:** implementation may prepare and test a dry run, but the actual live migration/cutover is operator-gated.

## TRAIL / EVIDENCE (fill at close)

- Authority inventory + diagram/table; resolved-path evidence (sanitized); API/config propagation contract; migration dry-run/conflict report; tests using separate controller/worker roots; service-boundary checklist; PR and operator rollout/rollback runbook.

---

## Milestone (burndown)

- [x] Full controller/worker state-access inventory and sanitized live topology verification
- [x] Authority map + complete cutover/migration design reviewed against current tree
- [x] Shared authenticated worker state/config seam implemented; all direct controller-owned DB paths removed
- [x] Conflict-safe reconciliation and rollback/dry-run tooling implemented
- [x] Separate-root integration tests and service-boundary checklist pass
- [ ] Documentation, CONTEXT note, DISPATCH_LOG closure, PR, and operator-gated rollout runbook complete

## Execution record — 2026-10-02 (branch `feat/database-authority-unification`)

- **Live evidence (read-only):** worker PID holds `~/dev/AI-team/state/mesh.db` (frozen at the
  2026-09-24 Docker migration) + its own `quota_windows.db`; controller uses
  `~/ai-team-data/controller/state/`. `runtime_flags` identical (14/14, value + `set_at`) ⇒ R1 clean.
  Report: `mesh_health_samples` (old rolling window), `nodes: kanebra` (deliberately deleted
  stale row), `push_subscriptions` (stray host `last_error` stamp) — nothing to migrate.
- **Change:** `src/control/controller_state.py` seam (installed ⇒ `get_db()` None, flag rows
  from the controller); task-server `GET /control/runtime-flags` + `POST
  /control/cases/{id}/boot-reconcile` on existing `WORKER_TOKEN` auth;
  `src/worker/controller_state_client.py` (LKG snapshot, 30 s refresh); worker `main()` installs it;
  `claude_driver` boot reconcile routes through it; `scripts/db_authority_report.py`.
  Authority map + runbook: `docs/backend/DATABASE_AUTHORITY.md`.
- **R2:** worker `quota_windows.db` kept as a classified worker-local observation cache.
- **Verification:** 21 new tests (incl. a real `server_main.py` subprocess on a separate DB root;
  mutation-checked: 4 seam tests RED without the `db.py` wiring); 340 existing tests across touched
  modules green. Merge dry-run: clean vs `main`; vs A82 only the generated `_DISPATCH_STATE.md`.
  A82 adds no worker-side `get_db()` (its worker gates are env-based).
- **Rollout hazard:** controller must be redeployed before the worker restarts on this code
  (old task-server ⇒ 404 ⇒ env/default flags, logged at ERROR). Hence: PR opened, **merge held**
  for the operator-approved sequence in `docs/backend/DATABASE_AUTHORITY.md` §6.

- **Pre-merge sweep (independent adversarial review + own pass), all fixed:** deploy preflight
  recreated the retired `mesh.db` (now installs the client and refuses a worker newer than its
  controller); work claimed before the first flag snapshot (now gated, read-only gate, single
  refresher); 404 retry/log storm (now once per episode, 30 s); tests leaked a developer `.env`
  (now isolated, child env pinned); report could report "clean" for uncompared tables and had
  colliding exit codes (now SKIPPED + exit 0/3/4/5/6); startup block cut to one ≤5 s attempt.
  Documented, not changed: reconcile under the driver lock (~10 s worst case, relay OFF live;
  deferred until A82 rewrites `_get_or_create`). A82 semantic-merge note in the doc §8.
  Follow-up written: **A93** (quota DB retention — worker never prunes).

## Closure (fill on completion)

(Do not mark done until the inventory demonstrates that every discovered duplicate authority was either eliminated or explicitly classified as non-canonical worker-private state, with evidence.)
