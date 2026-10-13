"""Admin routes — runtime flags, approvals, browser push, git.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md
"Approvals, push, runtime flags, git". Module-level helpers are looked up as ``core.<name>``
so tests that monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.control import control_api as core
from src.control.control_api import (
    _REASON_STATUS,
    ApprovalRequestBody,
    ApprovalResolveBody,
    GitCommitBody,
    PushSubscribeBody,
    PushUnsubscribeBody,
    RuntimeFlagBody,
)
from src.control.routes._responses import CommandResult


# --- OpenAPI success-response models (documentation only; handlers still
# return raw JSONResponse, so FastAPI passes the body through untouched). ---


class FlagOut(BaseModel):
    """A single effective runtime flag."""

    flag_name: str
    value: bool
    raw_value: str
    source: str
    effect_scope: str
    registry_writable: bool
    description: str
    registry: Optional[dict] = None
    env_value: Optional[str] = None


class ApprovalOut(BaseModel):
    """A single approval request record."""

    id: str
    session_id: Optional[str]
    task_id: Optional[str]
    action: str
    risk: str
    reversible: int
    status: str
    requested_by: str
    resolved_by: Optional[str]
    payload: Optional[str]
    created_at: str
    resolved_at: Optional[str]
    expires_at: Optional[str]
    flow_run_id: Optional[str] = None


class FlagsOut(CommandResult):
    """``GET /api/flags`` — the command envelope plus the flag list."""

    flags: list[FlagOut]


class FlagSetOut(CommandResult):
    """``PUT /api/flags/{flag_name}`` — envelope plus the updated flag."""

    flag: FlagOut


class FlagDeleteOut(CommandResult):
    """``DELETE /api/flags/{flag_name}`` — envelope plus the cleared flag."""

    deleted: bool
    flag: FlagOut


class ApprovalsOut(BaseModel):
    """``GET /api/approvals`` — the list of pending/resolved approvals."""

    approvals: list[ApprovalOut]


class ApprovalCreateOut(CommandResult):
    """``POST /api/approvals`` — envelope plus the created approval."""

    approval: ApprovalOut


class ApprovalResolveOut(CommandResult):
    """``POST /api/approvals/{approval_id}/resolve`` — envelope plus the approval."""

    reason: str
    approval: ApprovalOut


class PushStatusOut(BaseModel):
    """``GET /api/push/status`` — browser-push availability snapshot."""

    available: bool
    reason: str
    vapid_public_key: str
    missing_env: list[str]
    enabled_subscriptions: int


class GitStatusChanges(BaseModel):
    modified: list[str]
    created: list[str]
    deleted: list[str]
    total: int


class GitStatusSafety(BaseModel):
    safe_files: list[str]
    sensitive_files: list[str]
    has_sensitive_files: bool


class GitStatusOut(BaseModel):
    """``POST /api/git/status`` — working-tree status + safety classification."""

    current_branch: str
    working_directory_clean: bool
    changes: GitStatusChanges
    staged_files: list[str]
    unstaged_files: list[str]
    safety: GitStatusSafety


class GitCommitOut(BaseModel):
    """``POST /api/git/commit`` — commit result with safety blocking."""

    success: bool
    branch_name: Optional[str]
    commit_hash: Optional[str]
    files_committed: list[str]
    sensitive_files_blocked: list[str]
    errors: list[str]


class GitCommitAllOut(BaseModel):
    """``POST /api/git/commit_all`` — commit-all result."""

    success: bool
    branch_name: Optional[str]
    commit_hash: Optional[str]
    files_committed: list[str]
    errors: list[str]


def build_router(
    orchestrator: Any,
    *,
    require_auth: Callable[..., Any],
    idem_put: Callable[..., Any],
    idem_guard: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get("/api/flags", responses={200: {"model": FlagsOut, "description": "Effective runtime flags."}})
    def api_list_flags() -> JSONResponse:
        """List effective runtime flags.

        Rows in ``runtime_flags`` are overrides. Missing rows fall back to the
        current process env and then the compiled default, so introducing this
        endpoint does not change behavior until a flag is explicitly written.
        """
        from src.control.db import RUNTIME_FLAG_DEFINITIONS, render_runtime_flag

        db = core._db()
        return JSONResponse({
            "ok": True,
            "flags": [
                render_runtime_flag(name, db=db)
                for name in sorted(RUNTIME_FLAG_DEFINITIONS.keys())
            ],
        })

    @router.put("/api/flags/{flag_name}", responses={200: {"model": FlagSetOut, "description": "Updated runtime flag."}})
    def api_set_flag(flag_name: str, body: RuntimeFlagBody) -> JSONResponse:
        from src.control.db import (
            RUNTIME_FLAG_DEFINITIONS,
            render_runtime_flag,
            runtime_flag_registry_writable,
        )

        name = (flag_name or "").strip().upper()
        if name not in RUNTIME_FLAG_DEFINITIONS:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": "unknown_flag"},
            )
        if not runtime_flag_registry_writable(name):
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "reason": "flag_not_registry_writable",
                    "message": (
                        f"{name} is an env-controlled flag, not registry-writable. "
                        "Set it in .env and restart the gateway to change it."
                    ),
                },
            )
        db = core._db()
        if db is None:
            raise HTTPException(
                status_code=503,
                detail={"ok": False, "reason": "runtime_flag_store_unavailable"},
            )
        db.set_runtime_flag(name, body.value, source="api", set_by=body.set_by or "")
        return JSONResponse({"ok": True, "flag": render_runtime_flag(name, db=db)})

    @router.delete("/api/flags/{flag_name}", responses={200: {"model": FlagDeleteOut, "description": "Cleared runtime flag override."}})
    def api_delete_flag(flag_name: str) -> JSONResponse:
        from src.control.db import (
            RUNTIME_FLAG_DEFINITIONS,
            render_runtime_flag,
            runtime_flag_registry_writable,
        )

        name = (flag_name or "").strip().upper()
        if name not in RUNTIME_FLAG_DEFINITIONS:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": "unknown_flag"},
            )
        if not runtime_flag_registry_writable(name):
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "reason": "flag_not_registry_writable",
                    "message": (
                        f"{name} is an env-controlled flag, not registry-writable. "
                        "Set it in .env and restart the gateway to change it."
                    ),
                },
            )
        db = core._db()
        if db is None:
            raise HTTPException(
                status_code=503,
                detail={"ok": False, "reason": "runtime_flag_store_unavailable"},
            )
        deleted = db.delete_runtime_flag(name)
        return JSONResponse({"ok": True, "deleted": deleted, "flag": render_runtime_flag(name, db=db)})

    def _approval_service():
        """The orchestrator's ApprovalService if it wired one (with a real
        on-approve dispatch callback); else a queue-only service over the shared
        DB. Built lazily so the stub orchestrator in tests works too."""
        svc = getattr(orchestrator, "approval_service", None)
        if svc is not None:
            return svc
        db = core._db()
        if db is None:
            return None
        from src.services.approval_service import ApprovalService
        return ApprovalService(db)

    @router.get("/api/approvals", responses={200: {"model": ApprovalsOut, "description": "Approval requests."}})
    def api_list_approvals(
        status: Optional[str] = Query(default="pending"),
        limit: int = Query(50, ge=1, le=200),
    ) -> JSONResponse:
        """The queryable pending queue (rebuilds the UI after a restart). Pass
        ``status=`` (empty) to list all."""
        svc = _approval_service()
        if svc is None:
            return JSONResponse({"approvals": []})
        approvals = svc.list(status=status or None, limit=limit)
        return JSONResponse({"approvals": approvals})

    @router.post("/api/approvals", responses={200: {"model": ApprovalCreateOut, "description": "Created approval request."}})
    def api_request_approval(
        body: ApprovalRequestBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        """Record a pending approval (the seam a gated action calls). Exposed so the
        queue is exercisable end-to-end before a backend emits these natively."""
        with idem_guard("request_approval", idempotency_key) as cached:
            if cached is not None:
                return JSONResponse(cached)
            svc = _approval_service()
            if svc is None:
                raise HTTPException(status_code=503, detail="approvals_unavailable")
            result = svc.request(
                action=body.action, session_id=body.session_id, task_id=body.task_id,
                risk=body.risk, reversible=body.reversible, requested_by=body.requested_by,
            )
            if not result.ok:
                raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400),
                                    detail={"ok": False, "reason": result.reason})
            approval_id = result.reason  # request() carries the id on reason
            resp = {"ok": True, "approval": svc.get(approval_id)}
            idem_put("request_approval", idempotency_key, resp)
            return JSONResponse(resp)

    @router.post("/api/approvals/{approval_id}/resolve", responses={200: {"model": ApprovalResolveOut, "description": "Resolved approval request."}})
    async def api_resolve_approval(approval_id: str, body: ApprovalResolveBody) -> JSONResponse:
        """Approve or reject a pending approval. The guarded transition makes a
        double-resolve return 409 (already_resolved), not a second dispatch."""
        svc = _approval_service()
        if svc is None:
            raise HTTPException(status_code=503, detail="approvals_unavailable")
        result = await svc.resolve(approval_id, body.decision, resolved_by=body.resolved_by)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400),
                                detail={"ok": False, "reason": result.reason})
        return JSONResponse({"ok": True, "reason": "", "approval": svc.get(approval_id)})

    # --- web push (#21) ---

    @router.get("/api/push/status", responses={200: {"model": PushStatusOut, "description": "Browser-push availability."}})
    def api_push_status() -> JSONResponse:
        """Report whether push is available (VAPID configured + transport present)
        and expose the public VAPID key the browser needs to subscribe."""
        from config import config as _cfg
        from src.services.push_service import push_available

        db = core._db()
        available, reason = push_available(_cfg, db)
        push_cfg = getattr(_cfg, "push", None)
        pub = getattr(push_cfg, "vapid_public_key", "") or ""
        missing = push_cfg.missing_config() if push_cfg and hasattr(push_cfg, "missing_config") else []
        sub_count = 0
        try:
            if db is not None:
                sub_count = len(db.list_push_subscriptions(enabled_only=True))
        except Exception:
            sub_count = 0
        return JSONResponse({
            "available": available,
            "reason": reason,
            "vapid_public_key": pub if available else "",
            # Operator diagnostics: names the exact unset env vars + live sub count.
            "missing_env": missing,
            "enabled_subscriptions": sub_count,
        })

    @router.post("/api/push/subscribe", responses={200: {"model": CommandResult, "description": "Subscription stored."}})
    async def api_push_subscribe(request: Request) -> JSONResponse:
        """Register/refresh a browser push subscription (idempotent by endpoint).

        Enforces a hard body-size cap BEFORE parsing — FastAPI does not bound
        request bodies by default and subscribe payloads are tiny.
        """
        from config import config as _cfg

        max_bytes = int(getattr(getattr(_cfg, "push", None), "max_subscribe_bytes", 4096))
        # Reject BEFORE buffering the body: request.body() reads the whole payload
        # into memory, so an oversized Content-Length must be caught up front. The
        # post-read length check below is the fallback for chunked/absent CL.
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_bytes:
                    raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})
            except ValueError:
                raise HTTPException(status_code=400, detail={"ok": False, "reason": "bad_content_length"})
        raw = await request.body()
        if len(raw) > max_bytes:
            raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})
        try:
            body = PushSubscribeBody.model_validate_json(raw)
        except Exception:
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "invalid_subscription"})

        if not body.endpoint or not body.keys.p256dh or not body.keys.auth:
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "invalid_subscription"})

        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        db.upsert_push_subscription(
            endpoint=body.endpoint,
            p256dh_key=body.keys.p256dh,
            auth_key=body.keys.auth,
            label=(body.label or "")[:120] or None,
        )
        return JSONResponse({"ok": True})

    @router.post("/api/push/unsubscribe", responses={200: {"model": CommandResult, "description": "Subscription removed."}})
    def api_push_unsubscribe(body: PushUnsubscribeBody) -> JSONResponse:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        db.disable_push_subscription(body.endpoint)
        return JSONResponse({"ok": True})

    # --- backend account + usage visibility (#30/#33) ---

    # REVISIT (2026-10-05): no caller yet; meant for the Web UI git panel - wire it or drop it.
    @router.post("/api/git/status", responses={200: {"model": GitStatusOut, "description": "Working-tree status and safety."}})
    def api_git_status() -> JSONResponse:
        from src.services.git_automation import GitAutomationService
        return JSONResponse(GitAutomationService().get_git_status_summary())

    # REVISIT (2026-10-05): no caller yet; meant for the Web UI git panel - wire it or drop it.
    @router.post("/api/git/commit", responses={200: {"model": GitCommitOut, "description": "Commit result."}})
    def api_git_commit(body: GitCommitBody) -> JSONResponse:
        from src.services.git_automation import GitAutomationService
        result = GitAutomationService().safe_commit_task(
            task_id=body.task_id,
            task_description=body.task_description or f"Task {body.task_id} changes",
            create_branch=body.create_branch,
            push_branch=body.push_branch,
        )
        return JSONResponse(result)

    # REVISIT (2026-10-05): no caller yet; meant for the Web UI git panel - wire it or drop it.
    @router.post("/api/git/commit_all", responses={200: {"model": GitCommitAllOut, "description": "Commit-all result."}})
    def api_git_commit_all(body: GitCommitBody) -> JSONResponse:
        from src.services.git_automation import GitAutomationService
        result = GitAutomationService().commit_all_staged(
            task_id=body.task_id,
            task_description=body.task_description or f"Task {body.task_id} changes",
            create_branch=body.create_branch,
            push_branch=body.push_branch,
        )
        return JSONResponse(result)

    return router
