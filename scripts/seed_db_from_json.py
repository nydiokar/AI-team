"""
Seed the mesh DB from existing JSON state files.

Run once after the DB is first created to backfill historical session and
result data so it's queryable from day one.

Usage:
    python scripts/seed_db_from_json.py
    python scripts/seed_db_from_json.py --dry-run

What it does:
    1. Reads every state/sessions/*.json → upserts into sessions table
    2. Reads every results/task_*.json → inserts into mesh_tasks as 'completed'
       (skips tasks already present in the DB)
    3. Reads every logs/session_events/*.log (NDJSON) → inserts into task_events
       (skips events that would duplicate existing rows by session+task+timestamp)

Safe to re-run — all inserts are idempotent.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on the path so config and src imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main(dry_run: bool = False) -> None:
    from src.control.db import MeshDB
    from config import config as cfg

    db_path = Path(cfg.mesh.db_path)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    if dry_run:
        logger.info("DRY RUN — no writes will occur")
    else:
        logger.info("Seeding DB at %s", db_path)

    db = MeshDB(str(db_path)) if not dry_run else None

    # ----------------------------------------------------------------
    # 1. Sessions
    # ----------------------------------------------------------------
    sessions_dir = PROJECT_ROOT / "state" / "sessions"
    session_files = sorted(sessions_dir.glob("*.json"))
    logger.info("Found %d session files", len(session_files))

    seeded_sessions = 0
    for path in session_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if dry_run:
                logger.info("  [dry] session %s status=%s", data.get("session_id"), data.get("status"))
                seeded_sessions += 1
                continue
            # Build a minimal object duck-typed for upsert_session
            db.upsert_session(_DictSession(data))
            seeded_sessions += 1
        except Exception as e:
            logger.warning("  SKIP %s: %s", path.name, e)

    logger.info("Sessions seeded: %d", seeded_sessions)

    # ----------------------------------------------------------------
    # 2. Task results → mesh_tasks as completed
    # ----------------------------------------------------------------
    results_dir = PROJECT_ROOT / "results"
    result_files = sorted(results_dir.glob("task_*.json"))
    logger.info("Found %d result files", len(result_files))

    seeded_tasks = 0
    skipped_tasks = 0
    for path in result_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            task_id = data.get("task_id") or path.stem
            if not task_id:
                continue

            if dry_run:
                logger.info("  [dry] task %s success=%s", task_id, data.get("success"))
                seeded_tasks += 1
                continue

            # Skip if already in DB
            if db.get_task(task_id) is not None:
                skipped_tasks += 1
                continue

            session_info = data.get("session") or {}
            session_id = session_info.get("session_id") or None
            machine_id = session_info.get("machine_id") or None
            backend = session_info.get("backend") or data.get("runtime", {}).get("backend") or "claude"
            created_at = data.get("timestamp") or data.get("created_at") or ""
            artifact_path = str(Path("results") / f"{task_id}.json")

            payload = {
                "prompt": data.get("task", {}).get("title") or "",
                "task_id": task_id,
                "action": "resume_session" if session_id else "run_oneoff",
                "metadata": data.get("task") or {},
            }

            result_dict = {
                "success": data.get("success", False),
                "output": "",
                "errors": data.get("errors") or [],
                "files_modified": data.get("files_modified") or [],
                "execution_time": data.get("execution_time") or 0.0,
                "timestamp": created_at,
                "return_code": data.get("return_code") or 0,
            }

            db.enqueue_task(
                task_id=task_id,
                session_id=session_id,
                machine_id=machine_id,
                backend=backend,
                action=payload["action"],
                payload=payload,
                artifact_path=artifact_path,
            )
            # Immediately mark as completed — these are historical records
            db.complete_task(task_id, result_dict, artifact_path)
            seeded_tasks += 1
        except Exception as e:
            logger.warning("  SKIP %s: %s", path.name, e)

    logger.info("Tasks seeded: %d  skipped (already in DB): %d", seeded_tasks, skipped_tasks)

    # ----------------------------------------------------------------
    # 3. Session event logs → task_events
    # ----------------------------------------------------------------
    events_dir = PROJECT_ROOT / "logs" / "session_events"
    if not events_dir.exists():
        logger.info("No session_events directory found — skipping events")
    else:
        event_files = sorted(events_dir.glob("*.log"))
        logger.info("Found %d event log files", len(event_files))

        seeded_events = 0
        for path in event_files:
            session_id = path.stem
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if dry_run:
                        seeded_events += 1
                        continue
                    db.append_event(
                        session_id=session_id,
                        task_id=ev.get("task_id") or "",
                        success=bool(ev.get("success", False)),
                        execution_time=ev.get("execution_time"),
                        error=ev.get("error") or "",
                    )
                    seeded_events += 1
            except Exception as e:
                logger.warning("  SKIP %s: %s", path.name, e)

        logger.info("Events seeded: %d", seeded_events)

    if not dry_run:
        stats = db.stats()
        logger.info("DB stats after seed: %s", stats)
        db.close()

    logger.info("Done.")


class _DictSession:
    """Duck-typed wrapper so a raw dict can be passed to upsert_session."""

    def __init__(self, d: dict) -> None:
        self.session_id          = d["session_id"]
        self.backend             = d["backend"]
        self.repo_path           = d["repo_path"]
        self.status              = _StatusValue(d["status"])
        self.created_at          = d["created_at"]
        self.updated_at          = d["updated_at"]
        self.machine_id          = d.get("machine_id", "")
        self.backend_session_id  = d.get("backend_session_id", "")
        self.last_task_id        = d.get("last_task_id", "")
        self.last_artifact_path  = d.get("last_artifact_path", "")
        self.last_summary        = d.get("last_summary", "")
        self.last_user_message   = d.get("last_user_message", "")
        self.last_result_summary = d.get("last_result_summary", "")
        self.last_files_modified = d.get("last_files_modified", [])
        self.telegram_chat_id    = d.get("telegram_chat_id")
        self.telegram_thread_id  = d.get("telegram_thread_id")
        self.owner_user_id       = d.get("owner_user_id")
        self.task_history        = d.get("task_history", [])


class _StatusValue:
    """Wraps a string status so upsert_session's .value access works."""
    def __init__(self, v: str) -> None:
        self.value = v


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed mesh DB from JSON state files")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
