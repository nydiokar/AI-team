#!/usr/bin/env python3
"""[A88] Read-only reconciliation report: worker-local mesh.db vs controller mesh.db.

Run before retiring a worker's local ``state/mesh.db`` (docs/DATABASE_AUTHORITY.md §5-6).
Never writes either file: both are opened ``mode=ro`` and compared in SQL (no row
materialisation), so it is safe against a live controller and idempotent.

Exit codes (2 is argparse usage; 1 is an unexpected crash):
  0  every shared table compared; nothing in the worker file that the controller lacks
  3  runtime-flag conflict or worker-only flag row — operator must choose (R1)
  4  some tables could not be compared (no/mismatched primary key, or absent from the
     controller) — listed under "skipped"; the result is NOT a clean bill
  5  worker-only / worker-newer rows exist — review them (they are not migrated)
  6  a database could not be read (locked/corrupt/not SQLite)
Precedence when several apply: 3 > 4 > 5.

    python scripts/db_authority_report.py \
        --controller-db ~/ai-team-data/controller/state/mesh.db \
        --worker-db ~/dev/AI-team/state/mesh.db [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

from pydantic import BaseModel

_SAMPLE = 5
_MAX_PLAIN_KEY = 40  # URLs (push endpoints are capability URLs) and long keys are hashed


def _safe_key(key: str) -> str:
    if len(key) <= _MAX_PLAIN_KEY and "://" not in key:
        return key
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:12]


class FlagConflict(BaseModel):
    flag_name: str
    controller_value: str | None
    worker_value: str


class TableDivergence(BaseModel):
    table: str
    worker_only: int
    worker_newer: int
    sample_keys: list[str]


class SkippedTable(BaseModel):
    table: str
    reason: str


class AuthorityReport(BaseModel):
    flag_conflicts: list[FlagConflict]
    tables: list[TableDivergence]
    skipped: list[SkippedTable]
    exit_code: int


EXIT_CLEAN, EXIT_FLAG_CONFLICT, EXIT_SKIPPED, EXIT_ROWS, EXIT_UNREADABLE = 0, 3, 4, 5, 6


def _ro_uri(path: Path) -> str:
    return f"file:{path.resolve()}?mode=ro"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pk_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({_quote(table)})").fetchall()
    return [str(r[1]) for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA {schema}.table_info({_quote(table)})")]


def _flag_conflicts(conn: sqlite3.Connection) -> list[FlagConflict]:
    rows = conn.execute(
        """
        SELECT w.flag_name, c.value, w.value
        FROM w.runtime_flags AS w
        LEFT JOIN main.runtime_flags AS c ON c.flag_name = w.flag_name
        WHERE c.flag_name IS NULL OR c.value IS NOT w.value
        ORDER BY w.flag_name
        """
    ).fetchall()
    return [FlagConflict(flag_name=r[0], controller_value=r[1], worker_value=str(r[2])) for r in rows]


def _skip_reason(conn: sqlite3.Connection, table: str) -> str | None:
    pk = _pk_columns(conn, "w", table)
    if not pk:
        return "no primary key"
    if pk != _pk_columns(conn, "main", table):
        return "primary key differs between files"
    return None


def _table_divergence(conn: sqlite3.Connection, table: str) -> TableDivergence | None:
    pk = _pk_columns(conn, "w", table)
    t = _quote(table)
    join = " AND ".join(f"c.{_quote(k)} = w.{_quote(k)}" for k in pk)
    key_expr = " || '|' || ".join(f"CAST(w.{_quote(k)} AS TEXT)" for k in pk)
    only_sql = f"FROM w.{t} AS w WHERE NOT EXISTS (SELECT 1 FROM main.{t} AS c WHERE {join})"
    worker_only = int(conn.execute(f"SELECT COUNT(*) {only_sql}").fetchone()[0])
    samples = [_safe_key(str(r[0])) for r in conn.execute(f"SELECT {key_expr} {only_sql} LIMIT {_SAMPLE}")]
    worker_newer = 0
    shared_cols = set(_columns(conn, "w", table)) & set(_columns(conn, "main", table))
    if "updated_at" in shared_cols:
        newer_sql = f"FROM w.{t} AS w JOIN main.{t} AS c ON {join} WHERE w.updated_at > c.updated_at"
        worker_newer = int(conn.execute(f"SELECT COUNT(*) {newer_sql}").fetchone()[0])
        samples += [_safe_key(str(r[0])) for r in conn.execute(f"SELECT {key_expr} {newer_sql} LIMIT {_SAMPLE}")]
    if not worker_only and not worker_newer:
        return None
    return TableDivergence(table=table, worker_only=worker_only, worker_newer=worker_newer, sample_keys=samples)


def build_report(controller_db: Path, worker_db: Path) -> AuthorityReport:
    conn = sqlite3.connect(_ro_uri(controller_db), uri=True, timeout=5)
    try:
        conn.execute("ATTACH DATABASE ? AS w", (_ro_uri(worker_db),))
        controller_tables = {
            str(r[0]) for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")
        }
        worker_tables: list[str] = [
            str(r[0]) for r in conn.execute(
                "SELECT name FROM w.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        skipped: list[SkippedTable] = []
        compared: list[str] = []
        for table in worker_tables:
            reason = "absent from controller" if table not in controller_tables else _skip_reason(conn, table)
            if reason:
                skipped.append(SkippedTable(table=table, reason=reason))
            elif table != "runtime_flags":
                compared.append(table)
        conflicts = _flag_conflicts(conn) if "runtime_flags" in controller_tables and "runtime_flags" in worker_tables else []
        tables = [d for d in (_table_divergence(conn, t) for t in compared) if d]
    finally:
        conn.close()
    if conflicts:
        code = EXIT_FLAG_CONFLICT
    elif skipped:
        code = EXIT_SKIPPED
    elif tables:
        code = EXIT_ROWS
    else:
        code = EXIT_CLEAN
    return AuthorityReport(flag_conflicts=conflicts, tables=tables, skipped=skipped, exit_code=code)


def _render(report: AuthorityReport) -> str:
    lines: list[str] = []
    if report.flag_conflicts:
        lines.append("RUNTIME FLAG CONFLICTS (operator decides; nothing is copied):")
        lines += [f"  {c.flag_name}: controller={c.controller_value!r} worker={c.worker_value!r}" for c in report.flag_conflicts]
    else:
        lines.append("runtime_flags: worker rows all match the controller")
    for t in report.tables:
        lines.append(
            f"{t.table}: worker_only={t.worker_only} worker_newer={t.worker_newer} sample={t.sample_keys}"
        )
    if not report.tables:
        lines.append("no worker-only or worker-newer rows in any compared table")
    lines += [f"SKIPPED {s.table}: {s.reason}" for s in report.skipped]
    lines.append(f"exit={report.exit_code}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--controller-db", type=Path, required=True)
    parser.add_argument("--worker-db", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    for p in (args.controller_db, args.worker_db):
        if not p.is_file():
            parser.error(f"not a file: {p}")
    try:
        report = build_report(args.controller_db, args.worker_db)
    except sqlite3.Error as exc:
        print(f"unreadable database: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    print(report.model_dump_json(indent=2) if args.json else _render(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
