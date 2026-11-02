# Build-vs-Buy Modernization Plan — storage, queue, config, CI

**As of:** 2026-11-02 (verified against `main` @ `bff21e9`) · **Owner:** operator · **Status:** proposed

The domain code (session semantics, backend adapters, admission *rules*, the dispatch
protocol) is the product and stays. The weak spots are generic plumbing that was
hand-built where mature libraries exist: the SQL data layer, migrations, SQLite as a
multi-process store, queue claim mechanics, polling-based wake-ups, env parsing and CI
enforcement. The biggest win is the storage chain **Alembic → SQLAlchemy Core → Postgres
(`SKIP LOCKED` + `LISTEN/NOTIFY`)**. It removes the class of bugs the recent reliability
PRs (#262, #263, #269, #270) were fighting.

This is a reference plan. Each workstream (W0–W6) becomes its own dispatch packet via the
`managing-dispatch-jobs` skill. Packets must re-verify the evidence below; do not trust it.

---

## 1. Findings — what is hand-built or below standard

| ID | Area | Today (evidence) | Mature replacement | Effort |
|---|---|---|---|---|
| F1 | Data access | `src/control/db.py` = 12,300 lines of raw stdlib `sqlite3` (docstring: "No ORM"). Its "single place where SQL is written" rule is already broken: `sqlite3` is imported directly by 10 other `src/` modules (`task_server.py`, `orchestrator.py`, `agent_inbox.py`, `session_store.py`, `routes/cost.py`, `routes/monitoring.py`, `session_node_resolver.py`, `quota_window_coordinator.py`, `codex_ownership.py`, `opencode.py`). `schema.prisma` is documentation only and can drift. | SQLAlchemy 2.0 **Core** (typed `Table` objects, composable queries, dialect-portable); ORM only where plain CRUD | High, incremental |
| F2 | Migrations | Numbered Python tuples in `_get_migrations()`, 96 `ALTER TABLE`, `_CURRENT_VERSION = max(...)`. No downgrade, no autogenerate, no schema-diff check. | Alembic | Low to start |
| F3 | Session-record authority | `SessionStore.save()` writes `state/sessions/*.json` and then `_shadow_write()` to the DB. PR #270 deliberately preserves a file-only read mode. Already solved elsewhere: conversation turns are DB-canonical (`CONVERSATION_DATA_FLOW.md` §0) and controller vs worker authority is settled (A88, `DATABASE_AUTHORITY.md`). **Session records are the remaining dual store.** | DB as the single writer; JSON as a one-way export or removed | Medium |
| F4 | Multi-process SQLite | The gateway and task-server containers share one `mesh.db` (WAL). Writes go through a module-level `_write_lock` plus 9 `BEGIN IMMEDIATE`. Lock waits are bounded only by the 15 s busy timeout (`READ_PATH_BURNDOWN.md`). | Postgres with row-level locks and MVCC | Medium, after F1/F2 |
| F5 | Queue claim mechanics | Hand-built claim, lock and recovery in `turn_queue.py` / `turn_admission.py` / `turn_scheduler.py` (~1,400 lines) and `task_server.py` (2,274 lines). | `SELECT … FOR UPDATE SKIP LOCKED` claims. Admission rules and queue tables stay. | High, biggest payoff |
| F6 | Cross-process wake | Worker finish → Manager wake is polled (A105: "16.5 s live"). The scheduler keeps a poll loop "as the backstop". | Postgres `LISTEN/NOTIFY` (`pg_notify` in the committing transaction) | Low once on Postgres |
| F7 | Config | `config/settings.py` makes 108 `os.getenv`/`environ` reads with hand parsing (`.lower() == "true"`, manual int casts). | `pydantic-settings` `BaseSettings` (Pydantic is already a dependency) | Low to medium |
| F8 | CI gates | `.github/workflows/ci.yml` runs only `pytest -q -n auto`. `ruff`, `black` and `mypy` are in the dev extras but nothing enforces them. No web typecheck in CI. | `ruff check` + `ruff format --check` + `mypy` (baselined) + `tsc --noEmit` + vitest; `pre-commit` | Low |

**Postgres portability inventory (`db.py` only):** `PRAGMA` ×15, `INSERT OR` ×8,
`json_extract` ×7, `AUTOINCREMENT` ×7, `lastrowid` ×5, `strftime(` ×2, `BEGIN IMMEDIATE` ×9.
Each needs a portable form (`ON CONFLICT`, JSONB operators / SQLAlchemy JSON, `Identity`,
`RETURNING`). Moving queries to SQLAlchemy Core in W2 is what makes this tractable.

### Reviewed and kept (do not change)

- **Dispatch system** (`.ai/dispatch`, yaml state blocks, `dispatch_state.py`). It is an
  agent-native job protocol: machine-readable state, evidence paths, audits. A generic
  issue tracker cannot replace that. It is intentional.
- **Notifications.** Web Push (`push_service.py`, VAPID) is the primary phone channel and
  Telegram is a minor system channel. Apprise has no browser Web Push, so swapping gains
  little.
- **PM2 files** (`ecosystem.config.js`, `pm2-ressurect_example.bat`). Workers run natively
  under PM2 by design. Only the gateway moved to Docker.
- FastAPI, Pydantic models, React 19 + TanStack Query + Zustand, bearer-token auth (single
  operator), the worker-private SQLite stores (`quota_windows.db`, carrier spools). These
  are standard choices or deliberately node-private.
- Telemetry stack (`telemetry_*`, `cost_read_model`): it could map to OpenTelemetry or
  Langfuse, but it has **not been audited**. It is a candidate for later, not a workstream here.

---

## 2. Priority list A — biggest wins

1. **W4 + W5: Postgres with `SKIP LOCKED` claims and `LISTEN/NOTIFY` wake (F4, F5, F6).**
   *Why:* this is where the reliability work keeps landing. Row locks replace the global
   write lock and busy-timeout waits. `SKIP LOCKED` is the standard concurrent-claim
   primitive. `NOTIFY` replaces poll-driven wake (makes A105's custom signal unnecessary).
   The poll loop shrinks to a slow safety net.
2. **W1: Alembic (F2).** *Why:* it stops the hand-rolled migration list growing, gives
   schema-diff checks in CI, and makes Postgres migrations a supported path, not a rewrite.
3. **W2: SQLAlchemy Core data layer (F1).** *Why:* the 12,300-line module and 10 stray
   `sqlite3` users are why every schema or dialect change is expensive. Core keeps
   SQL-shaped code but makes it typed, composable and dialect-portable. **Prerequisite for W4.**
4. **W3: single authority for session records (F3).** *Why:* removes the last dual store
   and finishes what §0 and A88 started. It is also a prerequisite for a clean data
   migration to Postgres (one source to migrate).

## 3. Priority list B — lowest effort, real improvement

| Rank | Workstream | Effort | Why |
|---|---|---|---|
| 1 | **W0: CI gates** | ~2–4 h | The tools are already declared. Enforcing them is the cheapest quality signal and blocks regressions from today. Baseline existing violations instead of fixing them all. |
| 2 | **W1: Alembic baseline** | ~0.5–1 day | Stamp the current schema and route new migrations through Alembic. No rewrite. It is also step one of list A. |
| 3 | **W6: pydantic-settings** | ~1 day | Typed, validated config that fails fast at startup instead of 108 hand-parsed reads. Independent of the storage chain. |

---

## 4. Workstreams (packet seeds)

Order: **W0 → W1 → W2 (queue tables first) → W3 → W4 → W5**. W6 can run in parallel at
any point. All `src/`/config changes go on `feat/<slug>` + PR. **Run targeted pytest only**
(`running-targeted-tests` skill); never the full or e2e suite.

### W0 — CI gates (Level 1)
- Add `ruff check`, `ruff format --check`, `mypy` and web `pnpm tsc --noEmit` + vitest to `ci.yml`. Add `.pre-commit-config.yaml`.
- Existing violations: generate a baseline (per-file ignores / mypy `exclude` allowlist) so CI goes green without a mass reformat. New and touched files must pass.
- **Accept:** CI red on a new lint/type error in a touched file; green on `main` as-is; no mass-formatting diff.

### W1 — Alembic baseline (Level 2)
- Add `alembic` to `pyproject.toml`. Create a baseline revision equal to the current `_CURRENT_VERSION` schema.
- Existing DBs: stamp at baseline without DDL. Fresh DBs: legacy runner then stamp, or Alembic-only (decide in the packet). After this point, no new `_get_migrations()` entries.
- **Accept:** a test proves a fresh Alembic-built schema == a legacy-built schema (table/column/index diff). Stamping a copy of the prod DB changes no data. The test DB template (`tests/conftest.py::_migrated_db_template`) still works.

### W2 — SQLAlchemy Core data layer (Level 3, incremental)
- Add `sqlalchemy>=2`. Define `Table` metadata matching the live schema, then move query families out of `db.py` into a `src/control/repo/` package. **First slice: `mesh_tasks` + turn-queue tables** (W4 depends on them). Then sessions, then the rest.
- Route the 10 direct-`sqlite3` modules through the repo layer as their tables move.
- Keep the public `db.*` function signatures as thin shims while callers migrate. No behavior change.
- **Accept:** per-slice targeted tests green with unchanged semantics. No new raw SQL in `db.py`. Each slice is a separate PR.

### W3 — Session-record single authority (Level 3)
- Make the DB the sole writer for session records. JSON becomes a one-way export (or is dropped).
- Follow A88's method: inventory readers/writers, dry-run parity report (as in the §0 backfill: counts + 0 mismatches), reversible cutover.
- **Operator decision needed:** the fate of the file-only read mode preserved in PR #270.
- **Accept:** no code path reads `state/sessions/*.json` as authority. A parity report is committed. Rollback is documented.

### W4 — Postgres + `SKIP LOCKED` (Level 3, operator-gated cutover)
- Add a `postgres` service to `compose.yaml` (volume under `DOCKER_DATA_ROOT`). Point the SQLAlchemy engine at it via config, with SQLite still selectable.
- Replace the SQLite-specific constructs (inventory in §1). Replace the queue claim/lock path with `FOR UPDATE SKIP LOCKED`. Admission rules stay unchanged.
- Data migration script: dry-run, idempotent, row-count + checksum parity, no silent overwrite.
- **Live cutover, data copy and any worker restart are operator-gated.** Use the `deploying-the-gateway` skill (DB backup + rollback tag). Rollback = point config back at the untouched SQLite file.
- **Accept:** a concurrent-claim test (N claimers, no double claim, no lost row) passes on Postgres. Targeted queue/admission tests are green on both dialects during the transition. A parity report is committed.

### W5 — `LISTEN/NOTIFY` wake (Level 2, after W4)
- Emit `pg_notify` in the committing transaction for turn completion / inbox / effects. The scheduler, inbox and effects consumer `LISTEN` on one channel. Reduce the poll loop to a slow safety net.
- **Re-scope A105** onto this transport instead of building a custom cross-process signal.
- **Accept:** measured finish → wake latency drops from ~16.5 s to sub-second. The safety-net poll still recovers a missed notify (test by dropping one).

### W6 — pydantic-settings (Level 1–2, independent)
- Replace the `os.getenv` reads in `config/settings.py` with `BaseSettings` model(s). Keep the env var names and defaults byte-identical. Keep the `AI_TEAM_ENV_FILE` behavior.
- **Accept:** settings tests cover every previously-read var. Invalid values fail at startup with a clear error. No env renames.

### Later / optional
- **Procrastinate** (Postgres-native task queue on `SKIP LOCKED` + `LISTEN/NOTIFY`) for *non-turn* background jobs: quota pre-warm, digests, cost alerts. Evaluate after W5. Keep the turn queue in-house unless per-key locks provably give per-session FIFO.

---

## 5. Open operator decisions

1. **A105:** pause until W5, or ship only a thin version? Building it on SQLite now means building it twice.
2. **PR #270 file-only session reads:** keep as a supported mode, or retire in W3?
3. **Postgres placement:** a container in the existing Compose stack on this host (recommended), or external.
