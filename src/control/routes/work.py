"""Read-only Case projections — flow_runs (`/api/flows`) and the Work surface (`/api/work`).

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md "2b.
Manager / Case surface". Module-level helpers are looked up as ``core.<name>`` so tests that
monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from src.control import control_api as core


def build_router(orchestrator: Any, *, require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])

    # The summary columns for the list view (packet §11): the six a human scans
    # to answer "what flows exist and at what stage". The full record (all §11
    # columns) is only served by the detail route, so the list stays cheap.
    _FLOW_SUMMARY_FIELDS = (
        "flow_run_id", "task_id", "current_stage", "status", "created_at", "updated_at",
    )

    @router.get("/api/flows")
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

    @router.get("/api/flows/{flow_run_id}")
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

    @router.get("/api/work")
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

    @router.get("/api/work/affiliations/sessions")
    def api_work_session_affiliations() -> JSONResponse:
        """Authoritative session→case affiliation index over the WHOLE substrate.

        One JOIN (flow_links entity_type='session' → flow_runs) — no per-case
        fanout, no cap — so a session linked to a case anywhere in the backlog is
        resolved, never a false Standalone (milestone authority rule 7). Registered
        BEFORE ``/api/work/{flow_run_id}`` so the literal path is never captured as
        a flow id. Empty ``affiliations`` when the substrate has no session links."""
        from src.control import work_read_model as _wrm
        db = core._db()
        rows = db.list_session_case_links() if db is not None else []
        return JSONResponse(_wrm.build_session_affiliations(rows))

    @router.get("/api/work/{flow_run_id}")
    def api_work_detail(flow_run_id: str) -> JSONResponse:
        """One case: summary + full record + grouped ledger + parent/children.

        404 (``case_not_found``) on an unknown flow_run_id. Linked entities come
        from flow_links only; absent sections render empty, not inferred."""
        from src.control import work_read_model as _wrm
        db = core._db()
        flow = db.get_flow_run(flow_run_id) if db is not None else None
        if flow is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        links = db.list_flow_links(flow_run_id=flow_run_id)
        event_count = db.count_flow_events(flow_run_id)  # [A104 I7] exact, not capped
        parent_id = flow.get("parent_flow_run_id")
        parent = db.get_flow_run(parent_id) if parent_id else None
        children = db.list_child_flow_runs(flow_run_id)
        model = _wrm.build_case_detail(flow, links, event_count, parent, children)
        return JSONResponse(model)

    @router.get("/api/work/{flow_run_id}/timeline")
    def api_work_timeline(
        flow_run_id: str,
        limit: int = Query(500, ge=1, le=2000),
    ) -> JSONResponse:
        """The case audit trail: the NEWEST ``limit`` flow_events, in order, +
        linked evidence pointers (A104 I7: a long Case shows its latest events —
        incl. a worker's ``task.finished`` for an un-redeployed wait_for_worker).
        404 on unknown case."""
        from src.control import work_read_model as _wrm
        db = core._db()
        flow = db.get_flow_run(flow_run_id) if db is not None else None
        if flow is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        events = db.list_flow_events(flow_run_id, limit=limit, newest=True)
        links = db.list_flow_links(flow_run_id=flow_run_id)
        return JSONResponse(_wrm.build_case_timeline(flow_run_id, events, links))

    @router.get("/api/work/{flow_run_id}/graph")
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

    @router.get("/api/work/{flow_run_id}/roster")
    def api_work_roster(flow_run_id: str) -> JSONResponse:
        """[Cockpit] The live operational roster for a Case — the "who is doing what
        right now" head of the Case, complementing the flow_events timeline (spine).

        Per Case: its sessions (manager + workers) with role, model, status, token
        totals, and turn count; and the running/finished SCRIPTS (watch_job rows)
        owned by those sessions, with orphaned/failed/agent-spawn flags. The join is
        case → flow_links(session) → those sessions' jobs. All reads are batched
        (no N+1); job liveness comes from worker-maintained status, never a probe.
        404 on unknown case."""
        from src.control import work_read_model as _wrm
        db = core._db()
        flow = db.get_flow_run(flow_run_id) if db is not None else None
        if flow is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        session_links = db.list_flow_links(flow_run_id=flow_run_id, entity_type="session")
        session_ids = [l.get("entity_id") for l in session_links if l.get("entity_id")]
        session_rows_by_id = {
            sid: row for sid in session_ids
            if (row := db.get_session(sid)) is not None
        }
        token_totals = db.get_session_token_totals(session_ids)
        turn_counts = db.get_session_turn_counts(session_ids)
        jobs = db.list_jobs_for_sessions(session_ids)
        return JSONResponse(_wrm.build_case_roster(
            flow_run_id, session_links, session_rows_by_id,
            token_totals, turn_counts, jobs,
        ))

    return router
