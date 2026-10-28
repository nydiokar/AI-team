"""Controller jobs-list composition; ownership/filtering belongs to MeshDB."""
from __future__ import annotations

from pydantic import JsonValue

from src.control.db import MeshDB


class JobsUnavailableError(RuntimeError):
    """The selected controller jobs authority is unavailable."""


def list_controller_jobs(
    db: MeshDB | None,
    *,
    limit: int = 20,
    session_id: str | None = None,
    ownership: str | None = None,
) -> dict[str, list[dict[str, JsonValue]]]:
    """Two data SELECTs, including healthy-empty; recent includes all statuses.

    No SessionStore fallback: a JSON-only owner is orphaned in controller mode.
    SQLite errors propagate to the HTTP adapter; neither failure nor empty data
    selects a different authority.
    """
    if db is None:
        raise JobsUnavailableError("jobs_unavailable")
    return {
        "running": db.list_jobs(status="running", session_id=session_id,
                                ownership=ownership, limit=limit),
        "recent": db.list_jobs(session_id=session_id, ownership=ownership, limit=limit),
    }
