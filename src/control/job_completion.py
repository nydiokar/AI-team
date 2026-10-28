"""Bounded watched-job snapshots; delivery metadata is stored in controller SQLite."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, JsonValue

JOB_COMPLETION_BATCH = 25
JOB_EFFECT_ATTEMPTS = 5
JOB_EFFECT_LEASE_SEC = 120
JOB_COMPLETION_PASS_BUDGET_SEC = 20
JOB_NOTICE_TIMEOUT_SEC = 20
JOB_ACTION_TIMEOUT_SEC = 30


class JobCompletion(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    node_id: str = Field(default="", max_length=128)
    label: str = Field(default="", max_length=512)
    status: Literal["done", "failed", "lost"]
    exit_code: int | None = None
    tail: str = Field(default="", max_length=1500)
    notify: bool = True
    notify_agent: bool = False


class JobCompletionPage(BaseModel):
    jobs: list[JobCompletion] = Field(max_length=JOB_COMPLETION_BATCH)
    next_cursor: str | None = Field(default=None, max_length=128)


def compact_job(job: dict[str, JsonValue]) -> JobCompletion:
    """Local rows may contain large historical text; snapshot only display evidence."""
    owner = job.get("session_id")
    # Never truncate an identity into somebody else's routable session. Legacy
    # register inputs were unbounded; an invalid owner becomes an explicit
    # unroutable/dead receipt, not a poisoned page or a fabricated recipient.
    owner = owner if isinstance(owner, str) and len(owner) <= 128 else None
    return JobCompletion.model_validate({
        **{key: job.get(key) for key in ("id", "status", "exit_code")},
        "session_id": owner,
        "node_id": str(job.get("node_id") or "")[:128],
        "label": str(job.get("label") or job.get("id") or "")[:512],
        "tail": str(job.get("tail") or "")[-1500:],
        "notify": bool(job.get("notify")),
        "notify_agent": bool(job.get("notify_agent")),
    })
