"""A104 test helpers: seed a REQUESTED Case child and drive it terminal through the
real terminal seam, so its completion lands in the requester's agent inbox."""
from typing import Optional

import src.control.db as db_mod
from src.control.db import MeshDB


def seed_child(
    db: MeshDB, case_id: Optional[str], task_id: str, *, requester: Optional[str],
    status: str = "running", token: str = "tok-seed", link: bool = True,
) -> None:
    """Insert one protocol-1 managed child turn (session_id NULL — no sessions FK),
    requested by ``requester`` (its ``sender_session_id``), scoped to ``case_id``."""
    now = db_mod._now()
    with db._write() as conn:
        conn.execute(
            "INSERT INTO mesh_tasks (id, session_id, backend, action, payload, status, queue_protocol, "
            "claim_token, flow_run_id, sender_session_id, created_at, updated_at) "
            "VALUES (?, NULL, 'claude', 'resume_session', '{}', ?, 1, ?, ?, ?, ?, ?)",
            (task_id, status, token, case_id, requester, now, now),
        )
    if case_id and link:
        db.create_flow_link(case_id, "task", task_id, "task", created_by="manager")


def finish_child(db: MeshDB, task_id: str, *, status: str = "completed", token: str = "tok-seed") -> None:
    assert db.complete_turn(task_id, token, {"success": status == "completed", "output": "ok"}, status=status)


def seed_finished_child(
    db: MeshDB, case_id: Optional[str], task_id: str, *, requester: Optional[str],
    status: str = "completed",
) -> None:
    seed_child(db, case_id, task_id, requester=requester)
    finish_child(db, task_id, status=status)
