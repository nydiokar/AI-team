"""Read-side view models (Cockpit Move C / Milestone M2).

One read shape for "what the operator sees about a session", consumed by
Telegram lists today and a Web UI dashboard later. Derived from ``Session``,
never persisted. The DTO carries the raw ``backend`` string only — rendering
(icons/labels) is each surface's concern, NOT this DTO's.

See docs/COCKPIT_REFACTOR_SPEC.md §4 (Move C) and docs/CONTROL_CONTRACT.md §6.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from src.core.interfaces import Session, SessionStatus


@dataclass(frozen=True)
class SessionView:
    """Operator-facing read model for a session. Derived, never persisted.

    Maps 1:1 to existing Session fields plus the few *derived booleans* every
    surface re-computes today (``needs_input``, ``is_active``). Both Telegram
    lists and a future Web UI consume this instead of re-deriving status logic
    from Session ad hoc.
    """
    session_id: str
    backend: str                # raw name; surface decides how to display it
    repo_path: str
    status: str                 # SessionStatus value
    machine_id: str
    backend_session_id: str     # native session ID the backend returned (resume key)
    model: Optional[str]
    effort: Optional[str]
    default_model: Optional[str]  # the backend's default model (shown when model is None)
    last_task_id: str
    last_summary: str
    last_files_modified: List[str]
    needs_input: bool           # status == AWAITING_INPUT
    is_active: bool             # status not in {CLOSED, ERROR, PINNED_NODE_OFFLINE}
    origin_channel: str         # where the session came from (SessionOrigin.channel)
    origin_kind: str            # SessionOrigin.kind
    updated_at: str

    @classmethod
    def from_session(cls, s: Session) -> "SessionView":
        origin = s.origin
        try:
            # The default the DRIVER will actually run (config default → catalog
            # default), not a static catalog guess — so the UI's "(default)"
            # label matches what really executes. Mirrors resolve_model().
            from config.models import resolved_default_model as _resolved_default_model
            resolved_default = _resolved_default_model(s.backend)
        except Exception:
            resolved_default = None
        return cls(
            session_id=s.session_id,
            backend=s.backend,
            repo_path=s.repo_path,
            status=s.status.value,
            machine_id=s.machine_id,
            backend_session_id=s.backend_session_id or "",
            model=s.model,
            effort=getattr(s, "effort", None),
            default_model=resolved_default,
            last_task_id=s.last_task_id,
            last_summary=s.last_result_summary or s.last_summary,
            last_files_modified=list(s.last_files_modified or []),
            needs_input=(s.status == SessionStatus.AWAITING_INPUT),
            # A18: PINNED_NODE_OFFLINE is a needs-attention terminal, parallel to
            # ERROR (a turn that did not complete) — inactive but still resumable
            # via the normal path. PAUSED_PINNED_NODE_OFFLINE is an in-flight hold,
            # so it stays active like BUSY.
            is_active=s.status not in (
                SessionStatus.CLOSED,
                SessionStatus.ERROR,
                SessionStatus.PINNED_NODE_OFFLINE,
            ),
            origin_channel=origin.channel if origin else "telegram",
            origin_kind=origin.kind if origin else "user",
            updated_at=s.updated_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)   # JSON-ready for a future Web UI / WebSocket
