# Database authority — controller vs worker (A88)

**Status:** design + implementation on `feat/database-authority-unification` (2026-10-02).
**Packet:** `.ai/dispatch/AGENT_88_DATABASE_AUTHORITY_UNIFICATION.md`.

## 1. The problem, as observed live (2026-10-02, read-only probes)

Topology: controller = Docker `gateway` (:9003) + `task-server` (:9002), both mounting
`~/ai-team-data/controller/state` at `/app/state` (`compose.yaml:22-27`). Native worker =
PM2 `ai-team-worker`, cwd `~/dev/AI-team`, `CONTROLLER_URL=http://<tailnet-ip>:9002`.

| Evidence | Finding |
|---|---|
| `/proc/<worker>/fd` | worker holds 4 handles on `~/dev/AI-team/state/mesh.db` + 1 on `state/quota_windows.db` |
| both `mesh.db` opened `mode=ro` | same schema (migration 33); every control table in the worker file stops at **2026-09-24** (the Docker migration) — it is a frozen pre-Docker copy |
| `runtime_flags` | 14 rows each, identical value **and** `set_at` → no divergence today (R1 clean) |
| `push_subscriptions` | one row updated on the host 2026-09-28 17:40 with a different `last_error` than the controller row — a stray host-side write (no worker code path reaches `PushService`) |
| `quota_windows.db` | two actively-written ledgers: worker 225k `coordinator_events`, controller 36k |

Root cause: `src/control/db.py:get_db()` is role-blind. Any process with `MESH_SHADOW_WRITE`
on (default) opens `<checkout>/state/mesh.db`, creating + migrating it if absent. The worker
therefore reads controller-owned configuration from a file the controller never writes, and
`/api/flags` / `scripts/ops_flag.sh` writes are invisible to it.

## 2. Authority map

Classes: **C** controller-canonical · **W** worker-private · **P** cache/projection · **D** deprecated.

| Data | Class | Sole writer | Readers | Worker access before A88 | After A88 |
|---|---|---|---|---|---|
| `mesh.db` (all tables: sessions, flows, tasks, nodes, telemetry, flags, push, approvals…) | C | controller (gateway + task-server, same volume) | controller | worker opened its own copy at boot (`telemetry_sink.py:434` → `get_db()`) | worker never opens any `mesh.db` |
| `runtime_flags` | C | controller `/api/flags` | controller; worker via HTTP | `runtime_flag_enabled` → local copy: `QUOTA_PREWARM_ENABLED` (`agent.py:1391`), `MANAGER_ROLE_ENABLED` / `MANAGER_TOOLS_ENABLED` (`claude_driver.py:470,482`), `DURABLE_RELAY_ENABLED` (`claude_driver.py:1271`) | `GET /control/runtime-flags` (task-server) → in-memory last-known-good snapshot |
| Case/flow ledger (`flow_runs`, `flow_events`, wait groups) | C | controller | controller | Manager boot reconcile read **and wrote** the local copy (`claude_driver.py:1274-1280`); dormant only because `DURABLE_RELAY_ENABLED=0` | `POST /control/cases/{id}/boot-reconcile` (task-server) |
| LLM telemetry (`llm_*`) | C | controller ingest (`/telemetry/batches`) | controller | HTTP **plus** a local mirror when the controller URL looked remote (`telemetry_sink.py:466-477`); live worker dropped it (tailnet IP == own IP) but still created/migrated the DB | HTTP only; spool (`logs/telemetry_spool`) is the durability layer |
| Telemetry spool `logs/telemetry_spool/*.json` | W | worker | worker (replay) | unchanged | unchanged — bounded by `TELEMETRY_SPOOL_MAX_BYTES`, rebuildable (only unsent batches) |
| `logs/events.ndjson`, job logs `.ai/job_*.log`, `<repo>/uploads/` | W | worker | worker / operator | unchanged | unchanged |
| `$CODEX_HOME/gateway-ownership.sqlite3` | W (host) | backend on that host | same | unchanged | unchanged — process-ownership fencing, host-scoped by design |
| Worker `state/quota_windows.db` | P (worker-local observation cache) | worker prewarmer coordinator | worker prewarmer only | unchanged | unchanged — see §4 R2 |
| Controller `quota_windows.db` | C | controller (`/telemetry/quota-observation` ingest) | controller quota API | — | — |
| Worker checkout `state/mesh.db` | **D** | nobody after A88 | nobody | read/written | retired by operator after cutover (§6) |

Not changed (recorded inconsistency): the worker derives its quota-*observation* default from
`QUOTA_COORDINATOR_ENABLED` in its **env** at startup (`src/worker/config.py:61-63`), with the
per-node `WORKER_QUOTA_OBSERVE` override, while the controller reads the same name from the
registry. It never touched `mesh.db`, so it is out of A88's cutover; moving it to the registry would
change live observation behaviour and is left as a follow-up decision.

## 3. Design — one seam, controller over HTTP

### 3.1 Server (task-server :9002, existing `WORKER_TOKEN` bearer `_require_auth`)
- `GET /control/runtime-flags` → `{revision, flags: [{flag_name, value, set_at}]}`.
  Only registry-writable flags that have a registry row (≤ `len(RUNTIME_FLAG_DEFINITIONS)`).
  `revision` = sha256 over the sorted `(flag_name, value, set_at)` tuples (change detector).
  503 when the controller DB is unavailable.
- `POST /control/cases/{case_id}/boot-reconcile` → the same guards the driver applied
  (unknown/closed Case ⇒ no-op) then `db.boot_reconcile_case(case_id, actor="manager")`,
  which is idempotent and self-gated on `DURABLE_RELAY_ENABLED` (read from the controller DB).
  `case_id` is length/charset validated (422 otherwise).

Placement (R3): the task-server is the worker's existing control plane (`CONTROLLER_URL`), and
A71 adds per-node `_authorize_node` to exactly these routes; the new routes use the same
`_require_auth` dependency so A71 wraps them without a second credential path. The control API
(:9003) is not reachable by workers by contract.

### 3.2 Process seam (`src/control/controller_state.py`)
A process-wide registration: `install(client)` / `active()` / `uninstall()`.
When a client is installed (worker `main()` only):
- `get_db()` returns `None` — the documented "no local DB" contract every caller already guards
  (`if db:`), so the worker can never open, create or migrate a `mesh.db`;
- `_runtime_flag_row` resolves registry rows from the client's snapshot, then the existing
  env → default fallback applies unchanged;
- `claude_driver._boot_reconcile_manager_case` calls `client.boot_reconcile_case(case_id)`.

Gateway, task-server, local-execution gateway and tests never install a client ⇒ byte-identical.

### 3.3 Worker client (`src/worker/controller_state_client.py`)
- Immutable snapshot (`RuntimeFlagSnapshot`, Pydantic) swapped atomically; reads are memory-only
  (no I/O on the event loop or in the driver).
- Refresh: one bounded fetch (≤5 s) at worker start, then the refresh loop — the **only**
  refresher (no concurrent fetches): every 30 s with a snapshot, every 5 s until the first success.
- **Work gate:** until the first snapshot exists the poll loop claims nothing
  (`WorkerAgent._controller_state_ready`, top of `_fetch_pending`), so no session boots on
  env/default flags after a startup outage. Exception: a controller that predates the route (404)
  does not gate — waiting would stall all work; the worker runs on env/default flags instead.
- Malformed/oversized response (Pydantic schema: ≤256 rows, bounded field lengths) ⇒ rejected,
  last-known-good kept, warning logged. A 404 on the route logs
  `event=controller_state_route_missing` at ERROR once per episode and retries every 30 s — the
  worker is newer than the task-server. The deploy preflight (`scripts/safe_worker_deploy.py`)
  refuses to deploy a worker in that state.
- Disconnected ⇒ last-known-good indefinitely; `event=controller_state_stale age_sec=…` logged
  once when the snapshot is older than 5 refresh intervals, `…_recovered` on the next success.
  Never fetched ⇒ registry rows absent ⇒ env/default (identical to today's no-DB behaviour).
  Flags are consumed only while handling controller-delivered work or by the prewarm supervisor
  (which already holds state on read errors), so a controller outage cannot flip live behaviour.
- `revision` change ⇒ `event=controller_flags_changed revision=… changed=[…]`.

## 4. Reserved decisions
- **R1 (divergent values):** none — all 14 flag rows identical (value + `set_at`). The report tool
  (§5) re-checks at cutover and exits non-zero on any conflict; the operator decides then.
- **R2 (worker-local durable state):** only the classified W/P items above. The worker
  `quota_windows.db` stays: the prewarmer must decide from *this host's* Claude status line with
  no controller round-trip in the activation path, and the controller already receives the same
  observations via `/telemetry/quota-observation`. It is never read by the controller and is
  rebuildable (delete ⇒ re-observed). Retention is **not bounded today** — tracked as A93
  (`.ai/dispatch/AGENT_93_QUOTA_DB_AUTO_PRUNING.md`): the worker never runs `_prune_once_daily`
  (prewarmer calls `observe_once()` directly), so snapshots date back to 2026-07-31, and
  `coordinator_events` has no retention on either side (worker 225k rows / 120 MB).
- **R3:** §3.1. **R4:** live cutover is operator-gated (§6).

## 5. Migration / reconciliation
`scripts/db_authority_report.py --controller-db … --worker-db …` (read-only, `mode=ro`, SQL
`ATTACH` + `NOT EXISTS`, no row materialisation; URL-shaped/long keys are hashed in output):
exit 3 = runtime-flag conflict or worker-only flag row (R1, operator decides); 4 = some tables
could not be compared (listed as SKIPPED — never a clean bill); 5 = worker-only / worker-newer rows
by primary key (review; nothing is migrated); 6 = unreadable database; 0 = clean. Idempotent by
construction (never writes either database; verified by checksum — a WAL file may still get its
`-shm` side file created by SQLite on a read-only open).

Live run 2026-10-02 (exit 5, nothing to migrate, nothing skipped):

| Table | Divergence | Classification |
|---|---|---|
| `runtime_flags` | none (14/14 identical) | — |
| `mesh_health_samples` | 5758 worker-only | rolling, pruned health samples from the worker file's pre-2026-09-24 window — projection history, not authority |
| `nodes` | `kanebra` worker-only | stale PM2-era node row deliberately deleted on the controller 2026-09-25 |
| `push_subscriptions` | 1 worker-newer | stray host-side `last_error` stamp 2026-09-28; the controller row is canonical |

## 6. Operator rollout / rollback (gated)
**Order matters: controller first, worker second.** A worker on the new code against an old
task-server gets 404 and runs on env/default flags (logged at ERROR) until the task-server is updated.

1. Merge; rebuild/recreate the controller (`docker compose up -d --build`,
   `docs/RUNBOOKS/OPERATIONS_DOCKER.md`); check `GET /control/runtime-flags` returns 200 with the
   worker token.
2. Run the report against both files; expect exit 5 with exactly the classified rows in §5.
   Exit 3, 4 or 6 — or any new divergence — stops the rollout for an operator decision.
3. Restart the native worker on the merged code (**operator-gated**: interrupts live sessions).
4. Verify: `ls -l /proc/$(pm2 pid ai-team-worker)/fd | grep mesh.db` is empty;
   worker log shows `event=controller_flags_refreshed`; flip a flag via `ops_flag.sh` and see
   `event=controller_flags_changed` within ~30 s.
5. Retire the old file: move `~/dev/AI-team/state/mesh.db{,-wal,-shm}` aside (keep, don't delete).
Rollback: redeploy the previous worker commit (it reopens its local file — still present until
step 5) and the previous task-server image; the new routes are additive.

## 7. Service-boundary checklist (new routes)
- **Concurrency:** 1 flag GET / 30 s / worker — one indexed SELECT of ≤33 rows; 100 workers ≈
  3.3 req/s. Boot-reconcile: one call per Manager session boot; idempotent; serialised by the DB
  write lock.
- **Memory:** response ≤ ~5 KB (≤33 registry rows); the client schema rejects >256 rows.
- **Request size:** GET has no body; POST takes no body and a validated path id.
- **Timeout:** client 5 s per call; refresh loop never blocks the event loop (`to_thread`).
- **Malformed input:** server 422 on a bad case id; client validates with Pydantic and keeps LKG.
- **Backing failure:** controller DB unavailable ⇒ 503; worker keeps LKG (or env/default when never
  fetched) and retries.
- **Known bound:** the worker-hosted Manager boot reconcile is a sync HTTP call inside the
  driver's session-creation lock (in a `to_thread` worker, never on the event loop). urllib's 5 s
  timeout is per socket operation, so a controller that accepts but stalls can hold that lock
  ~10 s, blocking other session creates/cancels on that worker; a timed-out reconcile is logged and
  not retried. Only when `DURABLE_RELAY_ENABLED` is on (OFF live 2026-10-02). Releasing the lock
  before the reconcile is deferred until A82 lands (A82 rewrites `_get_or_create`).
- **Deferred:** shared-token auth until A71 (routes use the same dependency A71 wraps);
  worker/controller `quota_windows.db` retention (A93); the `_http_target_is_colocated`
  heuristic is now inert for workers (no local sink) and is left in place; flags set only in the
  controller's env (no registry row) are invisible to workers — unchanged from before A88.

## 8. Merging with A82 (`feat/session-turn-queue`)
- Textual: only the generated `.ai/dispatch/_DISPATCH_STATE.md` / `_dispatch.parquet` conflict
  (regenerate with `scripts/dispatch/dispatch_state.py`).
- **Semantic:** A82 splits claiming into legacy `_fetch_pending` + `_fetch_pending_managed`. The
  `_controller_state_ready()` gate must cover **both** (managed claims too) after the merge.
- A82's worker-local `ManagedClaimStore` and managed-result spool are worker-private state;
  add them to §2 when A82 merges.
