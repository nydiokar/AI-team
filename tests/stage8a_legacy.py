"""[A82 Stage 8a] Test helpers that model PRE-CUTOVER state explicitly.

After the cutover every session is born managed and a protocol-0 session
EXECUTION row (create/resume/compact) is refused at insert and claim. Such rows
and unenrolled sessions still exist — rows written before the cutover (left
claimed/running by migration 43, or historical terminal rows the read models
render) and sessions an operator unenrolled — so tests that exercise the code
reading them build that state through these helpers instead of the now-fenced
production writers. Never used to bypass a fence in a test OF the fence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def unenrolled(db: Any, session_id: str) -> None:
    """Model an unenrolled session the only way one exists after the cutover:
    the operator exit (``unenroll_session_drained``); presence re-read."""
    db.unenroll_session_drained(session_id)
    db.refresh_enrollment_presence()


def enqueue_pre_cutover(
    db: Any,
    task_id: str,
    session_id: Optional[str],
    machine_id: Optional[str],
    backend: str,
    action: str,
    payload: Dict[str, Any],
    artifact_path: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    status: str = "pending",
) -> None:
    """``MeshDB.enqueue_task`` as it wrote a protocol-0 row before the Stage-8a
    insert fence (same columns, same values)."""
    now = _now()
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
    completed = now if status in ("completed", "failed", "failed_node_offline", "cancelled") else None
    with db._write() as conn:
        conn.execute(
            "INSERT INTO mesh_tasks (id, session_id, machine_id, backend, action, payload, prompt, "
            "status, artifact_path, parent_task_id, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, session_id, machine_id, backend, action, json.dumps(payload), prompt,
             status, artifact_path, parent_task_id, now, now, completed),
        )


def claim_pre_cutover(db: Any, task_id: str, node_id: str) -> bool:
    """``MeshDB.claim_task`` as it claimed a protocol-0 session row before the
    Stage-8a claim fence (same columns, same CAS)."""
    now = _now()
    with db._write() as conn:
        conn.execute(
            "UPDATE mesh_tasks SET status = 'claimed', claimed_by = ?, claimed_at = ?, updated_at = ?, "
            "claimer_incarnation = (SELECT incarnation_id FROM nodes WHERE node_id = ?) "
            "WHERE id = ? AND status = 'pending' AND COALESCE(queue_protocol, 0) = 0",
            (node_id, now, now, node_id, task_id),
        )
        return conn.execute("SELECT changes()").fetchone()[0] > 0
