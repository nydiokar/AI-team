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

[A82 pre-cutover, m2] ``sweep_legacy_owners`` (the operator / Stage 8 cutover
exit for identity-less legacy owners) also refuses while ANY ``codex
app-server`` using the same ``CODEX_HOME`` is alive, found via
``/proc/<pid>/cmdline`` + ``/proc/<pid>/environ``; an app-server whose environ
is unreadable, or a host without ``/proc``, fails closed (refused).
Container caveat: ``/proc`` shows only the caller's pid namespace. An
app-server in ANOTHER container/namespace sharing the same ``CODEX_HOME``
volume is invisible here — run the sweep in the namespace of every carrier
that mounts that ``CODEX_HOME`` (or with all of them stopped).
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
        caller reports recovery; never a blind re-submit) — unless that row is
        ``not_submitted``: refused before submission, provably unsent, so the
        requeued attempt begins again ([A82 step 4 rework, m1])."""
        with self._connect() as conn:
            try:
                conn.execute("INSERT INTO managed_turns VALUES (?, ?, ?, '', ?, 'submitting', ?, ?)",
                             (turn_uuid, self.session_key, thread_id, kind, self.owner, json.dumps(process)))
            except sqlite3.IntegrityError as exc:
                if conn.execute(
                        "UPDATE managed_turns SET session_key = ?, thread_id = ?, native_turn_id = '', "
                        "kind = ?, state = 'submitting', owner = ?, process = ? "
                        "WHERE turn_uuid = ? AND state = 'not_submitted'",
                        (self.session_key, thread_id, kind, self.owner, json.dumps(process),
                         turn_uuid)).rowcount != 1:
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


def _owner_process_gone(pid: int) -> bool:
    """True only with proof the pid no longer exists (fail closed)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import psutil
            return not psutil.pid_exists(pid)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


class LegacySweepRefused(RuntimeError):
    """[A82 pre-cutover, m2] The cutover sweep cannot prove no app-server
    still uses this ``CODEX_HOME``; nothing was cleared."""


def _live_app_servers(codex_home: Path, proc_root: Path) -> list[str]:
    """Pids (as text) of live ``codex app-server`` processes whose effective
    ``CODEX_HOME`` (env, else ``$HOME/.codex``) is ``codex_home``; a codex
    app-server whose environ cannot be read counts (fail closed). Raises
    ``LegacySweepRefused`` when the process table itself is unreadable."""
    target = codex_home.resolve()
    try:
        entries = [e for e in proc_root.iterdir() if e.name.isdigit() and int(e.name) != os.getpid()]
    except OSError as exc:
        raise LegacySweepRefused(f"process table unreadable at {proc_root}: {exc}") from exc
    found: list[str] = []
    for entry in entries:
        try:
            argv = [a.decode(errors="replace") for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except FileNotFoundError:
            continue  # exited meanwhile
        except OSError:
            found.append(f"{entry.name}(cmdline unreadable)")
            continue
        if "app-server" not in argv or not any(Path(a).name.startswith("codex") for a in argv):
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            found.append(f"{entry.name}(environ unreadable)")
            continue
        env = dict(item.decode(errors="replace").partition("=")[::2] for item in raw.split(b"\0") if item)
        home = env.get("CODEX_HOME") or (str(Path(env["HOME"]) / ".codex") if env.get("HOME") else "")
        if not home:
            found.append(f"{entry.name}(CODEX_HOME unknown)")
        elif Path(home).resolve() == target:
            found.append(entry.name)
    return found


def sweep_legacy_owners(gone: Callable[[int], bool] = _owner_process_gone,
                        proc_root: str | Path = "/proc") -> list[str]:
    """[A82 step 4 rework, m2] Cutover sweep for Stage 8's migration (and the
    operator exit): clear owner rows left by the LEGACY ``_run`` path — owners
    with NO recorded app-server identity (no ``owner_processes`` row, no
    managed rows) — whose owning carrier process is provably gone (its pid no
    longer exists). Such an owner otherwise reads ``codex_thread_busy`` forever.
    Owners with a recorded identity are never touched here (``clear_dead_owners``
    decides them by app-server proof); a pid that exists — even a reused one —
    is never cleared (fail closed). Returns the cleared owner ids.

    Run it with: ``python -m src.backends.codex_ownership --sweep-legacy-owners``
    (uses ``CODEX_HOME`` like the carrier).

    [A82 pre-cutover, m2] Refused (``LegacySweepRefused``, nothing cleared)
    while any ``codex app-server`` for the same ``CODEX_HOME`` is alive — see
    the module docstring for the container pid-namespace caveat."""
    cleared: list[str] = []
    store = CodexOwnership()
    live = _live_app_servers(store.path.parent, Path(proc_root))
    if live:
        raise LegacySweepRefused("codex app-server still alive for this CODEX_HOME: pids " + ", ".join(live))
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT DISTINCT owner, pid FROM owners WHERE owner NOT IN (SELECT owner FROM owner_processes) "
            "AND owner NOT IN (SELECT owner FROM managed_turns)").fetchall()
        pids: dict[str, set[int]] = {}
        for owner, pid in rows:
            pids.setdefault(owner, set()).add(int(pid))
        for owner, owner_pids in pids.items():
            if all(gone(pid) for pid in owner_pids):
                conn.execute("DELETE FROM owners WHERE owner = ?", (owner,))
                cleared.append(owner)
    return cleared


def _record(row: tuple) -> ManagedTurnRecord:
    turn_uuid, session_key, thread_id, native_turn_id, kind, state, owner, process = row
    return ManagedTurnRecord(turn_uuid=turn_uuid, session_key=session_key, thread_id=thread_id,
                             native_turn_id=native_turn_id, kind=kind, state=state, owner=owner,
                             process=json.loads(process or "{}"))


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--sweep-legacy-owners"]:
        sys.exit("usage: python -m src.backends.codex_ownership --sweep-legacy-owners")
    try:
        print(json.dumps({"cleared_owners": sweep_legacy_owners()}))
    except LegacySweepRefused as refused:
        print(json.dumps({"refused": str(refused)}))
        sys.exit(2)
