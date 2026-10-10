"""Append-only metrics ledger for Scientific Governor reviews.

A review/verification pass produces one :class:`GovernorReviewMetric`; this module
appends it as a JSON line to ``governor_metrics.jsonl`` (JSONL so appends are atomic
and the ledger is never rewritten). Schema mirrors ``governor_metrics_schema.json``.

Wired minimally and deliberately: this is a self-contained recorder with no gateway
dependency, so it is unit-testable offline and can be called from either a harness
seam or a manual review. Pydantic (per project rules); no ``@staticmethod``; typed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION: int = 1

_DEFAULT_LEDGER: Path = Path(__file__).resolve().parent / "governor_metrics.jsonl"

PassKind = Literal["review", "verification"]
GovernorDecision = Literal[
    "accept", "accept-with-qualifications", "request-material-repair", "reject"
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernorReviewMetric(BaseModel):
    """One governor review or verification pass. Fields match the JSON schema."""

    schema_version: int = SCHEMA_VERSION
    recorded_at: str = Field(default_factory=_utc_now_iso)
    case_id: str
    milestone_id: Optional[str] = None
    pass_kind: PassKind
    decision: GovernorDecision
    reviews_performed: int = 1
    blocker_findings: int = 0
    material_findings: int = 0
    residual_findings: int = 0
    opportunity_findings: int = 0
    findings_accepted: int = 0
    findings_rebutted: int = 0
    findings_deferred: int = 0
    rework_completed_preclosure: int = 0
    review_latency_seconds: Optional[float] = None
    model_consumption_usd: Optional[float] = None
    escaped_defects_found_postclosure: int = 0


def append_metric(
    metric: GovernorReviewMetric, ledger_path: Optional[Path] = None
) -> Path:
    """Append one metric as a JSON line. Creates the ledger (and parents) if absent.

    Returns the ledger path written to. Append-only: never reads or rewrites prior
    records.
    """
    path: Path = ledger_path or _DEFAULT_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(metric.model_dump_json() + "\n")
    return path


def read_metrics(ledger_path: Optional[Path] = None) -> List[GovernorReviewMetric]:
    """Read all metrics from the ledger (empty list if it does not exist)."""
    path: Path = ledger_path or _DEFAULT_LEDGER
    if not path.is_file():
        return []
    out: List[GovernorReviewMetric] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(GovernorReviewMetric.model_validate_json(line))
    return out
