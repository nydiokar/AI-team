"""Operational DB backup script (scripts/backup/backup_dbs.sh).

Runs the real script against throwaway SQLite files in tmp_path: snapshots are
consistent, verified, compressed, checksummed and published atomically; a
corrupt source fails loudly without publishing a bad copy; retention keeps the
newest N runs plus one per day for M days.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO: Path = Path(__file__).resolve().parent.parent
SCRIPT: Path = REPO / "scripts" / "backup" / "backup_dbs.sh"
DB_NAMES: tuple[str, ...] = ("mesh.db", "quota_windows.db")


def _make_db(path: Path, rows: int) -> None:
    conn: sqlite3.Connection = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _run(src: Path, dest: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env: dict[str, str] = {
        **os.environ,
        "AI_TEAM_DB_SOURCE_DIR": str(src),
        "AI_TEAM_BACKUP_DIR": str(dest),
        **extra,
    }
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    d: Path = tmp_path / "state"
    d.mkdir()
    for name in DB_NAMES:
        _make_db(d / name, rows=50)
    return d


def test_snapshot_round_trips_and_is_checksummed(src: Path, tmp_path: Path) -> None:
    dest: Path = tmp_path / "backups"
    res = _run(src, dest)
    assert res.returncode == 0, res.stderr

    latest: str = (dest / "LATEST").read_text().strip()
    snap: Path = dest / "snapshots" / latest
    assert sorted(p.name for p in snap.iterdir()) == ["SHA256SUMS", "mesh.db.zst", "quota_windows.db.zst"]

    sums: dict[str, str] = {
        line.split()[1]: line.split()[0] for line in (snap / "SHA256SUMS").read_text().splitlines()
    }
    for name in DB_NAMES:
        blob: Path = snap / f"{name}.zst"
        assert sums[blob.name] == hashlib.sha256(blob.read_bytes()).hexdigest()
        restored: Path = tmp_path / f"restored-{name}"
        subprocess.run(["zstd", "-q", "-d", str(blob), "-o", str(restored)], check=True)
        conn: sqlite3.Connection = sqlite3.connect(restored)
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (50,)
        conn.close()
    assert not any((dest / ".tmp").iterdir())


def test_corrupt_source_fails_and_publishes_nothing_for_it(src: Path, tmp_path: Path) -> None:
    (src / "quota_windows.db").write_bytes(b"not a sqlite database" * 100)
    for side in ("-wal", "-shm"):
        (src / f"quota_windows.db{side}").unlink(missing_ok=True)
    dest: Path = tmp_path / "backups"
    res = _run(src, dest)
    assert res.returncode != 0
    snaps: list[Path] = list((dest / "snapshots").iterdir())
    assert len(snaps) == 1
    assert not (snaps[0] / "quota_windows.db.zst").exists()
    assert (snaps[0] / "mesh.db.zst").exists()


def test_missing_source_db_fails(src: Path, tmp_path: Path) -> None:
    (src / "mesh.db").unlink()
    res = _run(src, tmp_path / "backups")
    assert res.returncode != 0


def test_retention_keeps_recent_and_one_per_day(src: Path, tmp_path: Path) -> None:
    dest: Path = tmp_path / "backups"
    snaps: Path = dest / "snapshots"
    # 3 runs/day over 5 old days, oldest first.
    old: list[str] = [f"202001{d:02d}T{h:02d}00Z" for d in range(1, 6) for h in (1, 9, 17)]
    for name in old:
        (snaps / name).mkdir(parents=True)
    res = _run(src, dest, AI_TEAM_BACKUP_KEEP_RECENT="2", AI_TEAM_BACKUP_KEEP_DAILY="3")
    assert res.returncode == 0, res.stderr

    kept: list[str] = sorted(p.name for p in snaps.iterdir())
    new: str = (dest / "LATEST").read_text().strip()
    # Recent 2 = new run + 20200105T1700Z; daily 3 = the newest run of each of
    # the 3 newest days (today, 0105, 0104); everything else is pruned.
    assert kept == sorted([new, "20200105T1700Z", "20200104T1700Z"])
