"""Host-local, fail-closed ownership for Codex native execution.

Claims deliberately have no TTL: losing a gateway/worker does not prove its
child stopped. An unclean exit requires operator recovery after verifying the
old process tree is dead. All carriers sharing CODEX_HOME share this database.

Cancellation uses the shared process-tree helper's approximately eight-second
grace period, so a normal cancelled turn can take that long to settle.
"""
import os
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

_CARRIER_OWNER: str = uuid.uuid4().hex


class CodexOwnership:
    def __init__(self) -> None:
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "gateway-ownership.sqlite3"
        self.owner = uuid.uuid4().hex
        self.session_key = ""
        self.thread_id = ""
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS owners (key TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS threads (session_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS cancellations (task_id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE IF NOT EXISTS affinities (key TEXT PRIMARY KEY, cwd TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS workspace_claims (owner TEXT PRIMARY KEY, cwd TEXT NOT NULL, domain TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS workspace_claims_cwd ON workspace_claims(cwd)")

    def request_cancel(self, task_id: str) -> None:
        if not task_id or len(task_id) > 256:
            raise ValueError("Invalid Codex cancellation task ID")
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO cancellations VALUES (?)", (task_id,))

    def cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM cancellations WHERE task_id = ?", (task_id,)).fetchone() is not None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def acquire(self, session_key: str, thread_id: str, cwd: str = "") -> str:
        self.session_key = session_key
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT thread_id FROM threads WHERE session_key = ?", (session_key,)).fetchone()
            if row and thread_id and row[0] != thread_id:
                raise RuntimeError("codex_thread_identity_mismatch")
            # A stale remote payload must never start a second fresh thread.
            self.thread_id = row[0] if row else thread_id
            keys = [f"session:{session_key}"]
            if self.thread_id:
                keys.append(f"thread:{self.thread_id}")
            if cwd:
                existing = conn.execute("SELECT domain FROM workspace_claims WHERE cwd = ? LIMIT 1", (cwd,)).fetchone()
                if existing and existing[0] != _CARRIER_OWNER:
                    raise RuntimeError("codex_workspace_busy: another carrier owns this workspace")
                conn.execute("INSERT INTO workspace_claims VALUES (?, ?, ?)", (self.owner, cwd, _CARRIER_OWNER))
                for key in keys:
                    affinity = conn.execute("SELECT cwd FROM affinities WHERE key = ?", (key,)).fetchone()
                    if affinity and affinity[0] != cwd:
                        raise RuntimeError("codex_workspace_mismatch")
                    conn.execute("INSERT OR IGNORE INTO affinities VALUES (?, ?)", (key, cwd))
            for key in keys:
                try:
                    conn.execute("INSERT INTO owners VALUES (?, ?, ?)", (key, self.owner, os.getpid()))
                except sqlite3.IntegrityError as exc:
                    raise RuntimeError("codex_thread_busy: execution ownership is held; unclean exits require operator recovery") from exc
            if self.thread_id:
                conn.execute("INSERT OR REPLACE INTO threads VALUES (?, ?)", (session_key, self.thread_id))
        return self.thread_id

    def record_thread(self, thread_id: str) -> None:
        if not thread_id or thread_id == self.thread_id:
            return
        if self.thread_id:
            raise RuntimeError("Codex changed the exact resumed thread ID")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO owners VALUES (?, ?, ?)", (f"thread:{thread_id}", self.owner, os.getpid()))
            conn.execute("INSERT OR REPLACE INTO threads VALUES (?, ?)", (self.session_key, thread_id))
            affinity = conn.execute("SELECT cwd FROM affinities WHERE key = ?", (f"session:{self.session_key}",)).fetchone()
            if affinity:
                conn.execute("INSERT OR IGNORE INTO affinities VALUES (?, ?)", (f"thread:{thread_id}", affinity[0]))
        self.thread_id = thread_id

    def release(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM owners WHERE owner = ?", (self.owner,))
            conn.execute("DELETE FROM workspace_claims WHERE owner = ?", (self.owner,))
