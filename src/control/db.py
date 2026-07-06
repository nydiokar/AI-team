"""
Mesh database — SQLite with WAL mode.

This is the canonical database layer for the agent mesh.  It is the single
place where SQL is written; everything else imports from here.

Design principles
-----------------
- stdlib only (sqlite3).  No ORM.  Schema is simple and stable; an ORM adds
  indirection without benefit and complicates the eventual Postgres migration.
  When Postgres is needed, swap the connection factory and the RETURNING clause
  syntax — everything else is standard SQL.
- WAL mode mandatory.  Multiple workers will poll and claim simultaneously.
- Thread-safe.  `check_same_thread=False` + a module-level threading.Lock for
  writes.  Reads are concurrent; writes are serialised.
- Dual-write safe.  JSON files remain authoritative.  The DB is a mirror that
  shadows every SessionStore.save() and every task dispatch/completion.  The
  `shadow_write` flag in MeshConfig controls whether writes happen at all —
  default True so the DB is always warm when we flip the read source.
- Schema versioned.  A `schema_version` table tracks applied migrations.  New
  columns are added via ALTER TABLE in numbered migration functions so the DB
  upgrades in place without a full rebuild.

Tables
------
sessions        — mirrors state/sessions/*.json exactly; one row per gateway session
mesh_tasks      — dispatch queue; one row per task turn dispatched to a worker
task_events     — append-only event log per session (mirrors logs/session_events/)
nodes           — registered worker nodes (ephemeral; rebuilt from heartbeats)

Future tables (noted, not built yet)
--------------------------------------
task_dependencies  — DAG edges for agent-to-agent autonomous flows
agent_runs         — fine-grained per-tool-call log (dashboard/audit)
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

_mesh_health_sample_lock = threading.Lock()
_mesh_health_last_sample: Dict[str, float] = {}

# ---------------------------------------------------------------------------
# Schema version — bump when adding migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION = 21


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Gateway sessions — mirrors state/sessions/*.json
-- Kept in sync by SessionStore via shadow-write.
-- DO NOT add columns that are not also in the Session dataclass unless they
-- are mesh-layer concerns (e.g. node routing).
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    backend             TEXT NOT NULL,
    repo_path           TEXT NOT NULL,
    status              TEXT NOT NULL,       -- idle|busy|awaiting_input|error|cancelled|closed
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    machine_id          TEXT NOT NULL DEFAULT '',
    backend_session_id  TEXT NOT NULL DEFAULT '',
    -- NOTE: `model` column is added by migration 11 (not here) so fresh and
    -- existing DBs converge through the same ALTER. See _get_migrations().
    last_task_id        TEXT NOT NULL DEFAULT '',
    last_artifact_path  TEXT NOT NULL DEFAULT '',
    last_summary        TEXT NOT NULL DEFAULT '',
    last_user_message   TEXT NOT NULL DEFAULT '',
    last_result_summary TEXT NOT NULL DEFAULT '',
    last_files_modified TEXT NOT NULL DEFAULT '[]',  -- JSON array
    telegram_chat_id    INTEGER,
    telegram_thread_id  INTEGER,
    owner_user_id       INTEGER,
    task_history        TEXT NOT NULL DEFAULT '[]'   -- JSON array of task history dicts
    -- NOTE: `model` (migration 11) and `origin` (migration 12, {"channel","kind"}
    -- JSON) are added by ALTER, not here, so fresh and existing DBs converge
    -- through the same migration path. See _get_migrations().
);

-- Mesh task queue — one row per task dispatch turn.
-- status lifecycle: pending → claimed → completed | failed | failed_node_offline
-- The `payload` column holds the full dispatch context as JSON:
--   {session: <Session dict>, prompt: str, task_id: str, action: str}
-- The `result` column holds ExecutionResult as JSON on completion.
-- `parent_task_id` enables dependency chains (agent-to-agent flows).
CREATE TABLE IF NOT EXISTS mesh_tasks (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT,               -- NULL for run_oneoff tasks
    machine_id          TEXT,               -- NULL = any capable node; set on dispatch
    backend             TEXT NOT NULL,
    action              TEXT NOT NULL,      -- create_session|resume_session|run_oneoff|cancel|compact_session
    payload             TEXT NOT NULL,      -- JSON: {session?, prompt, task_id, action, metadata?}
    status              TEXT NOT NULL DEFAULT 'pending',
    claimed_by          TEXT,               -- node_id
    claimed_at          TEXT,
    completed_at        TEXT,
    result              TEXT,               -- JSON: ExecutionResult on completion
    error               TEXT,               -- error message on failure (non-JSON for readability)
    artifact_path       TEXT,               -- pointer to results/{task_id}.json
    parent_task_id      TEXT,               -- for dependency chains (future: task_dependencies table)
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_mesh_tasks_status_machine
    ON mesh_tasks(status, machine_id);

CREATE INDEX IF NOT EXISTS idx_mesh_tasks_session
    ON mesh_tasks(session_id);

CREATE INDEX IF NOT EXISTS idx_mesh_tasks_created
    ON mesh_tasks(created_at);

-- Append-only event log per session — mirrors logs/session_events/{session_id}.log
-- Kept as a table so the dashboard can query "all events for session X" without
-- parsing NDJSON files.
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    success     INTEGER NOT NULL,           -- 0 | 1
    execution_time REAL,
    error       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_task_events_session
    ON task_events(session_id);

-- Registered worker nodes — ephemeral; rebuilt from heartbeats on restart.
-- The VPS node_registry keeps an in-memory copy; this table is the persistent
-- backing store so /nodes Telegram command works after a VPS restart.
CREATE TABLE IF NOT EXISTS nodes (
    node_id             TEXT PRIMARY KEY,
    tailscale_ip        TEXT NOT NULL DEFAULT '',
    api_port            INTEGER NOT NULL DEFAULT 9001,
    backends            TEXT NOT NULL DEFAULT '[]',  -- JSON array of backend names
    max_concurrent      INTEGER NOT NULL DEFAULT 2,
    status              TEXT NOT NULL DEFAULT 'online',  -- online|offline
    last_heartbeat      TEXT NOT NULL,
    registered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Watched jobs — orthogonal to mesh_tasks/session lifecycle.
-- A job is a long-lived external process monitored by the worker's
-- _job_watcher_loop.  It does NOT hold a task semaphore slot, does NOT
-- keep a session BUSY, and does NOT enter the stale-busy reattach loop.
-- Operational mesh health history. Aggregate telemetry, not task lifecycle.
CREATE TABLE IF NOT EXISTS mesh_health_samples (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at                  TEXT NOT NULL,
    source                      TEXT NOT NULL,
    sessions_busy               INTEGER NOT NULL DEFAULT 0,
    tasks_pending               INTEGER NOT NULL DEFAULT 0,
    tasks_claimed               INTEGER NOT NULL DEFAULT 0,
    nodes_online                INTEGER NOT NULL DEFAULT 0,
    nodes_total                 INTEGER NOT NULL DEFAULT 0,
    slots_used                  INTEGER NOT NULL DEFAULT 0,
    slots_total                 INTEGER NOT NULL DEFAULT 0,
    slots_available             INTEGER NOT NULL DEFAULT 0,
    active_tasks                INTEGER NOT NULL DEFAULT 0,
    stale_busy_sessions         INTEGER NOT NULL DEFAULT 0,
    nodes_with_live_state       INTEGER NOT NULL DEFAULT 0,
    nodes_without_live_state    INTEGER NOT NULL DEFAULT 0,
    stale_live_state_nodes_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_mesh_health_samples_sampled_at
    ON mesh_health_samples(sampled_at);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    node_id         TEXT NOT NULL,
    label           TEXT NOT NULL,
    command         TEXT,
    pid             INTEGER,
    pgid            INTEGER,
    started_at      TEXT NOT NULL,
    started_epoch   REAL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | lost
    exit_code       INTEGER,
    log_path        TEXT,
    tail            TEXT,
    notify          INTEGER NOT NULL DEFAULT 1,
    notify_agent    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_node_status
    ON jobs(node_id, status);

CREATE INDEX IF NOT EXISTS idx_jobs_session
    ON jobs(session_id);

-- FlowRun record (v0.4 §13 item 1) — one row per dispatch flow.
-- This is a RECORD, not a stage machine: nothing reads current_stage to decide
-- what runs next. Written best-effort by the orchestrator at dispatch-start and
-- updated at a stage transition; a write failure never affects task execution.
CREATE TABLE IF NOT EXISTS flow_runs (
    flow_run_id     TEXT PRIMARY KEY,
    task_id         TEXT,
    current_stage   TEXT,
    objective_lock  TEXT,
    created_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_flow_runs_task
    ON flow_runs(task_id);
"""

_APPROVALS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    session_id   TEXT,
    task_id      TEXT,
    action       TEXT NOT NULL,
    risk         TEXT NOT NULL DEFAULT 'medium',
    reversible   INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL DEFAULT '',
    resolved_by  TEXT,
    payload      TEXT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id)
"""

_LLM_TELEMETRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT,
    task_id TEXT NOT NULL,
    gateway_node_id TEXT,
    execution_node_id TEXT,
    backend TEXT,
    backend_session_id_start TEXT,
    backend_session_id_end TEXT,
    requested_model TEXT,
    observed_models TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    ended_at TEXT,
    final_status TEXT NOT NULL DEFAULT 'running',
    timeout_status TEXT NOT NULL DEFAULT 'none',
    final_exit_code INTEGER,
    final_invocation_id TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    projection_version INTEGER NOT NULL DEFAULT 1,
    events_pruned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_invocations (
    invocation_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    parent_invocation_id TEXT,
    retry_of_invocation_id TEXT,
    duplicate_of_invocation_id TEXT,
    attempt INTEGER NOT NULL,
    spawn_reason TEXT NOT NULL,
    action TEXT NOT NULL,
    node_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    requested_model TEXT,
    observed_model TEXT,
    process_instance_id TEXT,
    pid INTEGER,
    process_started_at TEXT,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL,
    timeout_kind TEXT,
    exit_code INTEGER,
    signal INTEGER,
    retry_reason TEXT,
    model_request_count INTEGER,
    tool_call_count INTEGER,
    subagent_count INTEGER,
    usage_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(turn_id) REFERENCES llm_turns(turn_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS llm_processes (
    process_instance_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    pid INTEGER,
    parent_process_instance_id TEXT,
    process_role TEXT NOT NULL,
    backend TEXT,
    executable_name TEXT,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    signal INTEGER,
    status TEXT NOT NULL,
    data_quality_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS llm_invocation_processes (
    invocation_id TEXT NOT NULL,
    process_instance_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    PRIMARY KEY(invocation_id, process_instance_id),
    FOREIGN KEY(invocation_id) REFERENCES llm_invocations(invocation_id) ON DELETE CASCADE,
    FOREIGN KEY(process_instance_id) REFERENCES llm_processes(process_instance_id)
);
CREATE TABLE IF NOT EXISTS llm_model_requests (
    model_request_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    provider_request_id TEXT,
    model TEXT,
    work_category TEXT NOT NULL DEFAULT 'unknown',
    started_at TEXT,
    ended_at TEXT,
    status TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    reasoning_tokens INTEGER,
    context_tokens INTEGER,
    input_token_semantics TEXT NOT NULL DEFAULT 'unknown',
    usage_granularity TEXT NOT NULL,
    usage_source TEXT,
    usage_coverage TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(invocation_id) REFERENCES llm_invocations(invocation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS llm_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    event_time TEXT NOT NULL,
    observed_time TEXT NOT NULL,
    node_id TEXT NOT NULL,
    emitter_process_instance_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_sequence INTEGER,
    clock_quality TEXT NOT NULL DEFAULT 'unknown',
    session_id TEXT,
    turn_id TEXT NOT NULL,
    invocation_id TEXT,
    model_request_id TEXT,
    tool_call_id TEXT,
    subagent_id TEXT,
    backend TEXT,
    model TEXT,
    pid INTEGER,
    attributes TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_events_turn
    ON llm_events(turn_id, event_time, source_sequence);
CREATE INDEX IF NOT EXISTS idx_llm_events_invocation
    ON llm_events(invocation_id, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_events_session
    ON llm_events(session_id, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_events_name
    ON llm_events(event_name, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_invocations_turn
    ON llm_invocations(turn_id, attempt);
CREATE INDEX IF NOT EXISTS idx_llm_model_requests_turn
    ON llm_model_requests(turn_id, invocation_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_model_provider_request
    ON llm_model_requests(invocation_id, provider_request_id)
    WHERE provider_request_id IS NOT NULL
"""


# ---------------------------------------------------------------------------
# MeshDB — the public interface
# ---------------------------------------------------------------------------

class MeshDB:
    """SQLite-backed mesh database.

    Thread safety: reads are fully concurrent; writes acquire `_write_lock`.
    All public methods are synchronous and safe to call from any thread
    (including asyncio.to_thread wrappers).

    Usage::

        db = MeshDB("state/mesh.db")
        db.upsert_session(session)
        db.enqueue_task(task_id, session_id, backend, action, payload_dict)
        rows = db.get_pending_tasks(node_id="main-pc", backends=["claude"])
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()   # per-thread connection cache
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a per-thread cached connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                isolation_level=None,   # autocommit; we manage transactions explicitly
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection, None, None]:
        """Serialised write context. Yields a connection inside a transaction."""
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    # ------------------------------------------------------------------
    # Schema init + migrations
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        # executescript() issues a COMMIT before running, so we cannot wrap
        # it in our BEGIN IMMEDIATE context manager.  Run DDL directly then
        # handle migrations (which use plain execute) under the write lock.
        conn = self._conn()
        conn.executescript(_DDL)
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._run_migrations(conn)
                self._ensure_merged_schema(conn)
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply any pending numbered migrations in order."""
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0

        migrations = _get_migrations()
        for version, sql in migrations:
            if version > current:
                # executescript() issues an implicit COMMIT before running —
                # even for an empty/no-op script — which would terminate the
                # BEGIN IMMEDIATE transaction this method runs inside. Skip it
                # for baseline markers (empty SQL) and use plain execute()
                # for real migrations instead so the transaction stays intact.
                if sql.strip():
                    for statement in filter(None, (s.strip() for s in sql.split(";"))):
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
                logger.info("event=db_migration_applied version=%d", version)

    def _ensure_merged_schema(self, conn: sqlite3.Connection) -> None:
        """Repair the merged Web UI/main schema across divergent migration 13s.

        Web UI used migration 13 for approvals; main used migration 13 for LLM
        telemetry and then 14/15 for telemetry columns. This idempotent pass
        preserves both lineages without relying on a DB having taken only one
        exact branch path.
        """
        for sql in (_APPROVALS_SCHEMA_SQL, _LLM_TELEMETRY_SCHEMA_SQL):
            for statement in filter(None, (s.strip() for s in sql.split(";"))):
                conn.execute(statement)
        self._add_column_if_missing(
            conn,
            "llm_events",
            "clock_quality",
            "TEXT NOT NULL DEFAULT 'unknown'",
        )
        self._add_column_if_missing(conn, "llm_turns", "events_pruned_at", "TEXT")

    def _add_column_if_missing(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def upsert_session(self, session: Any) -> None:
        """Mirror a Session dataclass into the sessions table."""
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, backend, repo_path, status,
                        created_at, updated_at, machine_id, backend_session_id, model,
                        last_task_id, last_artifact_path, last_summary,
                        last_user_message, last_result_summary, last_files_modified,
                        telegram_chat_id, telegram_thread_id, owner_user_id, task_history,
                        origin,
                        driver_type, driver_status, cache_health, cache_unhealthy_count,
                        previous_backend_session_ids
                    ) VALUES (
                        :session_id, :backend, :repo_path, :status,
                        :created_at, :updated_at, :machine_id, :backend_session_id, :model,
                        :last_task_id, :last_artifact_path, :last_summary,
                        :last_user_message, :last_result_summary, :last_files_modified,
                        :telegram_chat_id, :telegram_thread_id, :owner_user_id, :task_history,
                        :origin,
                        :driver_type, :driver_status, :cache_health, :cache_unhealthy_count,
                        :previous_backend_session_ids
                    )
                    ON CONFLICT(session_id) DO UPDATE SET
                        backend             = excluded.backend,
                        repo_path           = excluded.repo_path,
                        status              = excluded.status,
                        updated_at          = excluded.updated_at,
                        machine_id          = excluded.machine_id,
                        backend_session_id  = excluded.backend_session_id,
                        model               = excluded.model,
                        last_task_id        = excluded.last_task_id,
                        last_artifact_path  = excluded.last_artifact_path,
                        last_summary        = excluded.last_summary,
                        last_user_message   = excluded.last_user_message,
                        last_result_summary = excluded.last_result_summary,
                        last_files_modified = excluded.last_files_modified,
                        telegram_chat_id    = excluded.telegram_chat_id,
                        telegram_thread_id  = excluded.telegram_thread_id,
                        owner_user_id       = excluded.owner_user_id,
                        task_history        = excluded.task_history,
                        origin              = excluded.origin,
                        driver_type                  = excluded.driver_type,
                        driver_status                = excluded.driver_status,
                        cache_health                 = excluded.cache_health,
                        cache_unhealthy_count        = excluded.cache_unhealthy_count,
                        previous_backend_session_ids = excluded.previous_backend_session_ids
                    """,
                    {
                        "session_id":          session.session_id,
                        "backend":             session.backend,
                        "repo_path":           session.repo_path,
                        "status":              session.status.value if hasattr(session.status, "value") else session.status,
                        "created_at":          session.created_at,
                        "updated_at":          session.updated_at,
                        "machine_id":          session.machine_id or "",
                        "backend_session_id":  session.backend_session_id or "",
                        "model":               getattr(session, "model", None),
                        "last_task_id":        session.last_task_id or "",
                        "last_artifact_path":  session.last_artifact_path or "",
                        "last_summary":        session.last_summary or "",
                        "last_user_message":   session.last_user_message or "",
                        "last_result_summary": session.last_result_summary or "",
                        "last_files_modified": json.dumps(session.last_files_modified or []),
                        "telegram_chat_id":    session.telegram_chat_id,
                        "telegram_thread_id":  session.telegram_thread_id,
                        "owner_user_id":       session.owner_user_id,
                        "task_history":        json.dumps(session.task_history or []),
                        "origin":              _origin_json(getattr(session, "origin", None)),
                        "driver_type":           getattr(session, "driver_type", "") or "",
                        "driver_status":         getattr(session, "driver_status", "") or "",
                        "cache_health":          getattr(session, "cache_health", "unknown") or "unknown",
                        "cache_unhealthy_count": int(getattr(session, "cache_unhealthy_count", 0) or 0),
                        "previous_backend_session_ids": json.dumps(getattr(session, "previous_backend_session_ids", None) or []),
                    },
                )
        except Exception as e:
            logger.warning("event=db_upsert_session_failed session_id=%s err=%s", session.session_id, e)

    def mark_driver_sessions_lost_for_node(self, node_id: str, *, backend: str = "claude") -> int:
        """Mark idle live SDK-backed sessions on a restarted worker as lost."""
        try:
            with self._write() as conn:
                cur = conn.execute(
                    """
                    UPDATE sessions
                    SET driver_status = 'lost', updated_at = ?
                    WHERE machine_id = ?
                      AND backend = ?
                      AND driver_type = 'sdk'
                      AND driver_status = 'live'
                      AND status IN ('idle', 'awaiting_input')
                    """,
                    (_now(), node_id, backend),
                )
                return int(cur.rowcount or 0)
        except Exception as e:
            logger.warning("event=db_mark_driver_sessions_lost_failed node_id=%s err=%s", node_id, e)
            return 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self,
        status: Optional[str] = None,
        backend: Optional[str] = None,
        machine_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if backend:
            clauses.append("backend = ?")
            params.append(backend)
        if machine_id:
            clauses.append("machine_id = ?")
            params.append(machine_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM sessions {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_stale_busy_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return BUSY sessions with no pending or claimed mesh task.

        This is the gateway-side M3 reconciliation query. A session is considered
        stale-busy when the gateway still marks it busy but the dispatch ledger has
        no active task for that session. Completed/failed historical rows do not
        count as active work.
        """
        rows = self._conn().execute(
            """
            SELECT s.*
            FROM sessions s
            WHERE s.status = 'busy'
              AND NOT EXISTS (
                SELECT 1
                FROM mesh_tasks t
                WHERE t.session_id = s.session_id
                  AND t.status IN ('pending', 'claimed')
              )
            ORDER BY s.updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Mesh tasks
    # ------------------------------------------------------------------

    def enqueue_task(
        self,
        task_id: str,
        session_id: Optional[str],
        machine_id: Optional[str],
        backend: str,
        action: str,
        payload: Dict[str, Any],
        artifact_path: Optional[str] = None,
        parent_task_id: Optional[str] = None,
    ) -> None:
        """Insert a new pending task into the dispatch queue."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO mesh_tasks (
                        id, session_id, machine_id, backend, action,
                        payload, status, artifact_path, parent_task_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        session_id,
                        machine_id,
                        backend,
                        action,
                        json.dumps(payload),
                        artifact_path,
                        parent_task_id,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: mesh_tasks.id" in str(e):
                # Idempotent — task already exists (e.g. duplicate dispatch on retry)
                logger.debug("event=db_enqueue_task_duplicate task_id=%s", task_id)
            else:
                logger.warning("event=db_enqueue_task_integrity_failed task_id=%s err=%s", task_id, e)
        except Exception as e:
            logger.warning("event=db_enqueue_task_failed task_id=%s err=%s", task_id, e)

    def claim_task(self, task_id: str, node_id: str) -> bool:
        """Atomically claim a pending task. Returns True if claim succeeded."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'claimed', claimed_by = ?, claimed_at = ?, updated_at = ?,
                        claimer_incarnation = (SELECT incarnation_id FROM nodes WHERE node_id = ?)
                    WHERE id = ? AND status = 'pending'
                    """,
                    (node_id, now, now, node_id, task_id),
                )
                return conn.execute(
                    "SELECT changes()"
                ).fetchone()[0] > 0
        except Exception as e:
            logger.warning("event=db_claim_task_failed task_id=%s err=%s", task_id, e)
            return False

    def release_task(self, task_id: str, node_id: str) -> bool:
        """Release a claimed task back to pending. Only succeeds if claimed_by matches.

        Returns True if the task was released. Used by workers on graceful shutdown
        and by the stale-claim reaper to reclaim orphaned tasks.
        """
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                        claimer_incarnation = NULL, updated_at = ?
                    WHERE id = ? AND claimed_by = ? AND status = 'claimed'
                    """,
                    (now, task_id, node_id),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception as e:
            logger.warning("event=db_release_task_failed task_id=%s err=%s", task_id, e)
            return False

    def release_node_claims(self, node_id: str) -> List[str]:
        """Release all claimed tasks for node_id back to pending. Returns released task ids.

        Called when a node re-registers (startup sweep). A re-registering node
        means a new process started — any claims from the previous process are
        orphaned and must be returned to the queue so another worker can pick
        them up. This is the fast-path complement to list_stale_claims: it fires
        immediately on restart rather than waiting for the reaper lease to expire.
        """
        now = _now()
        try:
            with self._write() as conn:
                rows = conn.execute(
                    "SELECT id FROM mesh_tasks WHERE claimed_by = ? AND status = 'claimed'",
                    (node_id,),
                ).fetchall()
                task_ids = [r[0] for r in rows]
                if task_ids:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                            claimer_incarnation = NULL, updated_at = ?
                        WHERE claimed_by = ? AND status = 'claimed'
                        """,
                        (now, node_id),
                    )
                return task_ids
        except Exception as e:
            logger.warning("event=db_release_node_claims_failed node_id=%s err=%s", node_id, e)
            return []

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            try:
                dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S.%f")
            except Exception:
                return None
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    @classmethod
    def _stale_online_claim_reason(
        cls,
        row: sqlite3.Row,
        now: datetime,
        live_state_max_age_sec: int,
        active_task_max_runtime_sec: int,
    ) -> Optional[str]:
        live_raw = row["live_state"] if "live_state" in row.keys() else None
        updated_raw = row["live_state_updated_at"] if "live_state_updated_at" in row.keys() else None
        if not live_raw or not updated_raw:
            # Compatibility for old workers: without live_state, online still
            # means unknown. Offline/incarnation checks remain active.
            return None

        updated = cls._parse_dt(updated_raw)
        if updated is None or (now - updated).total_seconds() > live_state_max_age_sec:
            return None

        try:
            live = json.loads(live_raw) if isinstance(live_raw, str) else (live_raw or {})
        except Exception:
            return None
        if not isinstance(live, dict):
            return None

        task_id = row["id"]
        active_ids = set(str(t) for t in (live.get("active_tasks") or []))
        details = live.get("active_task_details") or {}
        if isinstance(details, list):
            details = {
                str(item.get("task_id")): item
                for item in details
                if isinstance(item, dict) and item.get("task_id")
            }
        elif not isinstance(details, dict):
            details = {}

        if str(task_id) not in active_ids and str(task_id) not in details:
            return "missing_from_live_state"

        max_runtime = max(0, int(active_task_max_runtime_sec or 0))
        if max_runtime <= 0:
            return None

        detail = details.get(str(task_id)) if isinstance(details, dict) else None
        started_raw = detail.get("started_at") if isinstance(detail, dict) else None
        started = cls._parse_dt(started_raw) or cls._parse_dt(row["claimed_at"])
        if started is not None and (now - started).total_seconds() > max_runtime:
            return "active_task_over_max_runtime"
        return None

    def list_stale_claims(
        self,
        lease_sec: int = 300,
        *,
        live_state_max_age_sec: int = 90,
        active_task_max_runtime_sec: int = 1800,
    ) -> List[Dict[str, Any]]:
        """Return claimed tasks whose claim has expired.

        A claim is stale when:
        - `claimed_at` is older than `lease_sec` seconds ago, AND one of:
          - the claiming node no longer exists in the nodes table, OR
          - the claiming node is offline (missed heartbeats), OR
          - the claiming node is online but its current incarnation_id differs
            from claimer_incarnation (node restarted in-place — new process,
            same node_id, same online status — the dead process's claim is
            orphaned and will never complete).
          - the claiming node has fresh live_state and the task is not in that
            live_state's active task set.
          - the task is still active in fresh live_state but has exceeded the
            active-task hard runtime cap.

        The incarnation mismatch condition catches the PM2-restart gap that the
        offline-only check misses: the old process is SIGKILLed, the new process
        re-registers within seconds (online again), so the orphaned claim never
        becomes offline → was stuck forever before this fix.
        """
        try:
            # Open a fresh connection for stale-claim queries to avoid potential
            # stale cache issues when SQLite connections are reused across tests.
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(self._path))
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """
                SELECT t.*, n.status AS node_status, n.incarnation_id AS node_incarnation_id,
                       n.live_state, n.live_state_updated_at
                FROM mesh_tasks t
                LEFT JOIN nodes n ON t.claimed_by = n.node_id
                WHERE t.status = 'claimed'
                  AND t.claimed_at IS NOT NULL
                """,
            ).fetchall()
            conn.close()
            now = datetime.utcnow()
            cutoff = lease_sec
            result = []
            for r in rows:
                claimed_dt = self._parse_dt(r["claimed_at"])
                if claimed_dt is None:
                    continue
                age = (now - claimed_dt).total_seconds()
                if age <= cutoff:
                    continue

                node_status = r["node_status"]
                reason = None
                if node_status is None:
                    reason = "node_missing"
                elif node_status == "offline":
                    reason = "node_offline"
                elif (
                    node_status == "online"
                    and r["claimer_incarnation"] is not None
                    and r["node_incarnation_id"] is not None
                    and r["node_incarnation_id"] != r["claimer_incarnation"]
                ):
                    reason = "incarnation_mismatch"
                elif node_status == "online":
                    reason = self._stale_online_claim_reason(
                        r,
                        now,
                        live_state_max_age_sec,
                        active_task_max_runtime_sec,
                    )

                if reason:
                    d = dict(r)
                    d.pop("node_status", None)
                    d["_stale_reason"] = reason
                    result.append(d)
            return result
        except Exception as e:
            logger.warning("event=db_list_stale_claims_failed err=%s", e)
            return []

    def complete_task(
        self,
        task_id: str,
        result: Dict[str, Any],
        artifact_path: Optional[str] = None,
    ) -> None:
        """Mark a claimed task as completed and store the ExecutionResult."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'completed', result = ?, artifact_path = COALESCE(?, artifact_path),
                        completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(result), artifact_path, now, now, task_id),
                )
        except Exception as e:
            logger.warning("event=db_complete_task_failed task_id=%s err=%s", task_id, e)

    def fail_task(
        self,
        task_id: str,
        error: str,
        status: str = "failed",
        result: Optional[Dict[str, Any]] = None,
        artifact_path: Optional[str] = None,
    ) -> None:
        """Mark a task as failed. status can be 'failed' or 'failed_node_offline'."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = ?, error = ?, result = COALESCE(?, result),
                        artifact_path = COALESCE(?, artifact_path),
                        completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        error,
                        json.dumps(result) if result is not None else None,
                        artifact_path,
                        now,
                        now,
                        task_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_fail_task_failed task_id=%s err=%s", task_id, e)

    def get_pending_tasks(
        self,
        node_id: Optional[str] = None,
        backends: Optional[List[str]] = None,
        accept_unpinned: bool = True,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return pending tasks routable to this node.

        machine_id=NULL means any node can claim it.
        machine_id=<node_id> means only that node can claim it (session affinity).
        """
        params: List[Any] = []
        machine_clause = ""
        if node_id:
            if accept_unpinned:
                machine_clause = "AND (machine_id IS NULL OR machine_id = ?)"
            else:
                machine_clause = "AND machine_id = ?"
            params.append(node_id)

        backend_clause = ""
        if backends:
            placeholders = ",".join("?" * len(backends))
            backend_clause = f"AND backend IN ({placeholders})"
            params.extend(backends)

        params.append(limit)
        rows = self._conn().execute(
            f"""
            SELECT * FROM mesh_tasks
            WHERE status = 'pending'
            {machine_clause}
            {backend_clause}
            ORDER BY created_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM mesh_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_task_by_session(self, session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Return a task row matching both session_id and task_id."""
        row = self._conn().execute(
            "SELECT * FROM mesh_tasks WHERE session_id = ? AND id = ?",
            (session_id, task_id),
        ).fetchone()
        return dict(row) if row else None

    def enrich_task(
        self,
        task_id: str,
        *,
        prompt: Optional[str] = None,
        reply_text: Optional[str] = None,
        parsed_output: Any = None,
        file_changes: Any = None,
        files_modified: Any = None,
        usage: Any = None,
        error_class: Optional[str] = None,
        return_code: Optional[int] = None,
    ) -> None:
        """Populate the artifact-complete columns on a task row.

        This is the DB side of the file-free conversation/artifact store: the
        orchestrator calls this at turn completion (and the backfill calls it for
        historical tasks) so ``mesh_tasks`` holds everything ``results/task_*.json``
        used to hold — full untruncated ``reply_text``, ``parsed_output``,
        per-file ``file_changes``, token ``usage`` — without the 264 MB of raw
        NDJSON. COALESCE keeps existing values when a field isn't supplied so this
        is safe to call repeatedly / partially (idempotent backfill).
        """
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks SET
                        prompt              = COALESCE(?, prompt),
                        reply_text          = COALESCE(?, reply_text),
                        parsed_output_json  = COALESCE(?, parsed_output_json),
                        file_changes_json   = COALESCE(?, file_changes_json),
                        files_modified_json = COALESCE(?, files_modified_json),
                        usage_json          = COALESCE(?, usage_json),
                        error_class         = COALESCE(?, error_class),
                        return_code         = COALESCE(?, return_code),
                        updated_at          = ?
                    WHERE id = ?
                    """,
                    (
                        prompt,
                        reply_text,
                        json.dumps(parsed_output) if parsed_output is not None else None,
                        json.dumps(file_changes) if file_changes is not None else None,
                        json.dumps(files_modified) if files_modified is not None else None,
                        json.dumps(usage) if usage is not None else None,
                        error_class,
                        return_code,
                        _now(),
                        task_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_enrich_task_failed task_id=%s err=%s", task_id, e)

    def enrich_tasks_batch(self, rows: List[Dict[str, Any]]) -> int:
        """Enrich many task rows in ONE transaction (fast backfill path).

        Each dict: {task_id, prompt?, reply_text?, parsed_output?, file_changes?,
        files_modified?, usage?, error_class?, return_code?}. Same COALESCE
        semantics as enrich_task. Returns the count attempted. Wrapping all
        UPDATEs in a single BEGIN IMMEDIATE avoids 900+ fsync/commit cycles —
        the difference between seconds and minutes on a server with WAL.
        """
        if not rows:
            return 0
        now = _now()
        try:
            with self._write() as conn:
                for r in rows:
                    conn.execute(
                        """
                        UPDATE mesh_tasks SET
                            prompt              = COALESCE(?, prompt),
                            reply_text          = COALESCE(?, reply_text),
                            parsed_output_json  = COALESCE(?, parsed_output_json),
                            file_changes_json   = COALESCE(?, file_changes_json),
                            files_modified_json = COALESCE(?, files_modified_json),
                            usage_json          = COALESCE(?, usage_json),
                            error_class         = COALESCE(?, error_class),
                            return_code         = COALESCE(?, return_code),
                            updated_at          = ?
                        WHERE id = ?
                        """,
                        (
                            r.get("prompt"),
                            r.get("reply_text"),
                            json.dumps(r["parsed_output"]) if r.get("parsed_output") is not None else None,
                            json.dumps(r["file_changes"]) if r.get("file_changes") is not None else None,
                            json.dumps(r["files_modified"]) if r.get("files_modified") is not None else None,
                            json.dumps(r["usage"]) if r.get("usage") is not None else None,
                            r.get("error_class"),
                            r.get("return_code"),
                            now,
                            r["task_id"],
                        ),
                    )
        except Exception as e:
            logger.warning("event=db_enrich_tasks_batch_failed err=%s", e)
        return len(rows)

    def existing_task_ids(self) -> set:
        """All task ids present in mesh_tasks (one query — backfill membership test)."""
        try:
            rows = self._conn().execute("SELECT id FROM mesh_tasks").fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()

    def get_session_turns(self, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Return the session's tasks as conversation turns, oldest→newest.

        The conversation is a projection of the task ledger: each task row yields
        one user turn (``prompt``) and one assistant turn (``reply_text``). This is
        the file-free replacement for ``transcript.get_transcript`` — no artifact
        files, no NDJSON parsing. Rows lacking ``reply_text`` (not yet backfilled)
        are returned with ``reply_text=None`` so the caller can fall back.
        """
        rows = self._conn().execute(
            """
            SELECT id AS task_id, session_id, prompt, reply_text,
                   parsed_output_json, file_changes_json, files_modified_json,
                   usage_json, error_class, return_code, status, result,
                   created_at, completed_at
            FROM mesh_tasks
            WHERE session_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tasks(
        self,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM mesh_tasks {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # FlowRun record (v0.4 §13 item 1, A19) — one row per dispatch flow.
    #
    # This is a RECORD, not a driver/stage-machine. Nothing in the codebase
    # reads current_stage to decide what runs next; these methods only persist
    # and query the flow-state row. The orchestrator write hook is best-effort
    # (try/except) so a failure here can never fail or delay a real task.
    # ------------------------------------------------------------------

    def create_flow_run(
        self,
        task_id: str,
        current_stage: str,
        objective_lock: Optional[str] = None,
    ) -> str:
        """Insert a new flow_runs row. Returns the generated flow_run_id."""
        flow_run_id = uuid.uuid4().hex
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO flow_runs (
                    flow_run_id, task_id, current_stage, objective_lock, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (flow_run_id, task_id, current_stage, objective_lock, _now()),
            )
        return flow_run_id

    def update_flow_stage(self, flow_run_id: str, current_stage: str) -> None:
        """Update the current_stage of an existing flow_runs row."""
        with self._write() as conn:
            conn.execute(
                "UPDATE flow_runs SET current_stage = ? WHERE flow_run_id = ?",
                (current_stage, flow_run_id),
            )

    def list_flow_runs(
        self,
        task_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Read path for flow_runs. Optional task_id filter; newest first."""
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM flow_runs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Approvals (Move H) — durable approval gate. A pending approval is a
    # promise of a NOT-yet-dispatched action; resolving it is what triggers
    # dispatch. Persisting it here is what lets it survive a gateway restart
    # (an in-memory asyncio.Event would not) and rebuild the pending queue.
    # ------------------------------------------------------------------

    def create_approval(
        self,
        approval_id: str,
        action: str,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        risk: str = "medium",
        reversible: bool = True,
        requested_by: str = "",
        payload: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
    ) -> None:
        """Insert a new pending approval."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO approvals (
                        id, session_id, task_id, action, risk, reversible,
                        status, requested_by, payload, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        approval_id, session_id, task_id, action, risk,
                        1 if reversible else 0, requested_by,
                        json.dumps(payload) if payload is not None else None,
                        now, expires_at,
                    ),
                )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: approvals.id" in str(e):
                logger.debug("event=db_create_approval_duplicate id=%s", approval_id)
            else:
                logger.warning("event=db_create_approval_integrity_failed id=%s err=%s", approval_id, e)
        except Exception as e:
            logger.warning("event=db_create_approval_failed id=%s err=%s", approval_id, e)

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_approvals(
        self,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_approval(
        self, approval_id: str, status: str, resolved_by: str = ""
    ) -> bool:
        """Guarded transition: only a PENDING approval moves to a terminal status.

        Returns True iff exactly this call performed the transition. The
        ``status = 'pending'`` guard in the WHERE makes the resolve atomic — a
        concurrent double-resolve (two surfaces racing) results in exactly one
        True; the loser sees False (caller maps to already_resolved). This is the
        same optimistic-claim pattern as ``claim_task``.
        """
        if status not in ("approved", "rejected", "expired"):
            return False
        now = _now()
        try:
            with self._write() as conn:
                cur = conn.execute(
                    """
                    UPDATE approvals
                       SET status = ?, resolved_by = ?, resolved_at = ?
                     WHERE id = ? AND status = 'pending'
                    """,
                    (status, resolved_by, now, approval_id),
                )
                return cur.rowcount == 1
        except Exception as e:
            logger.warning("event=db_resolve_approval_failed id=%s err=%s", approval_id, e)
            return False

    # ------------------------------------------------------------------
    # Task events
    # ------------------------------------------------------------------

    def append_event(
        self,
        session_id: str,
        task_id: str,
        success: bool,
        execution_time: Optional[float] = None,
        error: str = "",
    ) -> None:
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO task_events
                        (session_id, task_id, timestamp, success, execution_time, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, task_id, _now(), int(success), execution_time, error),
                )
        except Exception as e:
            logger.warning("event=db_append_event_failed session_id=%s err=%s", session_id, e)

    def get_events(
        self,
        session_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM task_events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_id: str,
        tailscale_ip: str,
        api_port: int,
        backends: List[str],
        max_concurrent: int,
        status: str = "online",
        projects_root: str = "",
        repos: Optional[List[dict]] = None,
        incarnation_id: Optional[str] = None,
    ) -> str:
        """Upsert a node record and return the new incarnation_id.

        New workers provide a process incarnation_id that remains stable across
        controller restarts and re-registration. Older workers omit it, so we
        mint a fresh UUID and preserve the previous restart-detection behavior.
        list_stale_claims uses the mismatch between claimer_incarnation and the
        node's current incarnation_id to detect claims orphaned by a restart.
        """
        now = _now()
        incarnation_id = incarnation_id or uuid.uuid4().hex
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO nodes
                        (node_id, tailscale_ip, api_port, backends, max_concurrent,
                         status, last_heartbeat, registered_at, updated_at,
                         projects_root, repos, incarnation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        tailscale_ip   = excluded.tailscale_ip,
                        api_port       = excluded.api_port,
                        backends       = excluded.backends,
                        max_concurrent = excluded.max_concurrent,
                        status         = excluded.status,
                        last_heartbeat = excluded.last_heartbeat,
                        updated_at     = excluded.updated_at,
                        projects_root  = excluded.projects_root,
                        repos          = excluded.repos,
                        incarnation_id = excluded.incarnation_id
                    """,
                    (
                        node_id,
                        tailscale_ip,
                        api_port,
                        json.dumps(backends),
                        max_concurrent,
                        status,
                        now,
                        now,
                        now,
                        projects_root,
                        json.dumps(repos or []),
                        incarnation_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_upsert_node_failed node_id=%s err=%s", node_id, e)
        return incarnation_id

    def heartbeat_node(self, node_id: str, live_state: Optional[str] = None) -> None:
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE nodes
                    SET last_heartbeat = ?, status = 'online', updated_at = ?,
                        live_state = COALESCE(?, live_state),
                        live_state_updated_at = CASE WHEN ? IS NOT NULL THEN ? ELSE live_state_updated_at END
                    WHERE node_id = ?
                    """,
                    (now, now, live_state, live_state, now, node_id),
                )
        except Exception as e:
            logger.warning("event=db_heartbeat_node_failed node_id=%s err=%s", node_id, e)

    def mark_node_offline(self, node_id: str) -> None:
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE nodes SET status = 'offline', updated_at = ? WHERE node_id = ?",
                    (now, node_id),
                )
        except Exception as e:
            logger.warning("event=db_mark_node_offline_failed node_id=%s err=%s", node_id, e)

    def list_nodes(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self._conn().execute(
                "SELECT * FROM nodes WHERE status = ? ORDER BY node_id", (status,)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM nodes ORDER BY node_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Watched jobs
    # ------------------------------------------------------------------

    def register_job(
        self,
        job_id: str,
        node_id: str,
        label: str,
        session_id: Optional[str] = None,
        command: Optional[str] = None,
        cwd: Optional[str] = None,
        log_path: Optional[str] = None,
        notify: bool = True,
        notify_agent: bool = False,
    ) -> None:
        """Insert a new running job row."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs
                        (id, session_id, node_id, label, command, cwd, status,
                         started_at, started_epoch, log_path, notify, notify_agent,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, session_id, node_id, label, command, cwd,
                        now, time.time(), log_path,
                        1 if notify else 0, 1 if notify_agent else 0,
                        now, now,
                    ),
                )
        except sqlite3.IntegrityError:
            logger.debug("event=db_register_job_duplicate job_id=%s", job_id)
        except Exception as e:
            logger.warning("event=db_register_job_failed job_id=%s err=%s", job_id, e)

    def start_job(
        self,
        job_id: str,
        pid: int,
        pgid: int,
        log_path: Optional[str] = None,
        started_epoch: Optional[float] = None,
        observed_command: Optional[str] = None,
    ) -> None:
        """Record PID/PGID for a spawned job."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET pid = ?, pgid = ?, log_path = COALESCE(?, log_path),
                        started_epoch = COALESCE(?, started_epoch),
                        last_checked_at = ?,
                        last_probe_error = '',
                        last_seen_command = COALESCE(?, last_seen_command),
                        last_seen_started_epoch = COALESCE(?, last_seen_started_epoch),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        pid, pgid, log_path,
                        started_epoch, now,
                        observed_command, started_epoch,
                        now, job_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_start_job_failed job_id=%s err=%s", job_id, e)

    def record_job_probe(
        self,
        job_id: str,
        *,
        checked_at: Optional[str] = None,
        observed_command: Optional[str] = None,
        observed_started_epoch: Optional[float] = None,
        probe_error: str = "",
    ) -> None:
        """Persist the worker's latest process-identity probe for a running job."""
        now = checked_at or _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET last_checked_at = ?,
                        last_probe_error = ?,
                        last_seen_command = COALESCE(?, last_seen_command),
                        last_seen_started_epoch = COALESCE(?, last_seen_started_epoch),
                        updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        now,
                        probe_error,
                        observed_command,
                        observed_started_epoch,
                        now,
                        job_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_record_job_probe_failed job_id=%s err=%s", job_id, e)

    def complete_job(
        self,
        job_id: str,
        exit_code: int,
        tail: str = "",
    ) -> None:
        """Mark a running job as done."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'done', exit_code = ?, tail = ?,
                        finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (exit_code, tail, now, now, job_id),
                )
        except Exception as e:
            logger.warning("event=db_complete_job_failed job_id=%s err=%s", job_id, e)

    def fail_job(
        self,
        job_id: str,
        error: str,
        status: str = "failed",
    ) -> None:
        """Mark a running job as failed or lost."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, tail = ?, finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (status, error, now, now, job_id),
                )
        except Exception as e:
            logger.warning("event=db_fail_job_failed job_id=%s err=%s", job_id, e)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_jobs(
        self,
        node_id: Optional[str] = None,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        ownership: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if node_id:
            clauses.append("node_id = ?")
            params.append(node_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        elif ownership == "unowned":
            clauses.append("session_id IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_terminal_jobs_since(self, since: str) -> List[Dict[str, Any]]:
        """Return jobs that reached a terminal state after `since`."""
        rows = self._conn().execute(
            "SELECT * FROM jobs WHERE updated_at > ? AND status IN ('done', 'failed', 'lost')",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_running_jobs_for_node(self, node_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM jobs WHERE node_id = ? AND status = 'running'",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Mesh health samples (M5)
    # ------------------------------------------------------------------

    def record_mesh_health_sample(self, source: str = "manual") -> Dict[str, Any]:
        """Append one aggregate mesh health sample and enforce retention."""
        snapshot = self.stats()
        mesh_load = snapshot.get("mesh_load") or {}
        sampled_at: str = _now()
        row: Dict[str, Any] = {
            "sampled_at": sampled_at,
            "source": source,
            "sessions_busy": int(snapshot.get("sessions_busy") or 0),
            "tasks_pending": int(snapshot.get("tasks_pending") or 0),
            "tasks_claimed": int(snapshot.get("tasks_claimed") or 0),
            "nodes_online": int(snapshot.get("nodes_online") or 0),
            "nodes_total": int(snapshot.get("nodes_total") or 0),
            "slots_used": int(mesh_load.get("slots_used") or 0),
            "slots_total": int(mesh_load.get("slots_total") or 0),
            "slots_available": int(mesh_load.get("slots_available") or 0),
            "active_tasks": int(mesh_load.get("active_tasks") or 0),
            "stale_busy_sessions": int(mesh_load.get("stale_busy_sessions") or 0),
            "nodes_with_live_state": int(mesh_load.get("nodes_with_live_state") or 0),
            "nodes_without_live_state": int(mesh_load.get("nodes_without_live_state") or 0),
            "stale_live_state_nodes_json": json.dumps(mesh_load.get("stale_live_state_nodes") or []),
        }
        try:
            with self._write() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO mesh_health_samples (
                        sampled_at, source, sessions_busy, tasks_pending,
                        tasks_claimed, nodes_online, nodes_total, slots_used,
                        slots_total, slots_available, active_tasks,
                        stale_busy_sessions, nodes_with_live_state,
                        nodes_without_live_state, stale_live_state_nodes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["sampled_at"],
                        row["source"],
                        row["sessions_busy"],
                        row["tasks_pending"],
                        row["tasks_claimed"],
                        row["nodes_online"],
                        row["nodes_total"],
                        row["slots_used"],
                        row["slots_total"],
                        row["slots_available"],
                        row["active_tasks"],
                        row["stale_busy_sessions"],
                        row["nodes_with_live_state"],
                        row["nodes_without_live_state"],
                        row["stale_live_state_nodes_json"],
                    ),
                )
                row["id"] = int(cur.lastrowid)
            self.prune_mesh_health_samples()
        except Exception as e:
            logger.warning("event=db_record_mesh_health_sample_failed err=%s", e)
        return self._decode_mesh_health_sample(row)

    def maybe_record_mesh_health_sample(
        self,
        source: str = "manual",
        *,
        min_interval_seconds: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """Append a sample at most once per source interval."""
        now = time.monotonic()
        with _mesh_health_sample_lock:
            last = _mesh_health_last_sample.get(source)
            if last is not None and now - last < max(1.0, min_interval_seconds):
                return None
            _mesh_health_last_sample[source] = now
        return self.record_mesh_health_sample(source=source)

    def list_mesh_health_samples(
        self,
        *,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 1000))
        params: List[Any] = []
        where = ""
        if since:
            where = "WHERE sampled_at >= ?"
            params.append(since)
        params.append(bounded_limit)
        rows = self._conn().execute(
            f"""
            SELECT * FROM mesh_health_samples
            {where}
            ORDER BY sampled_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._decode_mesh_health_sample(dict(r)) for r in rows]

    def prune_mesh_health_samples(
        self,
        *,
        retention_hours: int = 48,
        max_rows: int = 10000,
    ) -> None:
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=max(1, retention_hours))).isoformat()
        try:
            with self._write() as conn:
                conn.execute("DELETE FROM mesh_health_samples WHERE sampled_at < ?", (cutoff,))
                conn.execute(
                    """
                    DELETE FROM mesh_health_samples
                    WHERE id NOT IN (
                        SELECT id FROM mesh_health_samples
                        ORDER BY sampled_at DESC
                        LIMIT ?
                    )
                    """,
                    (max(1, max_rows),),
                )
        except Exception as e:
            logger.debug("event=db_prune_mesh_health_samples_failed err=%s", e)

    def _decode_mesh_health_sample(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raw_nodes = row.pop("stale_live_state_nodes_json", "[]")
        try:
            stale_nodes = json.loads(raw_nodes) if isinstance(raw_nodes, str) else raw_nodes
        except Exception:
            stale_nodes = []
        row["stale_live_state_nodes"] = stale_nodes if isinstance(stale_nodes, list) else []
        return row

    # ------------------------------------------------------------------
    # Web Push subscriptions (#21)
    # ------------------------------------------------------------------

    def upsert_push_subscription(
        self,
        endpoint: str,
        p256dh_key: str,
        auth_key: str,
        label: Optional[str] = None,
    ) -> None:
        """Insert or refresh a browser push subscription (idempotent by endpoint).

        Re-subscribing from the same browser re-enables and refreshes keys/label.
        """
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO push_subscriptions
                        (endpoint, p256dh_key, auth_key, enabled, label,
                         last_error, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, NULL, ?, ?)
                    ON CONFLICT(endpoint) DO UPDATE SET
                        p256dh_key = excluded.p256dh_key,
                        auth_key   = excluded.auth_key,
                        enabled    = 1,
                        label      = excluded.label,
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (endpoint, p256dh_key, auth_key, label, now, now),
                )
        except Exception as e:
            logger.warning("event=db_upsert_push_subscription_failed err=%s", e)

    def list_push_subscriptions(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        try:
            if enabled_only:
                rows = self._conn().execute(
                    "SELECT * FROM push_subscriptions WHERE enabled = 1 ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT * FROM push_subscriptions ORDER BY created_at"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("event=db_list_push_subscriptions_failed err=%s", e)
            return []

    def disable_push_subscription(self, endpoint: str) -> None:
        """Disable a subscription (unsubscribe or after a permanent send error)."""
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE push_subscriptions SET enabled = 0, updated_at = ? WHERE endpoint = ?",
                    (_now(), endpoint),
                )
        except Exception as e:
            logger.warning("event=db_disable_push_subscription_failed err=%s", e)

    def mark_push_error(self, endpoint: str, error: str) -> None:
        """Record the last transient send error without disabling the subscription."""
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE push_subscriptions SET last_error = ?, updated_at = ? WHERE endpoint = ?",
                    (str(error)[:500], _now(), endpoint),
                )
        except Exception as e:
            logger.debug("event=db_mark_push_error_failed err=%s", e)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the per-thread connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def stats(self) -> Dict[str, Any]:
        """Quick health snapshot — useful for /status Telegram command."""
        conn = self._conn()
        nodes = self.list_nodes()
        mesh_load = _mesh_load_stats(nodes, self.list_stale_busy_sessions(limit=10000))
        return {
            "sessions_total":   conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "sessions_busy":    conn.execute("SELECT COUNT(*) FROM sessions WHERE status='busy'").fetchone()[0],
            "tasks_pending":    conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status='pending'").fetchone()[0],
            "tasks_claimed":    conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status='claimed'").fetchone()[0],
            "tasks_completed":  conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status='completed'").fetchone()[0],
            "tasks_failed":     conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status IN ('failed','failed_node_offline')").fetchone()[0],
            # Derived from the same `nodes` snapshot as mesh_load (consistent view).
            # nodes_total is the current fleet, not every row ever registered, so
            # long-dead test/canary inventory no longer inflates "N/M online".
            "nodes_online":     sum(1 for n in nodes if n.get("status") == "online"),
            "nodes_total":      _count_fleet_nodes(nodes),
            "schema_version":   conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0,
            "db_path":          str(self._path),
            "mesh_load":        mesh_load,
        }


# ---------------------------------------------------------------------------
# Migrations — add future ALTER TABLE statements here
# ---------------------------------------------------------------------------

def _get_migrations() -> List[tuple]:
    """Return list of (version, sql) tuples in ascending version order.

    Version 1 is the baseline — it's recorded after the initial _DDL runs
    so the migration framework has a clean starting point.

    To add a migration:
        1. Append (N, "ALTER TABLE ...") to this list.
        2. Bump _CURRENT_VERSION to N.
    """
    return [
        (1, ""),  # baseline marker — DDL already applied above
        (2, "ALTER TABLE nodes ADD COLUMN projects_root TEXT NOT NULL DEFAULT ''"),
        (3, "ALTER TABLE nodes ADD COLUMN repos TEXT NOT NULL DEFAULT '[]'"),
        (4, ""),  # jobs table added to _DDL; marker for clean version tracking
        (5, "ALTER TABLE jobs ADD COLUMN cwd TEXT"),  # working directory for spawn mode
        (6, "ALTER TABLE nodes ADD COLUMN incarnation_id TEXT"),  # per-restart UUID for orphan detection
        (7, "ALTER TABLE mesh_tasks ADD COLUMN claimer_incarnation TEXT"),  # matched against nodes.incarnation_id by reaper
        (8, "ALTER TABLE nodes ADD COLUMN live_state TEXT"),  # JSON snapshot sent with each heartbeat (slots, active tasks)
        (9, "ALTER TABLE nodes ADD COLUMN live_state_updated_at TEXT"),  # timestamp of last live_state update; NULL = never received
        (10, """
            ALTER TABLE jobs ADD COLUMN last_checked_at TEXT;
            ALTER TABLE jobs ADD COLUMN last_probe_error TEXT;
            ALTER TABLE jobs ADD COLUMN last_seen_command TEXT;
            ALTER TABLE jobs ADD COLUMN last_seen_started_epoch REAL
        """),  # durable watched-job process identity probes
        (11, "ALTER TABLE sessions ADD COLUMN model TEXT"),  # per-session picked model; NULL = backend default
        (12, "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT '{\"channel\":\"telegram\",\"kind\":\"user\"}'"),  # transport-neutral origin tag {channel, kind}; old rows default to telegram/user
        (13, _APPROVALS_SCHEMA_SQL),  # Web UI lineage: durable approval gate
        (14, _LLM_TELEMETRY_SCHEMA_SQL),  # main lineage: durable LLM telemetry
        (15, ""),  # marker retained for main telemetry history compatibility
        (16, ""),  # merged-lineage marker; _ensure_merged_schema repairs both paths
        (17, """
            ALTER TABLE mesh_tasks ADD COLUMN prompt TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN reply_text TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN parsed_output_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN file_changes_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN files_modified_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN usage_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN error_class TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN return_code INTEGER
        """),  # artifact-complete task rows: full reply + structured fields so
               # the DB is self-sufficient and results/task_*.json can be dropped.
               # reply_text holds the FULL untruncated assistant reply (the chat
               # source); the legacy `result` JSON keeps output[:2000] for back-compat.
        (18, """
            ALTER TABLE sessions ADD COLUMN driver_type TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN driver_status TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN cache_health TEXT NOT NULL DEFAULT 'unknown';
            ALTER TABLE sessions ADD COLUMN cache_unhealthy_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE sessions ADD COLUMN previous_backend_session_ids TEXT NOT NULL DEFAULT '[]'
        """),  # P0 replacement-engine driver state: persisted so the
               # cache_unhealthy_count>=2 guard and driver_status=lost guard
               # survive across turns and gateway restarts.
        (19, """
            CREATE TABLE IF NOT EXISTS mesh_health_samples (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at                  TEXT NOT NULL,
                source                      TEXT NOT NULL,
                sessions_busy               INTEGER NOT NULL DEFAULT 0,
                tasks_pending               INTEGER NOT NULL DEFAULT 0,
                tasks_claimed               INTEGER NOT NULL DEFAULT 0,
                nodes_online                INTEGER NOT NULL DEFAULT 0,
                nodes_total                 INTEGER NOT NULL DEFAULT 0,
                slots_used                  INTEGER NOT NULL DEFAULT 0,
                slots_total                 INTEGER NOT NULL DEFAULT 0,
                slots_available             INTEGER NOT NULL DEFAULT 0,
                active_tasks                INTEGER NOT NULL DEFAULT 0,
                stale_busy_sessions         INTEGER NOT NULL DEFAULT 0,
                nodes_with_live_state       INTEGER NOT NULL DEFAULT 0,
                nodes_without_live_state    INTEGER NOT NULL DEFAULT 0,
                stale_live_state_nodes_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_mesh_health_samples_sampled_at
                ON mesh_health_samples(sampled_at)
        """),  # M5 operational mesh health trend ledger.
        (20, """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint    TEXT PRIMARY KEY,
                p256dh_key  TEXT NOT NULL,
                auth_key    TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                label       TEXT,
                last_error  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """),  # #21 Web Push: durable browser push subscriptions. endpoint is the
               # natural key (unique per browser/device); re-subscribe is an upsert.
        (21, """
            CREATE TABLE IF NOT EXISTS flow_runs (
                flow_run_id     TEXT PRIMARY KEY,
                task_id         TEXT,
                current_stage   TEXT,
                objective_lock  TEXT,
                created_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_flow_runs_task
                ON flow_runs(task_id)
        """),  # A19 v0.4 §13 item 1: FlowRun RECORD (not a stage machine). One row
               # per dispatch flow; nothing reads current_stage to drive behavior.
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    # Always produce a timezone-aware UTC string so the browser can correctly
    # convert to local time. datetime.utcnow() produced naive strings that JS
    # treated as local time, causing a 3-hour clock skew vs telemetry timestamps
    # (which are always UTC-aware).
    return datetime.now(tz=timezone.utc).isoformat()


def _origin_json(origin: Any) -> str:
    """Serialize a SessionOrigin (or None) to the stored {channel, kind} JSON.

    db.py stays free of core imports, so this reads attributes duck-typed and
    falls back to the telegram/user default when origin is missing.
    """
    channel = getattr(origin, "channel", None) or "telegram"
    kind = getattr(origin, "kind", None) or "user"
    return json.dumps({"channel": channel, "kind": kind})


# A node not seen within this window is decommissioned inventory (e.g. old
# test/canary rows never pruned), not part of the current fleet. Excluding it
# keeps "nodes online N/M" honest instead of counting long-dead ghosts.
_NODE_FLEET_RETENTION_SEC = 2 * 86400


def _count_fleet_nodes(nodes: List[Dict[str, Any]]) -> int:
    """Count nodes that belong to the *current* fleet: online, or offline but
    heartbeated within the retention window. Long-dead inventory is excluded."""
    now = datetime.now(timezone.utc)
    count = 0
    for row in nodes:
        if row.get("status") == "online":
            count += 1
            continue
        hb = row.get("last_heartbeat")
        if not hb:
            continue
        try:
            ts = datetime.fromisoformat(str(hb))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - ts).total_seconds() <= _NODE_FLEET_RETENTION_SEC:
            count += 1
    return count


def _mesh_load_stats(nodes: List[Dict[str, Any]], stale_busy: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Aggregate node live_state blobs into network-wide slot/task counters.

    Only ``online`` nodes contribute. An offline node holds no live capacity and
    is not "heartbeating but scheduler-invisible" — it is simply gone. Counting
    offline nodes here previously (a) inflated ``slots_total`` with dead-node
    capacity and (b) reported long-dead inventory in ``nodes_without_live_state``
    as if it were live-but-silent, producing the misleading
    "N nodes heartbeating but scheduler-invisible" banner.
    """
    slots_used = 0
    slots_total = 0
    active_tasks = 0
    nodes_with_state = 0
    nodes_without_state = 0
    stale_state_nodes: List[str] = []

    # tz-aware "now": live_state_updated_at is written tz-aware (+00:00) by the
    # registry heartbeat, so subtracting it from a naive utcnow() raised
    # TypeError — caught below — which silently marked EVERY fresh online node
    # stale (zeroing slots and dropping active tasks). Compare aware-to-aware.
    now = datetime.now(timezone.utc)
    _live_state_max_age_s = 120
    for row in nodes:
        # Offline nodes are inventory, not live mesh — skip them entirely.
        if row.get("status") != "online":
            continue

        live_raw = row.get("live_state")
        live: Dict[str, Any] = {}
        if isinstance(live_raw, dict):
            live = live_raw
        elif isinstance(live_raw, str) and live_raw.strip():
            try:
                parsed = json.loads(live_raw)
                if isinstance(parsed, dict):
                    live = parsed
            except Exception:
                live = {}

        # Check staleness before aggregating — stale live_state must not
        # contribute phantom slot/task counts to the mesh totals.
        live_is_fresh = False
        updated = row.get("live_state_updated_at")
        if live and updated:
            try:
                parsed_ts = datetime.fromisoformat(str(updated))
                if parsed_ts.tzinfo is None:
                    # Legacy naive timestamps are assumed UTC (that's how they
                    # were written before the registry moved to tz-aware).
                    parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                age_s = (now - parsed_ts).total_seconds()
                live_is_fresh = age_s <= _live_state_max_age_s
            except Exception:
                live_is_fresh = False
        elif live and not updated:
            live_is_fresh = False  # live_state present but timestamp missing — treat as stale

        if live and live_is_fresh:
            nodes_with_state += 1
            try:
                slots_used += int(live.get("slots_used") or 0)
            except Exception:
                pass
            try:
                slots_total += int(live.get("slots_total") or row.get("max_concurrent") or 0)
            except Exception:
                pass
            tasks = live.get("active_tasks")
            if isinstance(tasks, list):
                active_tasks += len(tasks)
        else:
            nodes_without_state += 1
            try:
                slots_total += int(row.get("max_concurrent") or 0)
            except Exception:
                pass

        # Reached only for online nodes (offline skipped above): an online node
        # with missing/stale live_state is genuinely reporting-silent.
        if not updated or not live_is_fresh:
            stale_state_nodes.append(row.get("node_id", ""))

    return {
        "slots_used": slots_used,
        "slots_total": slots_total,
        "slots_available": max(slots_total - slots_used, 0),
        "active_tasks": active_tasks,
        "nodes_with_live_state": nodes_with_state,
        "nodes_without_live_state": nodes_without_state,
        "stale_live_state_nodes": [n for n in stale_state_nodes if n],
        "stale_busy_sessions": len(stale_busy or []),
    }


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------

_db_instance: Optional[MeshDB] = None
_db_lock = threading.Lock()


def get_db() -> Optional[MeshDB]:
    """Return the singleton MeshDB if shadow_write is enabled, else None.

    The first call initialises the DB.  Subsequent calls return the cached
    instance.  Returns None when mesh.shadow_write is False so callers can
    guard with a simple `if db:` check.
    """
    global _db_instance
    if _db_instance is not None:
        return _db_instance
    with _db_lock:
        if _db_instance is not None:
            return _db_instance
        try:
            from config import config as _cfg
            if not _cfg.mesh.shadow_write:
                return None
            project_root = Path(__file__).resolve().parent.parent.parent
            db_path = Path(_cfg.mesh.db_path)
            if not db_path.is_absolute():
                db_path = project_root / db_path
            _db_instance = MeshDB(str(db_path))
            logger.info("event=mesh_db_ready path=%s", db_path)
        except Exception as e:
            logger.warning("event=mesh_db_init_failed err=%s — shadow writes disabled", e)
            return None
    return _db_instance
