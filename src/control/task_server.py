"""
Mesh task server — FastAPI app (VPS-side).

Locally testable without Tailscale or a VPS:
    uvicorn src.control.task_server:app --host 127.0.0.1 --port 9002

All endpoints except /health require:
    Authorization: Bearer {WORKER_TOKEN}

The backing store is MeshDB (src/control/db.py). No SQL lives here.
"""

import json
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.control.db import get_db
from src.control.node_registry import NodeInfo, NodeCapabilities, get_registry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    get_registry().start()
    logger.info("event=task_server_started")
    yield
    get_registry().stop()


app = FastAPI(title="AI-Team Mesh Task Server", version="1.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


@lru_cache(maxsize=1)
def _worker_token() -> str:
    try:
        from config import config as _cfg
        return _cfg.mesh.worker_token
    except Exception:
        import os
        return os.getenv("WORKER_TOKEN", "")


def _require_auth(
    creds: HTTPAuthorizationCredentials = Security(_bearer),
) -> None:
    token = _worker_token()
    if not token:
        raise HTTPException(status_code=500, detail="WORKER_TOKEN not configured on server")
    if creds.credentials != token:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class _Capabilities(BaseModel):
    backends: List[str] = []
    max_concurrent: int = 2
    projects_root: str = ""
    repos: List[Dict[str, str]] = []


class NodeRegisterPayload(BaseModel):
    node_id: str
    tailscale_ip: str = ""
    api_port: int = 9001
    capabilities: _Capabilities = _Capabilities()


class HeartbeatPayload(BaseModel):
    node_id: str


class DeregisterPayload(BaseModel):
    node_id: str


class ClaimPayload(BaseModel):
    node_id: str


class ExecutionResultPayload(BaseModel):
    node_id: str
    success: bool
    output: str = ""
    errors: List[str] = []
    files_modified: List[str] = []
    execution_time: float = 0.0
    timestamp: str = ""
    return_code: int = 0
    artifact_path: Optional[str] = None
    backend_session_id: str = ""  # worker echoes back the native session ID for affinity continuity
    error_detail: str = ""  # full traceback when the worker caught an exception (D2)


# ---------------------------------------------------------------------------
# Job models (T3)
# ---------------------------------------------------------------------------

class RegisterJobPayload(BaseModel):
    node_id: str
    session_id: Optional[str] = None
    label: str
    command: Optional[str] = None
    log_path: Optional[str] = None
    notify: bool = True
    notify_agent: bool = False


class JobDonePayload(BaseModel):
    node_id: str
    exit_code: int
    tail: str = ""


class JobStartPayload(BaseModel):
    node_id: str
    pid: int
    pgid: int = 0
    log_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, Any]:
    db = get_db()
    stats = db.stats() if db else {}
    return {"status": "ok", "db": stats}


# ---------------------------------------------------------------------------
# Metrics — live aggregates for operators and the future project-manager agent
# ---------------------------------------------------------------------------

@app.get("/metrics", dependencies=[Depends(_require_auth)])
def metrics() -> Dict[str, Any]:
    """Live system aggregates: task counts, node liveness, success rate.

    Authenticated (unlike /health) because it exposes operational detail. Built
    from db.stats() plus the in-memory registry, so it reflects the embedded
    server's real-time view. Intended to be polled over Tailscale rather than
    tailing logs, and to be consumed by a task-distributing agent.
    """
    db = get_db()
    s = db.stats() if db else {}
    completed = s.get("tasks_completed", 0)
    failed = s.get("tasks_failed", 0)
    finished = completed + failed
    success_rate = round(100.0 * completed / finished, 1) if finished else None

    registry = get_registry()
    nodes = [
        {
            "node_id": n.node_id,
            "status": n.status,
            "backends": n.capabilities.backends,
            "last_heartbeat": n.last_heartbeat.isoformat() if n.last_heartbeat else None,
        }
        for n in registry.list_all()
    ]

    return {
        "tasks": {
            "pending": s.get("tasks_pending", 0),
            "claimed": s.get("tasks_claimed", 0),
            "completed": completed,
            "failed": failed,
            "success_rate_pct": success_rate,
        },
        "nodes": {
            "online": s.get("nodes_online", 0),
            "total": s.get("nodes_total", 0),
            "detail": nodes,
        },
        "sessions": {
            "total": s.get("sessions_total", 0),
            "busy": s.get("sessions_busy", 0),
        },
        "schema_version": s.get("schema_version", 0),
    }


# ---------------------------------------------------------------------------
# Node endpoints
# ---------------------------------------------------------------------------

@app.post("/nodes/register", dependencies=[Depends(_require_auth)])
def register_node(payload: NodeRegisterPayload) -> Dict[str, str]:
    info = NodeInfo(
        node_id=payload.node_id,
        tailscale_ip=payload.tailscale_ip,
        api_port=payload.api_port,
        capabilities=NodeCapabilities(
            backends=list(payload.capabilities.backends),
            max_concurrent=payload.capabilities.max_concurrent,
            projects_root=payload.capabilities.projects_root,
            repos=list(payload.capabilities.repos),
        ),
    )
    get_registry().register(info)
    return {"status": "registered", "node_id": payload.node_id}


@app.post("/nodes/heartbeat", dependencies=[Depends(_require_auth)])
def node_heartbeat(payload: HeartbeatPayload) -> Dict[str, str]:
    ok = get_registry().heartbeat(payload.node_id)
    if not ok:
        # Unknown node — prompt re-register instead of silently failing
        raise HTTPException(status_code=404, detail="Node not found; send /nodes/register first")
    return {"status": "ok"}


@app.post("/nodes/deregister", dependencies=[Depends(_require_auth)])
def deregister_node(payload: DeregisterPayload) -> Dict[str, str]:
    get_registry().deregister(payload.node_id)
    return {"status": "deregistered", "node_id": payload.node_id}


@app.get("/nodes", dependencies=[Depends(_require_auth)])
def list_nodes() -> List[Dict[str, Any]]:
    return [n.to_dict() for n in get_registry().list_all()]


@app.post("/nodes/{node_id}/nudge", dependencies=[Depends(_require_auth)])
def nudge_node(node_id: str) -> Dict[str, str]:
    """VPS pushes a nudge to a worker so it polls immediately.

    The actual HTTP call to the worker's nudge listener is fire-and-forget;
    this endpoint just records the intent. The worker's poll loop will pick
    up tasks on its next cycle regardless.
    """
    node = get_registry().get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id!r} not found")
    if node.status != "online":
        raise HTTPException(status_code=409, detail=f"Node {node_id!r} is offline")
    # Best-effort HTTP push to worker's nudge listener
    _fire_nudge(node)
    return {"status": "nudged", "node_id": node_id}


def _fire_nudge(node: NodeInfo) -> None:
    """Non-blocking HTTP POST to worker nudge endpoint. Failures are logged, not raised."""
    import threading
    import urllib.request

    if not node.tailscale_ip:
        logger.debug("event=nudge_skipped node_id=%s reason=no_tailscale_ip", node.node_id)
        return

    def _do() -> None:
        url = f"http://{node.tailscale_ip}:{node.api_port}/nudge"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=3):
                pass
        except Exception as e:
            logger.debug("event=nudge_failed node_id=%s url=%s err=%s", node.node_id, url, e)

    threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Task endpoints
# ---------------------------------------------------------------------------

@app.get("/tasks/pending", dependencies=[Depends(_require_auth)])
def get_pending_tasks(
    node_id: Optional[str] = None,
    backends: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Return pending tasks routable to this node.

    Query params:
      node_id  — filters by session affinity (machine_id IS NULL OR machine_id = node_id)
      backends — comma-separated list of backend names the worker supports
      limit    — max rows returned (default 10)
    """
    db = get_db()
    if db is None:
        return []
    backend_list = [b.strip() for b in backends.split(",") if b.strip()] if backends else None
    rows = db.get_pending_tasks(node_id=node_id, backends=backend_list, limit=limit)
    # Deserialise JSON payload column for convenience
    for row in rows:
        if isinstance(row.get("payload"), str):
            try:
                row["payload"] = json.loads(row["payload"])
            except Exception:
                pass
    return rows


@app.post("/tasks/{task_id}/claim", dependencies=[Depends(_require_auth)])
def claim_task(task_id: str, payload: ClaimPayload) -> Dict[str, Any]:
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ok = db.claim_task(task_id, payload.node_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Task already claimed or not pending")
    task = db.get_task(task_id)
    if task and isinstance(task.get("payload"), str):
        try:
            task["payload"] = json.loads(task["payload"])
        except Exception:
            pass
    return {"status": "claimed", "task": task}


@app.post("/tasks/{task_id}/result", dependencies=[Depends(_require_auth)])
def submit_result(task_id: str, payload: ExecutionResultPayload) -> Dict[str, str]:
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    claimed_by = task.get("claimed_by")
    if claimed_by and claimed_by != payload.node_id:
        raise HTTPException(
            status_code=403,
            detail=f"Task {task_id!r} was claimed by {claimed_by!r}, not {payload.node_id!r}",
        )
    result_dict = {
        "success": payload.success,
        "output": payload.output,
        "errors": payload.errors,
        "files_modified": payload.files_modified,
        "execution_time": payload.execution_time,
        "timestamp": payload.timestamp,
        "return_code": payload.return_code,
        "backend_session_id": payload.backend_session_id,
        "error_detail": payload.error_detail,
    }
    session_id = task.get("session_id")
    if payload.success:
        db.complete_task(task_id, result_dict, payload.artifact_path)
        # Append event for the session if present
        if session_id:
            db.append_event(
                session_id=session_id,
                task_id=task_id,
                success=True,
                execution_time=payload.execution_time,
            )
    else:
        error_str = "; ".join(payload.errors) if payload.errors else "worker reported failure"
        db.fail_task(task_id, error_str)
        if session_id:
            db.append_event(
                session_id=session_id,
                task_id=task_id,
                success=False,
                execution_time=payload.execution_time,
                error=error_str,
            )
        # Record the failure in the controller-side event stream too, so the
        # gateway's events.ndjson reflects remote failures (not only the
        # worker's local stream). Correlated by task_id/session_id.
        try:
            from src.core.observability import emit_event
            emit_event(
                "task_failed",
                task_id=task_id,
                session_id=session_id or None,
                node_id=payload.node_id,
                error=payload.errors[0] if payload.errors else error_str,
                error_detail=(payload.error_detail or "")[:4000],
                duration_s=round(payload.execution_time, 3),
            )
        except Exception:
            pass
    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Job endpoints (T3 — Watched Jobs)
# ---------------------------------------------------------------------------


@app.post("/jobs", dependencies=[Depends(_require_auth)])
def register_job(payload: RegisterJobPayload) -> Dict[str, Any]:
    """Register a new watched job. The worker's job watcher loop will pick it
    up, spawn the command (if any), and monitor the process."""
    import uuid
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    db.register_job(
        job_id=job_id,
        node_id=payload.node_id,
        label=payload.label,
        session_id=payload.session_id,
        command=payload.command,
        log_path=payload.log_path,
        notify=payload.notify,
        notify_agent=payload.notify_agent,
    )
    job = db.get_job(job_id)
    return {"status": "registered", "job_id": job_id, "job": job}


@app.post("/jobs/{job_id}/start", dependencies=[Depends(_require_auth)])
def start_job(job_id: str, payload: JobStartPayload) -> Dict[str, str]:
    """Worker records PID/PGID for a spawned job."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if job.get("node_id") != payload.node_id:
        raise HTTPException(
            status_code=403,
            detail=f"Job {job_id!r} is owned by node {job.get('node_id')!r}",
        )
    db.start_job(job_id, payload.pid, payload.pgid, payload.log_path)
    return {"status": "started", "job_id": job_id}


@app.post("/jobs/{job_id}/done", dependencies=[Depends(_require_auth)])
def report_job_done(job_id: str, payload: JobDonePayload) -> Dict[str, str]:
    """Worker reports that a watched job reached terminal state."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if job.get("node_id") != payload.node_id:
        raise HTTPException(
            status_code=403,
            detail=f"Job {job_id!r} is owned by node {job.get('node_id')!r}, not {payload.node_id!r}",
        )
    if payload.exit_code == 0:
        db.complete_job(job_id, payload.exit_code, payload.tail)
    else:
        err = f"exit code {payload.exit_code}"
        if payload.tail:
            err = f"{err}: {payload.tail[:500]}"
        db.fail_job(job_id, err)
    return {"status": "accepted", "job_id": job_id}


@app.get("/jobs", dependencies=[Depends(_require_auth)])
def list_jobs(
    node_id: Optional[str] = None,
    status: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    db = get_db()
    if db is None:
        return []
    return db.list_jobs(node_id=node_id, status=status, session_id=session_id, limit=limit)


@app.get("/jobs/{job_id}", dependencies=[Depends(_require_auth)])
def get_job(job_id: str) -> Dict[str, Any]:
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job
