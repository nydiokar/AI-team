"""Read-only session projection and the existing persistence parsing rules.

The adapter deliberately retains the legacy Session dataclass parser's tolerance:
model_construct prevents Pydantic coercion/rejection from changing visible rows.
Status and cache-count parsing still reject exactly where the full parser does.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from src.core.interfaces import SessionOrigin, SessionStatus


class SessionListProjection(BaseModel):
    """List/view evidence only; never a lifecycle object or a persistence input."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    backend: str
    repo_path: str
    status: SessionStatus
    created_at: str
    updated_at: str
    machine_id: str = ""
    backend_session_id: str = ""
    model: str | None = None
    effort: str | None = None
    last_task_id: str = ""
    last_summary: str = ""
    last_result_summary: str = ""
    last_files_modified: list[str] = Field(default_factory=list)
    origin: SessionOrigin | None = None
    previous_backend_session_ids: list[str] = Field(default_factory=list)
    current_case_id: str | None = None
    case_role: str | None = None
    continued_from: str | None = None
    keep_pinned: bool = False
    keep_note: str = ""

    @classmethod
    def from_row(cls, row: dict[str, object]) -> SessionListProjection:
        return cls.model_construct(**session_values(row))


def session_values(d: dict[str, object]) -> dict[str, object]:
    def _parse_list(value: object) -> object:
        """Parse a field that may be a JSON string (DB) or already a list (JSON file)."""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:  # noqa: BLE001 — retain legacy malformed-field fallback
                return []
        return value or []

    def _parse_origin(value: object) -> SessionOrigin:
        """Read origin from a nested dict (JSON file) or JSON string (DB row);
        absent/unparseable → default SessionOrigin (telegram/user)."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:  # noqa: BLE001 — retain legacy malformed-field fallback
                value = None
        if isinstance(value, dict):
            return SessionOrigin(
                channel=value.get("channel", "telegram"),
                kind=value.get("kind", "user"),
            )
        return SessionOrigin()

    return {
        "session_id": d["session_id"],
        "backend": d["backend"],
        "repo_path": d["repo_path"],
        "status": SessionStatus(d["status"]),
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "machine_id": d.get("machine_id", ""),
        "backend_session_id": d.get("backend_session_id", ""),
        "model": d.get("model") or None,
        "effort": d.get("effort") or None,
        "last_task_id": d.get("last_task_id", ""),
        "last_artifact_path": d.get("last_artifact_path", ""),
        "last_summary": d.get("last_summary", ""),
        "last_user_message": d.get("last_user_message", ""),
        "last_result_summary": d.get("last_result_summary", ""),
        "last_files_modified": _parse_list(d.get("last_files_modified", [])),
        "telegram_chat_id": d.get("telegram_chat_id"),
        "telegram_thread_id": d.get("telegram_thread_id"),
        "owner_user_id": d.get("owner_user_id"),
        "task_history": _parse_list(d.get("task_history", [])),
        "origin": _parse_origin(d.get("origin")),
        "driver_type": d.get("driver_type", "") or "",
        "driver_status": d.get("driver_status", "") or "",
        "cache_health": d.get("cache_health", "unknown") or "unknown",
        "cache_unhealthy_count": int(d.get("cache_unhealthy_count", 0) or 0),
        "previous_backend_session_ids": _parse_list(d.get("previous_backend_session_ids", [])),
        "current_case_id": d.get("current_case_id") or None,
        "case_role": d.get("case_role") or None,
        "role_boot": d.get("role_boot") or None,
        "continued_from": d.get("continued_from") or None,
        "keep_pinned": bool(d.get("keep_pinned", False)),
        "keep_note": d.get("keep_note") or "",
    }
