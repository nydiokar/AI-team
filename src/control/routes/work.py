"""Read-only Case projections — flow_runs (`/api/flows`) and the Work surface (`/api/work`).

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md "2b.
Manager / Case surface". Module-level helpers are looked up as ``core.<name>`` so tests that
monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
import threading
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from src.control import control_api as core
from src.control.routes._responses import TokenTotals

_affiliations_gate = threading.BoundedSemaphore(4)
_case_read_gate = threading.BoundedSemaphore(4)
_CASE_LINK_PAGE_SIZE = 200
_SQLITE_ID_MAX = 2**63 - 1


@contextmanager
def _case_read():
    # The synchronous handler owns the slot through SQL and response encoding,
    # even if the HTTP client disconnects.
    if not _case_read_gate.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="case_read_busy")
    try:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail="case_read_unavailable")
        with db.read_snapshot():
            yield db
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="case_read_unavailable") from exc
    finally:
        _case_read_gate.release()


def _case_link_page(db, flow_run_id: str, after_id: int):
    rows = db.list_flow_links(
        flow_run_id=flow_run_id, after_id=after_id, limit=_CASE_LINK_PAGE_SIZE + 1,
    )
    next_cursor = str(rows[_CASE_LINK_PAGE_SIZE - 1]["id"]) if len(rows) > _CASE_LINK_PAGE_SIZE else None
    return rows[:_CASE_LINK_PAGE_SIZE], next_cursor


# --- Response models (documentation only; handlers return JSONResponse) --------
# These describe the success (200) body shape of each read-only projection for
# the generated API reference. They are NOT used to validate/serialize responses
# — the handlers below return JSONResponse verbatim — only to document schema.


class FlowSummary(BaseModel):
    flow_run_id: Optional[str] = None
    task_id: Optional[str] = None
    current_stage: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CaseSummary(BaseModel):
    flow_run_id: Optional[str] = None
    task_id: Optional[str] = None
    objective_lock: Optional[str] = None
    current_stage: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    parent_flow_run_id: Optional[str] = None
    dispatched_by: Optional[str] = None
    dispatch_file: Optional[str] = None
    bucket: str


class LinkRow(BaseModel):
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    role: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    metadata_json: Optional[str] = None


class FlowsOut(BaseModel):
    flows: list[FlowSummary]


class FlowDetailOut(BaseModel):
    flow: dict


class WorkBucketCounts(BaseModel):
    needs_decision: int
    blocked: int
    review: int
    active: int
    closed: int
    unknown: int


class WorkListOut(BaseModel):
    cases: list[CaseSummary]
    bucket_counts: WorkBucketCounts
    total: int


class SessionAffiliation(BaseModel):
    session_id: str
    flow_run_id: Optional[str] = None
    role: str
    objective_lock: Optional[str] = None
    case_status: Optional[str] = None


class SessionAffiliationsOut(BaseModel):
    affiliations: list[SessionAffiliation]
    total: int


class CompactSessionAffiliation(BaseModel):
    session_id: str
    flow_run_id: str | None = None
    role: Literal["manager", "worker", "reviewer", "evidence", "session"]
    case_status: str | None = None
    case_title: str = Field(description="caseTitle equivalent, at most 120 UTF-16 units; no raw objective")


class CompactSessionAffiliationsOut(BaseModel):
    affiliations: list[CompactSessionAffiliation]
    total: int


class CaseLedger(BaseModel):
    tasks: list[LinkRow]
    sessions: list[LinkRow]
    approvals: list[LinkRow]
    artifacts: list[LinkRow]
    jobs: list[LinkRow]
    flows: list[LinkRow]
    other: list[LinkRow]


class CaseCounts(BaseModel):
    links: int
    events: int
    children: int


class CaseCoverage(BaseModel):
    has_links: bool
    has_events: bool
    has_parent: bool
    is_root: bool


class CaseDetailOut(BaseModel):
    next_link_cursor: str | None = None
    case: CaseSummary
    record: dict
    ledger: CaseLedger
    parent: Optional[CaseSummary] = None
    children: list[CaseSummary]
    counts: CaseCounts
    coverage: CaseCoverage


class TimelineEvent(BaseModel):
    id: Optional[int] = None
    event_type: Optional[str] = None
    actor: Optional[str] = None
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    payload_json: Optional[str] = None
    created_at: Optional[str] = None


class CaseTimelineOut(BaseModel):
    next_link_cursor: str | None = None
    total_event_count: int
    flow_run_id: str
    events: list[TimelineEvent]
    evidence: list[LinkRow]
    event_count: int


class GraphNode(BaseModel):
    flow_run_id: Optional[str] = None
    rel: str
    current_stage: Optional[str] = None
    status: Optional[str] = None
    bucket: str
    objective_lock: Optional[str] = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    role: str


class CaseGraphOut(BaseModel):
    flow_run_id: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class RosterSession(BaseModel):
    session_id: Optional[str] = None
    role: str
    present: bool
    backend: Optional[str] = None
    status: Optional[str] = None
    model: Optional[str] = None
    node: str
    last_activity: Optional[str] = None
    last_report: Optional[str] = None
    turn_count: int
    tokens: TokenTotals


class RosterJob(BaseModel):
    job_id: Optional[str] = None
    label: Optional[str] = None
    command_summary: Optional[str] = None
    session_id: Optional[str] = None
    node: Optional[str] = None
    status: str
    started_at: Optional[str] = None
    started_epoch: Optional[int] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    tail: Optional[str] = None
    orphaned: bool
    is_agent_spawn: bool


class RosterCounts(BaseModel):
    sessions: int
    jobs: int
    running_jobs: int


class CaseRosterOut(BaseModel):
    next_session_cursor: str | None = None
    next_job_cursor: str | None = None
    flow_run_id: str
    sessions: list[RosterSession]
    jobs: list[RosterJob]
    counts: RosterCounts
    token_totals: TokenTotals


def build_router(orchestrator: Any, *, require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])

    # The summary columns for the list view (packet §11): the six a human scans
    # to answer "what flows exist and at what stage". The full record (all §11
    # columns) is only served by the detail route, so the list stays cheap.
    _FLOW_SUMMARY_FIELDS = (
        "flow_run_id", "task_id", "current_stage", "status", "created_at", "updated_at",
    )

    @router.get(
        "/api/flows",
        responses={200: {"model": FlowsOut, "description": "Flow-run summaries, newest first."}},
    )
    def api_flows(
        task_id: Optional[str] = Query(default=None),
        limit: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        """List flow-run records (newest first), projected to summary fields.

        Optional ``task_id`` filters to one task's flows. Read-only: reuses
        ``db.list_flow_runs`` verbatim, then projects each row to the summary
        columns. Absent §11 columns stay null (never fabricated)."""
        db = core._db()
        rows = db.list_flow_runs(task_id=task_id, limit=limit) if db is not None else []
        flows = [{k: row.get(k) for k in _FLOW_SUMMARY_FIELDS} for row in rows]
        return JSONResponse({"flows": flows})

    @router.get(
        "/api/flows/{flow_run_id}",
        responses={200: {"model": FlowDetailOut, "description": "One flow's full §11 DB record."}},
    )
    def api_flow_detail(flow_run_id: str) -> JSONResponse:
        """One flow's full §11 record. 404 (``flow_not_found``) on an unknown id.

        Read-only: reuses ``db.get_flow_run`` verbatim. The whole DB row is
        returned as-is, so NULL columns serialize as JSON null."""
        db = core._db()
        flow = db.get_flow_run(flow_run_id) if db is not None else None
        if flow is None:
            raise HTTPException(status_code=404, detail="flow_not_found")
        return JSONResponse({"flow": flow})

    # ----------------------------------------------------------------------
    # Work / Case read model (A27) — read-only projections over the A25/A26
    # substrate (flow_runs + flow_links + flow_events). Honesty-first: missing
    # links render as empty/unknown, never inferred. No mutation endpoints.
    # ----------------------------------------------------------------------

    @router.get(
        "/api/work",
        responses={200: {"model": WorkListOut, "description": "Case summaries with attention-bucket counts."}},
    )
    def api_work_list(
        bucket: Optional[str] = Query(default=None),
        limit: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        """List Work/Case summaries (newest first) with attention buckets.

        Small summaries only (no per-case link/event queries). Optional ``bucket``
        filters to one attention section (needs_decision|blocked|review|active|
        closed|unknown). Buckets derive from AUTHORITATIVE status/current_stage
        only — never from timestamps or prose."""
        from src.control import work_read_model as _wrm
        db = core._db()
        rows = db.list_flow_runs(limit=limit) if db is not None else []
        model = _wrm.build_work_list(rows)
        if bucket:
            model["cases"] = [c for c in model["cases"] if c["bucket"] == bucket]
        return JSONResponse(model)

    @router.get(
        "/api/work/affiliations/sessions",
        responses={200: {"model": SessionAffiliationsOut | CompactSessionAffiliationsOut, "description": "Whole-substrate index. Default legacy carries objective_lock; format=compact carries case_title only. Both formats return 503 when unavailable and 429 at admission capacity."}},
    )
    def api_work_session_affiliations(format: Literal["legacy", "compact"] = Query("legacy")) -> Response:
        """Authoritative session→case affiliation index over the WHOLE substrate.

        One JOIN (flow_links entity_type='session' → flow_runs) — no per-case
        fanout, no cap — so a session linked to a case anywhere in the backlog is
        resolved, never a false Standalone (milestone authority rule 7). Registered
        BEFORE ``/api/work/{flow_run_id}`` so the literal path is never captured as
        a flow id. Empty ``affiliations`` when the substrate has no session links."""
        from src.control import work_read_model as _wrm
        if not _affiliations_gate.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="affiliations_busy")
        try:
            db = core._db()
            if db is None:
                raise RuntimeError("affiliations database unavailable")
            if format == "compact":
                with db.compact_session_case_data() as (links, cases):
                    result = _wrm.build_compact_session_affiliations(links, cases)
                return Response(json.dumps(result, ensure_ascii=True, separators=(",", ":")), media_type="application/json")
            with db.read_snapshot():
                rows = db.list_session_case_links()
                return JSONResponse(_wrm.build_session_affiliations(rows))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="affiliations_unavailable") from exc
        finally:
            _affiliations_gate.release()

    @router.get(
        "/api/work/{flow_run_id}",
        responses={200: {"model": CaseDetailOut, "description": "One case: summary, full record, grouped ledger, lineage."}},
    )
    def api_work_detail(
        flow_run_id: str = Path(max_length=128),
        link_after: int = Query(0, ge=0, le=_SQLITE_ID_MAX),
    ) -> JSONResponse:
        """Case summary, authoritative link count and an oldest-first ledger page."""
        from src.control import work_read_model as _wrm

        with _case_read() as db:
            flow = db.get_flow_run(flow_run_id)
            if flow is None:
                raise HTTPException(status_code=404, detail="case_not_found")
            links, next_cursor = _case_link_page(db, flow_run_id, link_after)
            event_count = db.count_flow_events(flow_run_id)
            parent_id = flow.get("parent_flow_run_id")
            parent = db.get_flow_run(parent_id) if parent_id else None
            children = db.list_child_flow_runs(flow_run_id)
            model = _wrm.build_case_detail(flow, links, event_count, parent, children)
            link_count = db.count_flow_links(flow_run_id)
            model["counts"]["links"] = link_count
            model["coverage"]["has_links"] = link_count > 0
            model["next_link_cursor"] = next_cursor
            return JSONResponse(model)

    @router.get(
        "/api/work/{flow_run_id}/timeline",
        responses={200: {"model": CaseTimelineOut, "description": "Case audit trail: newest flow_events + linked evidence."}},
    )
    def api_work_timeline(
        flow_run_id: str = Path(max_length=128),
        limit: int = Query(500, ge=1, le=2000),
        link_after: int = Query(0, ge=0, le=_SQLITE_ID_MAX),
    ) -> JSONResponse:
        """Newest audit events and an independently paged linked evidence list."""
        from src.control import work_read_model as _wrm

        with _case_read() as db:
            if db.get_flow_run(flow_run_id) is None:
                raise HTTPException(status_code=404, detail="case_not_found")
            events = db.list_flow_events(flow_run_id, limit=limit, newest=True)
            links, next_cursor = _case_link_page(db, flow_run_id, link_after)
            model = _wrm.build_case_timeline(flow_run_id, events, links)
            model["next_link_cursor"] = next_cursor
            model["total_event_count"] = db.count_flow_events(flow_run_id)
            return JSONResponse(model)

    @router.get(
        "/api/work/{flow_run_id}/graph",
        responses={200: {"model": CaseGraphOut, "description": "Compact lineage graph: case, parent, children + edges."}},
    )
    def api_work_graph(flow_run_id: str) -> JSONResponse:
        """Compact lineage graph: the case, its parent, and direct children, with
        parent→child edges from authoritative lineage. 404 on unknown case."""
        from src.control import work_read_model as _wrm
        db = core._db()
        flow = db.get_flow_run(flow_run_id) if db is not None else None
        if flow is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        parent_id = flow.get("parent_flow_run_id")
        parent = db.get_flow_run(parent_id) if parent_id else None
        children = db.list_child_flow_runs(flow_run_id)
        return JSONResponse(_wrm.build_case_graph(flow_run_id, flow, parent, children))

    @router.get(
        "/api/work/{flow_run_id}/roster",
        responses={200: {"model": CaseRosterOut, "description": "Live operational roster: sessions + jobs + token totals."}},
    )
    def api_work_roster(
        flow_run_id: str = Path(max_length=128),
        session_after: int = Query(0, ge=0, le=_SQLITE_ID_MAX),
        job_before: str | None = Query(None, max_length=512),
    ) -> JSONResponse:
        """Page sessions and jobs independently, with complete Case-wide totals."""
        from src.control import work_read_model as _wrm

        before = None
        if job_before is not None:
            timestamp, separator, job_id = job_before.partition("|")
            if not separator or not timestamp or not job_id:
                raise HTTPException(status_code=422, detail="invalid_job_cursor")
            before = (timestamp, job_id)
        with _case_read() as db:
            if db.get_flow_run(flow_run_id) is None:
                raise HTTPException(status_code=404, detail="case_not_found")
            page_size = _CASE_LINK_PAGE_SIZE
            session_page = db.case_roster_session_page(flow_run_id, session_after, page_size + 1)
            job_page = db.case_roster_job_page(flow_run_id, before, page_size + 1)
            session_links = session_page[:page_size]
            session_ids = [row["entity_id"] for row in session_links if row["entity_id"]]
            session_rows = {row["session_id"]: row for row in session_links if row["session_id"]}
            model = _wrm.build_case_roster(
                flow_run_id, session_links, session_rows, db.get_session_token_totals(session_ids),
                db.get_session_turn_counts(session_ids), job_page[:page_size],
            )
            model["counts"], model["token_totals"] = db.case_roster_totals(flow_run_id)
            model["next_session_cursor"] = str(session_links[-1]["id"]) if len(session_page) > page_size else None
            last_job = job_page[page_size - 1] if len(job_page) > page_size else None
            model["next_job_cursor"] = f'{last_job["created_at"]}|{last_job["id"]}' if last_job else None
            return JSONResponse(model)

    return router
