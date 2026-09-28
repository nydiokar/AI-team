"""Validated, transient activity hints shared by live backend producers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ActivityCategory = Literal[
    "thinking",
    "writing",
    "tool_started",
    "tool_completed",
    "waiting_permission",
    "backend_busy",
    "finished",
]
ActivityTool = Literal[
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Task",
    "WebSearch",
    "WebFetch",
    "NotebookEdit",
    "MCP tool",
    "OpenCode",
]


class BackendActivity(BaseModel):
    """One safe activity value correlated to the active gateway task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    category: ActivityCategory
    tool: ActivityTool | None = None

    @field_validator("session_id", "task_id")
    @classmethod
    def _ids_are_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("activity identifiers must not be blank")
        return value

    @model_validator(mode="after")
    def _tool_matches_category(self) -> BackendActivity:
        if (self.category in ("tool_started", "tool_completed")) != (self.tool is not None):
            raise ValueError("tool is required only for tool activity")
        return self

    @property
    def label(self) -> str:
        if self.category == "thinking":
            return "Thinking…"
        if self.category == "writing":
            return "Writing response…"
        if self.category == "tool_started":
            return f"Using {self.tool}"
        if self.category == "tool_completed":
            return f"Finished {self.tool}"
        if self.category == "waiting_permission":
            return "Waiting for permission"
        if self.category == "backend_busy":
            return "Using OpenCode"
        return "OpenCode finished"


def publish_activity(
    *,
    session_id: object,
    task_id: object,
    category: object,
    tool: object = None,
) -> bool:
    """Validate and emit one transient activity hint; malformed input is ignored."""
    try:
        activity = BackendActivity(
            session_id=session_id,
            task_id=task_id,
            category=category,
            tool=tool,
        )
        from src.core.observability import emit_event

        emit_event(
            "task_activity",
            session_id=activity.session_id,
            task_id=activity.task_id,
            turn_id=activity.task_id,
            label=activity.label,
        )
        return True
    except (ValidationError, TypeError, ValueError):
        return False
    except Exception:  # noqa: BLE001 — activity delivery must not affect execution
        # Activity must never interfere with a backend turn.
        return False
