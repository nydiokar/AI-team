"""Write-transaction acquisition resilience + WAL maintenance (PR: db-write-resilience).

The worker daemon shares the mesh SQLite file cross-process, so BEGIN IMMEDIATE can
raise "database is locked" once busy_timeout is exhausted. A dropped control-plane
write is lost/dishonest state, so acquisition is retried with bounded backoff.
"""
import sqlite3

import pytest

from src.control.db import (
    MeshDB,
    _WRITE_BEGIN_MAX_ATTEMPTS,
)


class _LockyConn:
    """Fake connection that raises 'database is locked' on the first N BEGINs."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.begins = 0

    def execute(self, sql: str):
        if sql.strip().upper().startswith("BEGIN"):
            self.begins += 1
            if self.begins <= self.fail_times:
                raise sqlite3.OperationalError("database is locked")
        return None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("src.control.db.time.sleep", lambda *_a, **_k: None)


def test_begin_immediate_retries_then_succeeds(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    conn = _LockyConn(fail_times=_WRITE_BEGIN_MAX_ATTEMPTS - 1)
    db._begin_immediate(conn)  # must not raise
    assert conn.begins == _WRITE_BEGIN_MAX_ATTEMPTS


def test_begin_immediate_raises_after_exhaustion(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    conn = _LockyConn(fail_times=999)
    with pytest.raises(sqlite3.OperationalError):
        db._begin_immediate(conn)
    assert conn.begins == _WRITE_BEGIN_MAX_ATTEMPTS


def test_begin_immediate_does_not_retry_non_lock_errors(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))

    class _SyntaxConn:
        def __init__(self):
            self.begins = 0

        def execute(self, sql: str):
            if sql.strip().upper().startswith("BEGIN"):
                self.begins += 1
                raise sqlite3.OperationalError("near 'x': syntax error")
            return None

    conn = _SyntaxConn()
    with pytest.raises(sqlite3.OperationalError):
        db._begin_immediate(conn)
    assert conn.begins == 1  # non-lock error surfaces immediately


def test_checkpoint_wal_is_safe_on_live_db(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    # A real write so the WAL has frames, then a checkpoint must not raise.
    with db._write() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    result = db.checkpoint_wal()
    assert result is None or isinstance(result, tuple)
