"""Cleanup: keep only sessions that have real task history in mesh.db.

A session is "real" iff it has >=1 row in mesh_tasks. Everything else
(test fixtures + abandoned empty shells) is deleted from the DB. Live session
JSON files on disk are NOT touched.

Usage:
  python scripts/analyze_sessions.py          # dry run
  python scripts/analyze_sessions.py --apply  # delete
"""
import sqlite3
import sys

APPLY = "--apply" in sys.argv
db = sqlite3.connect("state/mesh.db")
db.row_factory = sqlite3.Row

all_sids = set(r["session_id"] for r in db.execute("SELECT session_id FROM sessions"))
task_sids = set(r[0] for r in db.execute("SELECT DISTINCT session_id FROM mesh_tasks"))

keep = all_sids & task_sids
drop = all_sids - task_sids

print(f"total sessions:      {len(all_sids)}")
print(f"keep (have tasks):   {len(keep)}")
print(f"drop (zero tasks):   {len(drop)}")

# Orphaned task_events pointing at to-be-dropped sessions
ev_orphans = db.execute(
    f"SELECT COUNT(*) FROM task_events WHERE session_id IN ({','.join('?' * len(drop))})",
    list(drop),
).fetchone()[0] if drop else 0
print(f"task_events to drop: {ev_orphans}")

if not APPLY:
    print("\nDRY RUN — pass --apply to delete.")
    db.close()
    raise SystemExit(0)

with db:  # transaction
    db.executemany("DELETE FROM sessions WHERE session_id = ?", [(s,) for s in drop])
    db.executemany("DELETE FROM task_events WHERE session_id = ?", [(s,) for s in drop])

remaining = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
print(f"\nAPPLIED. sessions remaining: {remaining}")
db.execute("VACUUM")
db.close()
print("VACUUM done.")
