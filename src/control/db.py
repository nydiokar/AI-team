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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — bump when adding migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION = 10


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
                        created_at, updated_at, machine_id, backend_session_id,
                        last_task_id, last_artifact_path, last_summary,
                        last_user_message, last_result_summary, last_files_modified,
                        telegram_chat_id, telegram_thread_id, owner_user_id, task_history
                    ) VALUES (
                        :session_id, :backend, :repo_path, :status,
                        :created_at, :updated_at, :machine_id, :backend_session_id,
                        :last_task_id, :last_artifact_path, :last_summary,
                        :last_user_message, :last_result_summary, :last_files_modified,
                        :telegram_chat_id, :telegram_thread_id, :owner_user_id, :task_history
                    )
                    ON CONFLICT(session_id) DO UPDATE SET
                        backend             = excluded.backend,
                        repo_path           = excluded.repo_path,
                        status              = excluded.status,
                        updated_at          = excluded.updated_at,
                        machine_id          = excluded.machine_id,
                        backend_session_id  = excluded.backend_session_id,
                        last_task_id        = excluded.last_task_id,
                        last_artifact_path  = excluded.last_artifact_path,
                        last_summary        = excluded.last_summary,
                        last_user_message   = excluded.last_user_message,
                        last_result_summary = excluded.last_result_summary,
                        last_files_modified = excluded.last_files_modified,
                        telegram_chat_id    = excluded.telegram_chat_id,
                        telegram_thread_id  = excluded.telegram_thread_id,
                        owner_user_id       = excluded.owner_user_id,
                        task_history        = excluded.task_history
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
                    },
                )
        except Exception as e:
            logger.warning("event=db_upsert_session_failed session_id=%s err=%s", session.session_id, e)

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

    def list_stale_claims(self, lease_sec: int = 300) -> List[Dict[str, Any]]:
        """Return claimed tasks whose claim has expired.

        A claim is stale when:
        - `claimed_at` is older than `lease_sec` seconds ago, AND one of:
          - the claiming node no longer exists in the nodes table, OR
          - the claiming node is offline (missed heartbeats), OR
          - the claiming node is online but its current incarnation_id differs
            from claimer_incarnation (node restarted in-place — new process,
            same node_id, same online status — the dead process's claim is
            orphaned and will never complete).

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
                SELECT t.*, n.status AS node_status
                FROM mesh_tasks t
                LEFT JOIN nodes n ON t.claimed_by = n.node_id
                WHERE t.status = 'claimed'
                  AND t.claimed_at IS NOT NULL
                  AND (
                    n.node_id IS NULL
                    OR n.status = 'offline'
                    OR (
                      n.status = 'online'
                      AND t.claimer_incarnation IS NOT NULL
                      AND n.incarnation_id IS NOT NULL
                      AND n.incarnation_id != t.claimer_incarnation
                    )
                  )
                """,
            ).fetchall()
            conn.close()
            now = datetime.utcnow()
            cutoff = lease_sec
            result = []
            for r in rows:
                claimed_str = r["claimed_at"]
                try:
                    claimed_dt = datetime.fromisoformat(claimed_str)
                except Exception:
                    claimed_dt = datetime.strptime(claimed_str, "%Y-%m-%d %H:%M:%S.%f")
                age = (now - claimed_dt).total_seconds()
                if age > cutoff:
                    d = dict(r)
                    d.pop("node_status", None)
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
    ) -> None:
        """Mark a task as failed. status can be 'failed' or 'failed_node_offline'."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = ?, error = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, now, now, task_id),
                )
        except Exception as e:
            logger.warning("event=db_fail_task_failed task_id=%s err=%s", task_id, e)

    def get_pending_tasks(
        self,
        node_id: Optional[str] = None,
        backends: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return pending tasks routable to this node.

        machine_id=NULL means any node can claim it.
        machine_id=<node_id> means only that node can claim it (session affinity).
        """
        params: List[Any] = []
        machine_clause = ""
        if node_id:
            machine_clause = "AND (machine_id IS NULL OR machine_id = ?)"
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
    ) -> str:
        """Upsert a node record and return the new incarnation_id.

        A fresh incarnation_id UUID is minted on every call — each registration
        represents a new process start, even when the node_id is unchanged.
        list_stale_claims uses the mismatch between claimer_incarnation and the
        node's current incarnation_id to detect claims orphaned by a restart.
        """
        now = _now()
        incarnation_id = uuid.uuid4().hex
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
            "nodes_online":     conn.execute("SELECT COUNT(*) FROM nodes WHERE status='online'").fetchone()[0],
            "nodes_total":      conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
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
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()


def _mesh_load_stats(nodes: List[Dict[str, Any]], stale_busy: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Aggregate node live_state blobs into network-wide slot/task counters."""
    slots_used = 0
    slots_total = 0
    active_tasks = 0
    nodes_with_state = 0
    nodes_without_state = 0
    stale_state_nodes: List[str] = []

    now = datetime.utcnow()
    for row in nodes:
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

        if live:
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

        updated = row.get("live_state_updated_at")
        if row.get("status") == "online" and not updated:
            stale_state_nodes.append(row.get("node_id", ""))
        elif row.get("status") == "online" and updated:
            try:
                age_s = (now - datetime.fromisoformat(str(updated))).total_seconds()
                if age_s > 120:
                    stale_state_nodes.append(row.get("node_id", ""))
            except Exception:
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
