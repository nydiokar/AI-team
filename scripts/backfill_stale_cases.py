"""One-time honest-state backfill for spurious pre-A36 per-turn ``flow_runs``.

The gateway ran pre-A36 code that minted a Case (``flow_run``) per turn. That left
~95 non-terminal flow_runs (status NULL or ``blocked``) whose owning session is
already closed (or which have no live owner). The Work surface (``/api/work``) shows
them as active/blocked, which is misleading. This script resolves each such row to
the honest terminal status ``cancelled`` — they were never genuine objectives that
completed — WITHOUT touching genuinely-live work.

Dynamically computed PROTECT set (never modified):
  * any flow_run whose OWNER session is not ``closed`` (owner resolved two ways:
    ``flow_runs.task_id -> mesh_tasks.session_id`` AND ``flow_links`` session link);
  * any flow_run created within the last 30 minutes (in-flight safety margin);
  * any flow_run with ``parent_flow_run_id`` / ``dispatched_by`` set, or
    ``objective_lock`` NOT NULL (a genuine dispatched/managed Case — left for a real
    ``close_case``).

CLEAN set = (status IS NULL OR status='blocked') minus PROTECT minus already-terminal.
For each ``blocked`` CLEAN candidate the row is first checked for a genuinely-pending
approval; if one exists the row is SKIPPED and reported, not cancelled.

Idempotent: rows already terminal (status IN ('closed','cancelled')) are skipped.
Never deletes; ``flow_events`` is append-only; the ``sessions`` table is never touched.

    python scripts/backfill_stale_cases.py --dry-run   # read-only: counts + sample
    python scripts/backfill_stale_cases.py --apply      # single-transaction writes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = "state/mesh.db"
TERMINAL = ("closed", "cancelled")  # matches db._CLOSED_STATUSES (idempotency skip)
RECENT_WINDOW_MIN = 30


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _cutoff() -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(minutes=RECENT_WINDOW_MIN)).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


# The CLEAN candidate query. PROTECT is expressed as the NOT-matched complement so
# the whole set is computed in one authoritative pass over the live DB.
_CLEAN_SQL = """
SELECT fr.flow_run_id, fr.status, fr.created_at, fr.current_stage, fr.task_id
FROM flow_runs fr
WHERE (fr.status IS NULL OR fr.status = 'blocked')         -- non-terminal only => idempotent
  AND fr.created_at < :cutoff                              -- PROTECT: recent
  AND fr.parent_flow_run_id IS NULL                        -- PROTECT: dispatched lineage
  AND fr.dispatched_by IS NULL
  AND fr.objective_lock IS NULL                            -- PROTECT: managed Case
  AND NOT EXISTS (                                         -- PROTECT: owner session live (via task)
        SELECT 1 FROM mesh_tasks mt JOIN sessions s ON s.session_id = mt.session_id
        WHERE mt.id = fr.task_id AND s.status NOT IN ('closed')
      )
  AND NOT EXISTS (                                         -- PROTECT: owner session live (via link)
        SELECT 1 FROM flow_links fl JOIN sessions s ON s.session_id = fl.entity_id
        WHERE fl.flow_run_id = fr.flow_run_id
          AND fl.entity_type = 'session' AND s.status NOT IN ('closed')
      )
ORDER BY fr.created_at ASC
"""


def _has_pending_approval(conn: sqlite3.Connection, flow_run_id: str) -> bool:
    """A blocked row is genuine work if an approval linked to it is still pending —
    via the approvals.flow_run_id FK OR a flow_links entity_type='approval' edge."""
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM approvals a
             WHERE a.flow_run_id = :fid AND a.status = 'pending')
          +
          (SELECT COUNT(*) FROM flow_links fl JOIN approvals a ON a.id = fl.entity_id
             WHERE fl.flow_run_id = :fid AND fl.entity_type = 'approval'
               AND a.status = 'pending') AS n
        """,
        {"fid": flow_run_id},
    ).fetchone()
    return int(row["n"]) > 0


def _protect_count(conn: sqlite3.Connection) -> int:
    """Non-terminal rows that are PROTECTED (the complement of CLEAN)."""
    total = conn.execute(
        "SELECT COUNT(*) n FROM flow_runs WHERE status IS NULL OR status='blocked'"
    ).fetchone()["n"]
    clean = len(conn.execute(_CLEAN_SQL, {"cutoff": _cutoff()}).fetchall())
    return int(total) - clean


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="read-only (default)")
    g.add_argument("--apply", action="store_true", help="perform the writes")
    args = ap.parse_args()
    apply = args.apply  # default (no flag) => dry-run

    conn = _connect()
    try:
        candidates = [dict(r) for r in conn.execute(_CLEAN_SQL, {"cutoff": _cutoff()}).fetchall()]

        # Split blocked candidates: genuinely-pending approval => SKIP (report).
        clean: list[dict] = []
        skipped: list[dict] = []
        for r in candidates:
            if r["status"] == "blocked" and _has_pending_approval(conn, r["flow_run_id"]):
                skipped.append(r)
            else:
                clean.append(r)

        non_terminal = conn.execute(
            "SELECT COUNT(*) n FROM flow_runs WHERE status IS NULL OR status='blocked'"
        ).fetchone()["n"]
        protected = _protect_count(conn)

        print(f"mode                : {'APPLY' if apply else 'DRY-RUN (read-only)'}")
        print(f"now (UTC)           : {_now()}")
        print(f"non-terminal rows   : {non_terminal}  (status NULL or 'blocked')")
        print(f"PROTECTED (kept)    : {protected}")
        print(f"CLEAN -> cancel     : {len(clean)}")
        print(f"blocked w/ pending  : {len(skipped)}  (SKIPPED, left as-is)")
        print()
        print("sample (up to 10 CLEAN rows):")
        print(f"  {'flow_run_id':32}  {'status':7}  {'created_at':32}  stage")
        for r in clean[:10]:
            print(
                f"  {r['flow_run_id']:32}  {str(r['status'] or 'NULL'):7}  "
                f"{r['created_at']:32}  {r['current_stage']}"
            )
        if skipped:
            print("\nSKIPPED blocked rows (genuine pending approval):")
            for r in skipped:
                print(f"  {r['flow_run_id']}  (created {r['created_at']})")

        if not apply:
            print("\n-- DRY RUN: no writes. Re-run with --apply to commit. --")
            return

        # ---- single atomic transaction ----
        now = _now()
        payload = json.dumps({
            "outcome": "cancelled",
            "reason": "backfill: spurious pre-A36 per-turn case; owning session closed or orphan",
        })
        conn.execute("BEGIN IMMEDIATE;")
        try:
            for r in clean:
                fid = r["flow_run_id"]
                conn.execute(
                    "UPDATE flow_runs SET status='cancelled', updated_at=? WHERE flow_run_id=?",
                    (now, fid),
                )
                conn.execute(
                    """
                    INSERT INTO flow_events (
                        flow_run_id, event_type, actor, from_state, to_state,
                        entity_type, entity_id, payload_json, created_at
                    ) VALUES (?, 'flow.status_changed', 'backfill', ?, 'cancelled',
                              NULL, NULL, ?, ?)
                    """,
                    (fid, r["status"], payload, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        print(f"\nAPPLIED: cancelled {len(clean)} flow_run(s) + appended {len(clean)} flow_events.")
        print("cancelled IDs:")
        for r in clean:
            print(f"  {r['flow_run_id']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
