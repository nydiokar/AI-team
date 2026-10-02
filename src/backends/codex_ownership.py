"""Private durable ownership guard for the one Codex backend.

It prevents two carriers from mutating the same native thread while preserving
the workspace affinity of that thread.
Claims deliberately have no TTL: losing a gateway/worker does not prove its
child stopped. It does not start Codex or translate its protocol.

Cancellation uses the shared process-tree helper's approximately eight-second
grace period, so a normal cancelled turn can take that long to settle.

[A82 step 4a] Managed turns add two durable facts: the identity of the
``codex app-server`` process each managed owner drives (``owner_processes``), and
a write-ahead ``managed_turns`` map from the carrier's turn uuid to the native
thread/turn ids. A no-TTL owner row is cleared ONLY with proof that its
app-server is gone (all of that process's turns are then provably stopped);
an owner without a recorded identity stays unknown ⇒ busy.
"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import BaseModel

ACTIVE_STATES = ("submitting", "started")


class ManagedTurnRecord(BaseModel):
    turn_uuid: str
    session_key: str
    thread_id: str
    native_turn_id: str
    kind: str
    state: str
    owner: str
    process: dict

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
            conn.execute("CREATE TABLE IF NOT EXISTS owner_processes (owner TEXT PRIMARY KEY, process TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS managed_turns (turn_uuid TEXT PRIMARY KEY, "
                         "session_key TEXT NOT NULL, thread_id TEXT NOT NULL, native_turn_id TEXT NOT NULL, "
                         "kind TEXT NOT NULL, state TEXT NOT NULL, owner TEXT NOT NULL, process TEXT NOT NULL)")

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

    def acquire(self, session_key: str, thread_id: str, cwd: str = "", *, process: dict | None = None) -> str:
        self.session_key = session_key
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if process:
                # [A82 step 4a] The only basis on which a successor may clear
                # this owner's no-TTL rows: proof this app-server is gone.
                conn.execute("INSERT OR REPLACE INTO owner_processes VALUES (?, ?)",
                             (self.owner, json.dumps(process)))
            row = conn.execute("SELECT thread_id FROM threads WHERE session_key = ?", (session_key,)).fetchone()
            if row and thread_id and row[0] != thread_id:
                raise RuntimeError("codex_thread_identity_mismatch")
            # A stale remote payload must never start a second fresh thread.
            self.thread_id = row[0] if row else thread_id
            keys = [f"session:{session_key}"]
            if self.thread_id:
                keys.append(f"thread:{self.thread_id}")
            if cwd:
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

    def thread_for(self, session_key: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT thread_id FROM threads WHERE session_key = ?", (session_key,)).fetchone()
        return row[0] if row else ""

    def release(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM owners WHERE owner = ?", (self.owner,))
            conn.execute("DELETE FROM owner_processes WHERE owner = ?", (self.owner,))

    # ---------------------------------------------------------------- #
    # [A82 step 4a] Managed-turn write-ahead map + proof-based recovery.
    # ---------------------------------------------------------------- #
    def begin_managed(self, turn_uuid: str, thread_id: str, kind: str, process: dict) -> None:
        """Write-ahead BEFORE the prompt is submitted. A row that already exists
        for this uuid means an earlier life may have submitted it: raise (the
        caller reports recovery; never a blind re-submit)."""
        with self._connect() as conn:
            try:
                conn.execute("INSERT INTO managed_turns VALUES (?, ?, ?, '', ?, 'submitting', ?, ?)",
                             (turn_uuid, self.session_key, thread_id, kind, self.owner, json.dumps(process)))
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("codex_managed_turn_already_begun") from exc

    def bind_native_turn(self, turn_uuid: str, native_turn_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE managed_turns SET native_turn_id = ?, state = 'started' "
                         "WHERE turn_uuid = ? AND state = 'submitting'", (native_turn_id, turn_uuid))

    def finish_managed(self, turn_uuid: str, state: str) -> bool:
        """Terminal state for a still-active row; True iff it changed."""
        with self._connect() as conn:
            return conn.execute(
                f"UPDATE managed_turns SET state = ? WHERE turn_uuid = ? AND state IN {ACTIVE_STATES}",
                (state, turn_uuid)).rowcount > 0

    def managed_turn(self, turn_uuid: str) -> ManagedTurnRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM managed_turns WHERE turn_uuid = ?", (turn_uuid,)).fetchone()
        return _record(row) if row else None

    def clear_dead_owners(self, session_key: str, thread_id: str,
                          gone: Callable[[dict], dict | None]) -> bool:
        """True iff no OTHER owner holds the session/thread (or its active
        managed rows) once every owner whose app-server is provably gone is
        cleared: its owner rows dropped, its active managed rows ``stopped``.
        An owner with no recorded identity, or a live one, is never touched."""
        keys = [f"session:{session_key}"] + ([f"thread:{thread_id}"] if thread_id else [])
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" * len(keys))
            held = {r[0] for r in conn.execute(
                f"SELECT owner FROM owners WHERE key IN ({marks}) AND owner != ?", (*keys, self.owner))}
            held |= {r[0] for r in conn.execute(
                f"SELECT owner FROM managed_turns WHERE session_key = ? AND state IN {ACTIVE_STATES} "
                "AND owner != ?", (session_key, self.owner))}
            for owner in held:
                identity = conn.execute("SELECT process FROM owner_processes WHERE owner = ?", (owner,)).fetchone()
                if identity is None:
                    rows = conn.execute("SELECT process FROM managed_turns WHERE owner = ? LIMIT 1",
                                        (owner,)).fetchone()
                    identity = rows
                process = json.loads(identity[0]) if identity else {}
                if not process or gone(process) is None:
                    return False
            for owner in held:
                conn.execute("DELETE FROM owners WHERE owner = ?", (owner,))
                conn.execute("DELETE FROM owner_processes WHERE owner = ?", (owner,))
                conn.execute(f"UPDATE managed_turns SET state = 'stopped' WHERE owner = ? "
                             f"AND state IN {ACTIVE_STATES}", (owner,))
        return True


def _record(row: tuple) -> ManagedTurnRecord:
    turn_uuid, session_key, thread_id, native_turn_id, kind, state, owner, process = row
    return ManagedTurnRecord(turn_uuid=turn_uuid, session_key=session_key, thread_id=thread_id,
                             native_turn_id=native_turn_id, kind=kind, state=state, owner=owner,
                             process=json.loads(process or "{}"))
