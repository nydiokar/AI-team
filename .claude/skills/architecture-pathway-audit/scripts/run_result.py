"""Execution provenance shared by the two architecture audit entry points."""
from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field


class RunError(BaseModel):
    stage: str
    detail: str


class AuditRun(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    python: str = Field(default_factory=platform.python_version)
    status: Literal['pending', 'completed', 'partial', 'incomplete', 'failed'] = 'pending'
    stage: str = 'input'
    requested_scope: list[str] = Field(default_factory=list)
    effective_scope: list[str] = Field(default_factory=list)
    source_sha256: str | None = None
    errors: list[RunError] = Field(default_factory=list)

    @computed_field
    @property
    def required_analysis_complete(self) -> bool:
        return self.status == 'completed'

    def fail(self, stage: str, detail: str) -> None:
        self.status = 'failed'
        self.stage = stage
        self.errors.append(RunError(stage=stage, detail=detail))


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as temp:
        tmp = Path(temp.name)
        try:
            temp.write(content)
            temp.flush()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=1) + '\n')
