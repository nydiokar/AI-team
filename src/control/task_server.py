"""
Mesh task server — FastAPI app (VPS-side).

Locally testable without Tailscale or a VPS:
    uvicorn src.control.task_server:app --host 127.0.0.1 --port 9002

All endpoints except /health require:
    Authorization: Bearer {WORKER_TOKEN}

The backing store is MeshDB (src/control/db.py). No SQL lives here.
"""

import asyncio
import json
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, Security, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_STAGING_ROOT = Path(__file__).resolve().parent.parent.parent / "state" / "uploads"

from src.control.app_metrics import RequestTimingMiddleware
from src.control.db import cache_heartbeat_interval_sec, get_db
from src.control.mesh_health import get_mesh_health
from src.control.node_registry import NodeInfo, NodeCapabilities, get_registry
from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import TelemetryEvent

logger = logging.getLogger(__name__)

_SLOW_REQUEST_SECONDS = 1.0
_MESH_HEALTH_SAMPLE_SECONDS = 30.0

# --- Deferred telemetry projection ------------------------------------------
# Raw telemetry events insert synchronously (fast, indexed). The CPU-bound turn
# projection + session-growth refresh is the write-heavy half; it is deferred to
# a background flusher so it never holds the DB write lock inside a worker's
# request (heartbeat/claim/result share that one lock). Dirty ids coalesce, so a
# turn that receives 20 activity events in a second is projected once, not 20x.
_TELEMETRY_FLUSH_INTERVAL_SEC = 1.0
_TELEMETRY_DIRTY_CAP = 20000  # bound memory if the projector stalls (telemetry is droppable)
_WAL_CHECKPOINT_INTERVAL_SEC = 60.0  # bound WAL growth without contending on the hot path
# Fairness: telemetry projection shares mesh.db's single write lock with the
# control plane (heartbeat/claim/result). Under a backlog, projecting everything
# in one shot holds/re-grabs that lock in a tight loop and STARVES control-plane
# requests (observed: heartbeat/jobs/pending all stalling ~2s together). So each
# tick projects a BOUNDED number of turns, in small chunks, pausing between
# chunks to hand the lock back. Telemetry is eventually-consistent and droppable,
# so falling behind is acceptable; a stalled heartbeat is not.
_TELEMETRY_PROJECT_MAX_PER_TICK = 25
_TELEMETRY_PROJECT_CHUNK = 5
_TELEMETRY_PROJECT_CHUNK_PAUSE_SEC = 0.01
_telemetry_dirty_lock = threading.Lock()
_telemetry_dirty_turns: set[str] = set()
_telemetry_dirty_sessions: set[str] = set()


def _enqueue_projection(turn_ids: Iterable[str], session_ids: Iterable[str]) -> None:
    """Mark turns/sessions dirty for the background projection flusher."""
    with _telemetry_dirty_lock:
        if len(_telemetry_dirty_turns) < _TELEMETRY_DIRTY_CAP:
            _telemetry_dirty_turns.update(t for t in turn_ids if t)
        if len(_telemetry_dirty_sessions) < _TELEMETRY_DIRTY_CAP:
            _telemetry_dirty_sessions.update(s for s in session_ids if s)


def _drain_projection(max_turns: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """Remove and return dirty ids. With ``max_turns`` set, drain at most that
    many turns and leave the rest queued for the next tick (fairness); sessions
    are few, so always fully drained."""
    with _telemetry_dirty_lock:
        if max_turns is None or len(_telemetry_dirty_turns) <= max_turns:
            turns = list(_telemetry_dirty_turns)
            _telemetry_dirty_turns.clear()
        else:
            all_turns = list(_telemetry_dirty_turns)
            turns = all_turns[:max_turns]
            _telemetry_dirty_turns.clear()
            _telemetry_dirty_turns.update(all_turns[max_turns:])
        sessions = list(_telemetry_dirty_sessions)
        _telemetry_dirty_sessions.clear()
    return turns, sessions


async def _telemetry_projection_flusher_loop(
    interval_sec: float = _TELEMETRY_FLUSH_INTERVAL_SEC,
) -> None:
    """Coalesce and apply deferred telemetry projections off the request path.

    Doubles as the WAL maintenance tick: a time-gated ``wal_checkpoint`` keeps the
    write-ahead log from growing unbounded under sustained telemetry write volume.
    """
    last_checkpoint = time.monotonic()
    while True:
        try:
            await asyncio.sleep(interval_sec)
            db = get_db()
            if db is None:
                continue
            turns, sessions = _drain_projection(_TELEMETRY_PROJECT_MAX_PER_TICK)
            store = TelemetryStore(db)
            # Project in small chunks, releasing the write lock between each so a
            # control-plane heartbeat/claim can win it. The chunk runs in a worker
            # thread (holds the lock); the pause runs on the loop (lock released).
            for i in range(0, len(turns), _TELEMETRY_PROJECT_CHUNK):
                chunk = turns[i:i + _TELEMETRY_PROJECT_CHUNK]
                await asyncio.to_thread(store.project_dirty, chunk, [], isolate=True)
                await asyncio.sleep(_TELEMETRY_PROJECT_CHUNK_PAUSE_SEC)
            if sessions:
                await asyncio.to_thread(store.project_dirty, [], sessions, isolate=True)
            now = time.monotonic()
            if now - last_checkpoint >= _WAL_CHECKPOINT_INTERVAL_SEC:
                last_checkpoint = now
                await asyncio.to_thread(db.checkpoint_wal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("event=telemetry_projection_flush_failed", exc_info=True)


# Optional hook into the in-process orchestrator, set only when this server runs
# EMBEDDED in the gateway. Lets a worker-reported proactive turn trigger the
# gateway's live notification fan-out (web push / Telegram). When unset (the
# server runs standalone) the turn is still persisted to the DB, so the
# conversation stays correct — only the live "reach back" ping is skipped.
_PROACTIVE_HOOK: Optional[Any] = None


def bind_proactive_hook(hook: Optional[Any]) -> None:
    """Register (or clear) the proactive-turn notification hook. Signature:
    ``hook(session_id, task_id, text, backend_session_id)``. Best-effort."""
    global _PROACTIVE_HOOK
    _PROACTIVE_HOOK = hook


# Backend error phrases that can leak into `output` when a pre-fix worker treats
# an is_error result as a successful reply. A NORMAL reply that merely *mentions*
# these words won't match: we only test a short output that IS essentially the
# phrase (bounded length), never long prose. Kept deliberately conservative to
# avoid downgrading a genuine answer that discusses context windows.
def _looks_like_backend_error(output: Optional[str]) -> str:
    """Return an error_class if `output` looks like a bare backend error string,
    else "". Only fires on short outputs that are essentially just the phrase."""
    text = (output or "").strip()
    if not text or len(text) > 200:
        return ""
    from src.backends.claude_driver import classify_error_text, _CONTEXT_OVERFLOW_MARKERS
    low = text.lower()
    if any(m in low for m in _CONTEXT_OVERFLOW_MARKERS):
        return "context_overflow"
    return ""


def classify_completion_outcome(
    success: bool, output: Optional[str], errors: Optional[List[str]]
) -> "tuple[bool, str]":
    """[A82 Stage 3] Shared completion classification (design §6: "Extract shared
    completion classification rather than duplicating salvage/quota/cache-health/
    native-id rules").

    Returns ``(effective_success, downgraded_error_class)``. The single source of
    truth for the success-downgrade trust boundary: a worker running pre-fix
    driver code can report ``success=True`` while ``output`` is actually a bare
    backend error string. Both the legacy protocol-0 ``submit_result`` and the
    managed protocol-1 result path classify through here, so the two never drift.
    """
    if success and not errors:
        err_class = _looks_like_backend_error(output)
        if err_class:
            return False, err_class
    return success, ""



def _register_local_node() -> None:
    """Register the gateway's OWN host as an online node so its in-process
    self-claims are recognized as LIVE by the stale-claim reaper.

    The orchestrator locks a locally-run task to this process by self-claiming its
    mesh_tasks row under ``socket.gethostname()`` (so no remote daemon can pick it
    up — a double-execution guard). But nothing kept that host's node registration
    alive, so it was perpetually 'offline': any local task running longer than
    ``claim_lease_sec`` (300s) had its self-claim released as ``node_offline`` by the
    reaper, re-exposing the row to a remote daemon. Registering here mints a FRESH
    incarnation per gateway start (``register`` → ``upsert_node``), so self-claims
    orphaned by a *previous* gateway process are reaped via ``incarnation_mismatch``
    (register()'s fast path), while the *current* process's live self-claims are
    kept. Empty ``backends`` ⇒ never a remote-routing target (routing is pin-based
    and a pin to this host always runs locally), so this changes nothing but
    liveness truthfulness.
    """
    import socket
    from config import config as _cfg
    host = socket.gethostname()
    info = NodeInfo(
        node_id=host,
        tailscale_ip="",
        api_port=int(getattr(_cfg.mesh, "dashboard_port", 9003) or 9003),
        capabilities=NodeCapabilities(
            backends=[],
            max_concurrent=int(getattr(_cfg.system, "max_concurrent_tasks", 3) or 3),
        ),
    )
    get_registry().register(info)
    logger.info("event=local_node_registered node_id=%s (gateway self-claim liveness)", host)


async def _local_node_heartbeat_loop() -> None:
    """Keep the gateway's own node heartbeat fresh (< node_heartbeat_timeout_sec)
    so it stays 'online' while the gateway runs. Pure liveness — no live_state is
    published, so the reaper's online path (``_stale_online_claim_reason``) returns
    None and leaves local self-claims to the in-process task lifecycle (timeout /
    cancel), never releasing a live one."""
    import socket
    from config import config as _cfg
    host = socket.gethostname()
    timeout = int(getattr(_cfg.mesh, "node_heartbeat_timeout_sec", 90) or 90)
    interval = max(10, timeout // 3)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                if not await asyncio.to_thread(get_registry().heartbeat, host):
                    await asyncio.to_thread(_register_local_node)  # re-register if the row was dropped
            except Exception as e:
                logger.debug("event=local_node_heartbeat_error err=%s", e)
    except asyncio.CancelledError:
        pass


async def _mesh_health_sampler_loop() -> None:
    """Record aggregate health outside worker liveness request handling."""
    try:
        while True:
            db = get_db()
            if db is not None:
                try:
                    await asyncio.to_thread(
                        db.maybe_record_mesh_health_sample,
                        source="task_server",
                        min_interval_seconds=_MESH_HEALTH_SAMPLE_SECONDS,
                    )
                except Exception:
                    logger.warning("event=mesh_health_sample_failed", exc_info=True)
            await asyncio.sleep(_MESH_HEALTH_SAMPLE_SECONDS)
    except asyncio.CancelledError:
        pass


def _parse_claimed_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _should_release_stale_claim(row: Dict[str, Any], *, max_runtime_sec: int, now: Optional[datetime] = None) -> bool:
    """Whether the stale-claim reaper may requeue this row.

    `missing_from_live_state` is weak evidence: a heartbeat can overwrite a node's
    active task list while the original worker turn is still alive. Releasing it
    at the short claim lease creates duplicate execution. Wait until the bounded
    max-runtime window before requeueing that reason; stronger reasons still
    release immediately after the lease.
    """
    reason = str(row.get("_stale_reason") or "")
    if reason != "missing_from_live_state":
        return True
    max_runtime = max(0, int(max_runtime_sec or 0))
    if max_runtime <= 0:
        return False
    claimed = _parse_claimed_at(row.get("claimed_at"))
    if claimed is None:
        return False
    clock = now or datetime.utcnow()
    if clock.tzinfo is not None:
        clock = clock.astimezone(timezone.utc).replace(tzinfo=None)
    return (clock - claimed).total_seconds() >= max_runtime


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from config import config as _cfg
    get_registry().start()
    # The self-node only keeps in-process local self-claims live; with local
    # execution disabled there are none, and the row leaks as a phantom node.
    local_execution: bool = bool(_cfg.system.local_execution_enabled)
    if local_execution:
        _register_local_node()
    logger.info("event=task_server_started")
    reaper_task = asyncio.create_task(_stale_claim_reaper_loop())
    local_hb_task = asyncio.create_task(_local_node_heartbeat_loop()) if local_execution else None
    health_sampler_task = asyncio.create_task(_mesh_health_sampler_loop())
    projection_task = asyncio.create_task(_telemetry_projection_flusher_loop())
    _bg_tasks = tuple(t for t in (reaper_task, local_hb_task, health_sampler_task, projection_task) if t is not None)
    yield
    for _t in _bg_tasks:
        _t.cancel()
    for _t in _bg_tasks:
        try:
            await _t
        except asyncio.CancelledError:
            pass
    # Final drain so a clean shutdown does not strand pending projections.
    try:
        turns, sessions = _drain_projection()
        if turns or sessions:
            db = get_db()
            if db is not None:
                TelemetryStore(db).project_dirty(turns, sessions, isolate=True)
    except Exception:
        logger.debug("event=telemetry_projection_final_drain_failed", exc_info=True)
    get_registry().stop()


app = FastAPI(title="AI-Team Mesh Task Server", version="1.0", lifespan=_lifespan)
app.add_middleware(RequestTimingMiddleware, component="task_server")
# [A82 Stage 3 rework 4, m2] Streamed byte cap on every managed carrier route
# (chunked bodies included), enforced before the body is parsed.
from src.control.body_cap import BodyCapMiddleware  # noqa: E402

_MANAGED_ROUTE_BODY_CAP = 8 * 1024 * 1024 + 64 * 1024
app.add_middleware(
    BodyCapMiddleware,
    rules=[
        (r"/tasks/[^/]+/(result-managed|quiescence)", _MANAGED_ROUTE_BODY_CAP),
        (r"/tasks/[^/]+/(claim-managed|start-managed|release-managed|enter-recovery)", 16 * 1024),
    ],
)


@app.middleware("http")
async def _log_slow_request(request: Request, call_next):
    """Emit a correlated record when task-server work delays a worker request."""
    started = time.perf_counter()
    request_id = request.headers.get("X-AI-Team-Request-ID", "")
    status_code: Optional[int] = None
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - started
        if elapsed >= _SLOW_REQUEST_SECONDS:
            logger.warning(
                "event=task_server_request_slow method=%s path=%s status=%s elapsed_ms=%.1f request_id=%s",
                request.method,
                request.url.path,
                status_code if status_code is not None else "error",
                elapsed * 1000,
                request_id or "none",
            )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


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
    backends: List[str] = Field(default_factory=list)
    max_concurrent: int = 2
    projects_root: str = ""
    repos: List[Dict[str, str]] = Field(default_factory=list)
    models: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    # [A82 Stage 3] Managed turn-queue protocol versions this carrier supports.
    # Empty/[0] = legacy-only carrier: it MUST NOT be offered protocol-1 (managed)
    # rows (design §7: "Legacy workers must not receive managed rows"; capability
    # negotiation happens BEFORE managed pending rows are visible). A carrier
    # advertises 1 here once it runs the managed claim/result/spool path.
    queue_protocols: List[int] = Field(default_factory=lambda: [0])
    # [A82 Stage 3 rework] Backends on this carrier with a real managed
    # execution path; protocol-1 rows for any other backend are never offered.
    managed_backends: List[str] = Field(default_factory=list)


class NodeRegisterPayload(BaseModel):
    node_id: str
    tailscale_ip: str = ""
    api_port: int = 9001
    incarnation_id: str = ""
    capabilities: _Capabilities = Field(default_factory=_Capabilities)


class LiveStatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    v: int = 1
    active_tasks: List[str] = Field(default_factory=list)
    active_task_details: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    slots_used: int = 0
    slots_total: int = 0
    canary: bool = False
    incarnation_id: str = ""


class HeartbeatPayload(BaseModel):
    node_id: str
    live_state: Optional[LiveStatePayload] = None
    models: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)


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
    driver_type: str = ""
    driver_status: str = ""
    cache_health: str = "unknown"
    cache_unhealthy_count: int = 0
    previous_backend_session_ids: List[str] = Field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    telemetry_invocation_id: str = ""
    error_detail: str = ""  # full traceback when the worker caught an exception (D2)
    inspect: Optional[Dict[str, Any]] = None  # repo inspection op result (action=='inspect')


class TelemetryBatchPayload(BaseModel):
    batch_id: str = Field(min_length=1, max_length=96)
    node_id: str = Field(min_length=1, max_length=128)
    events: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)


class ActivityPayload(BaseModel):
    """A single live ``task_activity`` signal forwarded by a remote worker.

    The gateway re-emits it into its own event stream so the UI pill shows the
    worker's granular state ("Using Bash", "Thinking…") instead of a bare
    "Working…". This is ephemeral live signal only — durable turn/token facts
    still travel via /telemetry/batches, never here.
    """
    node_id: str = Field(min_length=1, max_length=128)
    session_id: Optional[str] = Field(default=None, max_length=128)
    task_id: Optional[str] = Field(default=None, max_length=128)
    turn_id: Optional[str] = Field(default=None, max_length=128)
    label: str = Field(min_length=1, max_length=200)


class QuotaObservationPayload(BaseModel):
    """A worker-originated Claude quota observation.

    The controller container has no Claude binary or OAuth credentials by design,
    so quota telemetry is read harness-side (on the worker) and shipped here as
    the provider's raw ``get_usage`` response plus provenance. The controller
    validates and persists it through the existing quota store/coordinator path,
    so the quota API/UI contract is unchanged. A non-empty ``error`` means the
    worker reached us but its harness read failed — recorded as
    adapter-unavailable, which is DISTINCT from a valid observation with no open
    window (never collapse transport/harness failure into an empty window).
    """
    node_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(default="claude", max_length=32)
    principal_key: str = Field(default="", max_length=200)
    sdk_version: Optional[str] = Field(default=None, max_length=64)
    claude_code_version: Optional[str] = Field(default=None, max_length=128)
    observed_at: Optional[str] = Field(default=None, max_length=64)
    usage: Optional[Dict[str, Any]] = None
    error: str = Field(default="", max_length=200)


# ---------------------------------------------------------------------------
# Job models (T3)
# ---------------------------------------------------------------------------

class RegisterJobPayload(BaseModel):
    node_id: str
    session_id: Optional[str] = None
    label: str
    command: Optional[str] = None
    attach_pid: Optional[int] = None   # attach to an already-running process instead of spawning
    cwd: Optional[str] = None          # working directory for spawn mode
    log_path: Optional[str] = None
    notify: bool = True
    notify_agent: bool = False
    expected_runtime_sec: Optional[int] = Field(default=None, ge=0, le=86400)
    cache_heartbeat: str = Field(default="auto", max_length=8)


class JobDonePayload(BaseModel):
    node_id: str
    exit_code: int
    tail: str = ""
    status: Optional[str] = None


class ProactiveTurnPayload(BaseModel):
    """A turn the agent produced on its own (a run_in_background job finished and
    it kept going), reported by the worker so the gateway can deliver it."""
    node_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    backend: str = Field(default="claude", max_length=64)
    output: str = ""
    backend_session_id: str = Field(default="", max_length=256)
    usage: Optional[Dict[str, Any]] = None
    is_error: bool = False
    error_text: str = Field(default="", max_length=2000)


class JobStartPayload(BaseModel):
    node_id: str
    pid: int
    pgid: int = 0
    log_path: Optional[str] = None
    started_epoch: Optional[float] = None
    observed_command: Optional[str] = None


class JobProbePayload(BaseModel):
    node_id: str
    observed_command: Optional[str] = None
    observed_started_epoch: Optional[float] = None
    probe_error: str = ""


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, Any]:
    db = get_db()
    stats = db.stats() if db else {}
    mesh_health = get_mesh_health()
    return {
        "status": "ok",
        "db": stats,
        "mesh_health": mesh_health.stats(),
    }


@app.post("/telemetry/batches", dependencies=[Depends(_require_auth)])
def submit_telemetry_batch(
    payload: TelemetryBatchPayload, request: Request
) -> Dict[str, Any]:
    """Validate and idempotently persist one gateway/worker telemetry batch."""
    from config import config

    if not config.telemetry.enabled:
        return {
            "batch_id": payload.batch_id,
            "accepted": 0,
            "duplicates": 0,
            "rejected": 0,
            "rejections": [],
            "disabled": True,
        }
    max_bytes = int(config.telemetry.upload_max_bytes)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Telemetry batch too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    encoded_size = len(
        payload.model_dump_json(exclude_none=False).encode("utf-8")
    )
    if encoded_size > max_bytes:
        raise HTTPException(status_code=413, detail="Telemetry batch too large")

    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Telemetry database unavailable")

    valid: List[TelemetryEvent] = []
    rejections: List[Dict[str, Any]] = []
    for index, raw_event in enumerate(payload.events):
        try:
            event = TelemetryEvent.model_validate(raw_event)
        except ValidationError:
            rejections.append({"index": index, "code": "schema_invalid"})
            continue
        if event.node_id != payload.node_id:
            rejections.append({"index": index, "code": "node_id_mismatch"})
            continue
        valid.append(event)

    # Insert raw events synchronously (fast, indexed) but defer the CPU-bound
    # turn projection + session-growth refresh to the background flusher so this
    # request never holds the DB write lock ahead of a worker heartbeat/claim.
    result = TelemetryStore(db).insert_events(valid, rebuild=False)
    session_ids = {e.session_id for e in valid if e.session_id is not None}
    _enqueue_projection(result["turn_ids"], session_ids)
    return {
        "batch_id": payload.batch_id,
        "accepted": result["accepted"],
        "duplicates": result["duplicates"],
        "rejected": len(rejections),
        "rejections": rejections,
    }


@app.post("/events/activity", dependencies=[Depends(_require_auth)])
def submit_activity(payload: ActivityPayload) -> Dict[str, Any]:
    """Re-emit a remote worker's live ``task_activity`` into the gateway stream.

    The gateway owns the SSE feed the UI tails; a remote worker's own
    events.ndjson is never read, so without this hop the pill is stuck on
    "Working…". Requires a session/task scope so stale cross-turn events can be
    filtered client-side, matching the in-process emitter's contract.
    """
    if not payload.session_id and not payload.task_id:
        raise HTTPException(status_code=422, detail="session_id or task_id required")
    from src.core.observability import emit_event

    emit_event(
        "task_activity",
        node_id=payload.node_id,
        session_id=payload.session_id,
        task_id=payload.task_id,
        turn_id=payload.turn_id,
        label=payload.label,
    )
    return {"accepted": True}


@app.post("/telemetry/quota-observation", dependencies=[Depends(_require_auth)])
async def submit_quota_observation(payload: QuotaObservationPayload) -> Dict[str, Any]:
    """Ingest a worker-originated quota observation into the shared quota store.

    Observation happens harness-side (worker); the controller only validates and
    persists — it never spawns Claude. Reuses the existing coordinator
    observe→persist pipeline (with an injected ``read_usage``), so the stored rows
    are identical to the pre-Docker in-process observer, minus the local spawn.
    The gateway process reads the same ``state/quota_windows.db`` for
    ``/api/quota-windows`` (both controller services share ``controller/state``),
    exactly like ``/telemetry/batches`` shares ``mesh.db``.
    """
    from src.services.quota_window_coordinator import ingest_worker_quota_observation

    result = await ingest_worker_quota_observation(
        provider=payload.provider or "claude",
        node_id=payload.node_id,
        principal_key=payload.principal_key or "",
        sdk_version=payload.sdk_version,
        claude_code_version=payload.claude_code_version,
        observed_at=payload.observed_at,
        usage=payload.usage,
        error=payload.error or "",
    )
    return {"accepted": bool(result.get("accepted")), **result}


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
    mesh_load = s.get("mesh_load") or {}
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
            "slots_used": mesh_load.get("slots_used", 0),
            "slots_total": mesh_load.get("slots_total", 0),
            "slots_available": mesh_load.get("slots_available", 0),
            "active_tasks": mesh_load.get("active_tasks", 0),
            "nodes_with_live_state": mesh_load.get("nodes_with_live_state", 0),
            "nodes_without_live_state": mesh_load.get("nodes_without_live_state", 0),
            "stale_live_state_nodes": mesh_load.get("stale_live_state_nodes", []),
            "detail": nodes,
        },
        "sessions": {
            "total": s.get("sessions_total", 0),
            "busy": s.get("sessions_busy", 0),
            "stale_busy": mesh_load.get("stale_busy_sessions", 0),
        },
        "history": {
            "recent": db.list_mesh_health_samples(limit=24) if db else [],
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
            models=dict(payload.capabilities.models),
            queue_protocols=list(payload.capabilities.queue_protocols),
            managed_backends=list(payload.capabilities.managed_backends),
        ),
        incarnation_id=payload.incarnation_id or None,
    )
    get_registry().register(info)
    return {"status": "registered", "node_id": payload.node_id}


@app.post("/nodes/heartbeat", dependencies=[Depends(_require_auth)])
def node_heartbeat(payload: HeartbeatPayload) -> Dict[str, str]:
    live_state = payload.live_state.model_dump() if payload.live_state is not None else None
    ok = get_registry().heartbeat(payload.node_id, live_state=live_state, models=payload.models)
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
    accept_unpinned: bool = True,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Return pending tasks routable to this node.

    Query params:
      node_id  — filters by session affinity (machine_id IS NULL OR machine_id = node_id)
      backends — comma-separated list of backend names the worker supports
      accept_unpinned — when false, only return tasks pinned to node_id
      limit    — max rows returned (default 10)
    """
    db = get_db()
    if db is None:
        return []
    backend_list = [b.strip() for b in backends.split(",") if b.strip()] if backends else None
    rows = db.get_pending_tasks(
        node_id=node_id,
        backends=backend_list,
        accept_unpinned=accept_unpinned,
        limit=limit,
    )
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
    ok = db.claim_task(task_id, payload.node_id)  # protocol-0 only (DB-fenced)
    if not ok:
        raise HTTPException(status_code=409, detail="Task already claimed or not pending")
    task = db.get_task(task_id)
    if task and isinstance(task.get("payload"), str):
        try:
            task["payload"] = json.loads(task["payload"])
        except Exception:
            pass
    if task:
        _strip_managed_secrets(task)
    return {"status": "claimed", "task": task}


def _strip_managed_secrets(row: Dict[str, Any]) -> Dict[str, Any]:
    """[A82 Stage 3 rework] Never return the managed execution credential or
    admission material through a legacy response."""
    for key in ("claim_token", "idempotency_key", "admission_hash"):
        row.pop(key, None)
    return row


def _refuse_if_managed(db: Any, task_id: str) -> Optional[Dict[str, Any]]:
    """[A82 Stage 3 rework, M5] Legacy protocol-0 routes must never mutate a
    protocol-1 row (that would bypass token fencing). Returns the row."""
    task = db.get_task(task_id)
    if task and int(task.get("queue_protocol") or 0) == 1:
        raise HTTPException(
            status_code=409,
            detail="managed (protocol-1) turn: use the managed carrier routes",
        )
    return task


# --------------------------------------------------------------------------- #
# [A82 Stage 3] Managed (protocol-1) carrier protocol — capability-negotiated.
# These extend the EXISTING poll/claim/result infrastructure; they do NOT form a
# parallel worker protocol. Legacy protocol-0 handlers above are unchanged.
# --------------------------------------------------------------------------- #
class ManagedClaimPayload(BaseModel):
    node_id: str
    carrier_kind: str = "gateway_local"
    incarnation_id: Optional[str] = None
    # A carrier MUST advertise it supports the managed protocol to receive a
    # managed claim; a legacy poll/claim never reaches this route.
    queue_protocols: List[int] = Field(default_factory=lambda: [1])


@app.get("/tasks/pending-managed", dependencies=[Depends(_require_auth)])
def get_pending_managed(
    node_id: Optional[str] = None,
    backends: Optional[str] = None,
    accept_unpinned: bool = True,
    limit: int = 10,
    queue_protocols: str = "1",
) -> List[Dict[str, Any]]:
    """Return protocol-1 pending turns to a carrier that negotiated managed
    support (design §6/§7). Capability negotiation happens BEFORE managed rows
    are visible: a caller that does not declare protocol 1 gets an empty list, so
    a legacy carrier can never see or claim a managed turn. The execution
    credential is stripped from this view — the claim response carries it.
    """
    # [A82 Stage 3 rework] Gate on the node's REGISTERED capability, not on a
    # query parameter the caller controls: an unregistered node, a node that
    # did not register protocol 1, or a backend it did not register as managed
    # sees no managed rows. (`queue_protocols` is accepted for compatibility
    # and ignored.)
    managed_backends = _registered_managed_backends(node_id)
    if not managed_backends:
        return []
    db = get_db()
    if db is None:
        return []
    requested = [b.strip() for b in backends.split(",") if b.strip()] if backends else list(managed_backends)
    backend_list = [b for b in requested if b in managed_backends]
    if not backend_list:
        return []
    return db.get_pending_managed_turns(
        node_id=node_id,
        backends=backend_list,
        accept_unpinned=accept_unpinned,
        limit=limit,
    )


def _registered_managed_backends(node_id: Optional[str]) -> List[str]:
    """Backends the node REGISTERED as managed-capable (protocol 1), else []."""
    if not node_id:
        return []
    node = get_registry().get(node_id)
    if node is None or 1 not in set(node.capabilities.queue_protocols or []):
        return []
    return list(node.capabilities.managed_backends or [])


@app.post("/tasks/{task_id}/claim-managed", dependencies=[Depends(_require_auth)])
def claim_managed(task_id: str, payload: ManagedClaimPayload) -> Dict[str, Any]:
    """Claim a managed turn, minting a fresh per-attempt token and returning the
    FROZEN execution payload in the response (design §6). The carrier executes
    THIS response, not the poll snapshot. A lost claim response is resolved by
    re-claiming with the same task + carrier process (the same live claimed
    attempt returns its existing ownership via ``claim_turn`` CAS). The token is
    an execution credential and appears ONLY here — never in poll/list/telemetry.
    """
    from .turn_queue import TurnQueueError

    if 1 not in set(payload.queue_protocols or []):
        raise HTTPException(status_code=409, detail="carrier did not negotiate managed protocol")
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    # [A82 Stage 3 rework] Fail closed on the REGISTERED capability: the node
    # must have registered protocol 1 for this row's backend.
    pre = db.get_task(task_id)
    if pre is not None and (pre.get("backend") or "") not in _registered_managed_backends(payload.node_id):
        raise HTTPException(
            status_code=409,
            detail="node has not registered a managed execution path for this backend",
        )
    try:
        token = db.claim_turn(
            task_id=task_id,
            node_id=payload.node_id,
            carrier_kind=payload.carrier_kind,
            incarnation_id=payload.incarnation_id,
        )
    except TurnQueueError as e:
        raise HTTPException(status_code=getattr(e, "status_code", 409), detail=str(e))
    _hint_turn_scheduler()  # [A82 Stage 4a] waiting count dropped
    # [A82 Stage 3 rework] The carrier executes THIS response, so it must carry
    # the routing fields the executor needs (backend/action) from the committed
    # row — non-secret columns only.
    row = db.get_task(task_id) or {}
    return {
        "status": "claimed",
        "claim_token": str(token),
        # The frozen execution payload the carrier must run (design §5).
        "task": {
            "id": token.task_id,
            "session_id": token.session_id,
            "backend": row.get("backend", ""),
            "action": row.get("action", ""),
            "queue_protocol": 1,
            "claim_token": str(token),
            "status": token.status,
            "payload": token.payload,
        },
    }


class ManagedAttemptPayload(BaseModel):
    """Identifies one managed execution attempt (design §6): the carrier node,
    its process incarnation and the per-attempt claim token."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    incarnation_id: Optional[str] = Field(default=None, max_length=128)
    # Release only: the carrier's write-ahead attestation that the backend was
    # never invoked for this attempt (allows release of a started row).
    backend_not_invoked: bool = False


# [A82 Stage 3 rework, m2] Server-side byte cap for managed carrier bodies: one
# result envelope (8 MiB, design §7) plus bounded framing.
_MANAGED_BODY_MAX_BYTES = 8 * 1024 * 1024 + 64 * 1024


def _guard_managed_body(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            too_big = int(content_length) > _MANAGED_BODY_MAX_BYTES
        except ValueError:
            raise HTTPException(status_code=400, detail={"ok": False, "reason": "invalid_content_length"})
        if too_big:
            raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})


@app.post("/tasks/{task_id}/start-managed", dependencies=[Depends(_require_auth)])
def start_managed(task_id: str, payload: ManagedAttemptPayload) -> Dict[str, Any]:
    """[A82 Stage 3 rework, B2] Conditional managed start (``claimed -> running``)
    for the CURRENT token and carrier incarnation (design §7: "Managed start is a
    new conditional operation"). A repeated start with the same live attempt
    returns the same authorization; a superseded token or a restarted carrier
    incarnation is refused (409) so an old authorization can start nothing."""
    from .turn_queue import TurnQueueError

    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        auth = db.start_turn(
            task_id=task_id,
            claim_token=payload.claim_token,
            incarnation_id=payload.incarnation_id,
        )
    except TurnQueueError as e:
        raise HTTPException(status_code=getattr(e, "status_code", 409), detail=str(e))
    return {"status": auth.status, "task_id": auth.task_id, "started_at": auth.started_at}


@app.post("/tasks/{task_id}/release-managed", dependencies=[Depends(_require_auth)])
def release_managed(task_id: str, payload: ManagedAttemptPayload) -> Dict[str, Any]:
    """[A82 Stage 3 rework] Release a claimed-but-NOT-started managed turn back
    to pending for the current token only (design §7: release before start is
    safe only for the current token). A started/foreign-token row is refused
    (409) — release after start is forbidden without quiescence evidence."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    from .turn_queue import TurnQueueError

    try:
        released = db.release_turn(
            task_id=task_id,
            claim_token=payload.claim_token,
            backend_not_invoked=payload.backend_not_invoked,
            node_id=payload.node_id,
        )
    except TurnQueueError as e:
        raise HTTPException(status_code=getattr(e, "status_code", 503), detail=str(e))
    if not released:
        raise HTTPException(
            status_code=409,
            detail="managed turn not releasable (started, superseded or not claimed)",
        )
    return {"status": "released", "task_id": task_id}


class ManagedRecoveryPayload(ManagedAttemptPayload):
    reason: str = Field(default="", max_length=500)


@app.post("/tasks/{task_id}/enter-recovery", dependencies=[Depends(_require_auth)])
def enter_managed_recovery(task_id: str, payload: ManagedRecoveryPayload) -> Dict[str, Any]:
    """[A82 Stage 3 rework] Carrier reports an UNCERTAIN managed outcome
    (uncorrelated result / managed deadline): move its claimed/running attempt
    to ``recovery_required`` via the Stage-2 ``enter_recovery`` helper. Fenced by
    the current claim token AND the claiming node. The slot stays held until
    resolved with recorded evidence (``/quiescence`` / operator) — this route
    never releases or completes anything."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    from .turn_queue import TurnQueueError

    task = db.get_task(task_id)
    if not task or int(task.get("queue_protocol") or 0) != 1:
        raise HTTPException(status_code=404, detail="no managed turn")
    if not _token_matches(task.get("claim_token"), payload.claim_token) or str(
        task.get("claimed_by") or ""
    ) != payload.node_id:
        raise HTTPException(status_code=409, detail="recovery request from a superseded or foreign attempt")
    if str(task.get("status")) == "recovery_required":
        return {"status": "recovery_required", "task_id": task_id}  # idempotent
    try:
        moved = db.enter_recovery(task_id=task_id, claim_token=payload.claim_token, reason=payload.reason)
    except TurnQueueError as e:
        raise HTTPException(status_code=getattr(e, "status_code", 503), detail=str(e))
    if not moved:
        raise HTTPException(status_code=409, detail="turn not in a recoverable state")
    return {"status": "recovery_required", "task_id": task_id}


class _ManagedResultFields(BaseModel):
    """[A82 Stage 3 rework, m2] The exact result-envelope fields a carrier may
    send (the worker's ``_execute_task`` result dict); anything else is
    rejected (extra="forbid"). Text fields are length-bounded."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    output: str = Field(default="", max_length=8 * 1024 * 1024)
    errors: List[str] = Field(default_factory=list, max_length=200)
    files_modified: List[str] = Field(default_factory=list, max_length=10000)
    execution_time: float = 0.0
    timestamp: str = Field(default="", max_length=64)
    return_code: int = 0
    error_detail: str = Field(default="", max_length=64 * 1024)
    raw_stdout: str = Field(default="", max_length=8 * 1024 * 1024)
    raw_stderr: str = Field(default="", max_length=1024 * 1024)
    error_class: str = Field(default="", max_length=128)
    backend_session_id: Optional[str] = Field(default=None, max_length=256)
    driver_type: str = Field(default="", max_length=64)
    driver_status: str = Field(default="", max_length=64)
    cache_health: str = Field(default="unknown", max_length=64)
    cache_unhealthy_count: int = 0
    previous_backend_session_ids: List[str] = Field(default_factory=list, max_length=1000)
    usage: Optional[Dict[str, Any]] = None
    telemetry_invocation_id: str = Field(default="", max_length=128)
    artifact_path: Optional[str] = Field(default=None, max_length=4096)


class ManagedResultPayload(_ManagedResultFields):
    node_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)


@app.post("/tasks/{task_id}/result-managed", dependencies=[Depends(_require_auth), Depends(_guard_managed_body)])
def submit_managed_result(task_id: str, payload: ManagedResultPayload) -> Dict[str, Any]:
    """Atomic managed (protocol-1) result commit (design §6). Verifies the claim
    token + state and commits the terminal outcome, native session id and active
    identity in ONE transaction via ``complete_turn``; classification goes through
    the SHARED helper so it never drifts from the legacy path. Returns a durable
    receipt that echoes ``task_id`` AND ``claim_token`` so the carrier's result
    spool can prune ONLY on a task/token-matched receipt (never on a bare 2xx or
    timeout — design §6). An identical repeated commit for the current token is
    idempotent; a superseded/foreign token is rejected (409). Body size is
    capped BEFORE validation by the ``_guard_managed_body`` dependency (m2)."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    # Late arrival for a row that is already terminal: return a durable STALE
    # receipt so the carrier can retire its spool obligation (design §6) — but
    # ONLY when the presented token is the row's recorded token. A superseded or
    # foreign token is refused and never echoed back (m2: no unverified echo).
    task = db.get_task(task_id)
    if task and str(task.get("status")) in ("completed", "failed", "failed_node_offline", "cancelled", "withdrawn"):
        if not _token_matches(task.get("claim_token"), payload.claim_token):
            raise HTTPException(status_code=409, detail="stale result presented a superseded or foreign claim token")
        return {"status": "accepted (stale)", "task_id": task_id, "claim_token": payload.claim_token}
    outcome = _commit_managed_result(
        db, task_id, payload.claim_token,
        success=payload.success, output=payload.output, errors=payload.errors,
        backend_session_id=payload.backend_session_id,
        artifact_path=payload.artifact_path,
    )
    return {
        "status": "accepted",
        "task_id": outcome.task_id,
        "claim_token": payload.claim_token,
        "resolved_status": outcome.status,
    }


def _token_matches(recorded: Any, presented: str) -> bool:
    import hmac

    return bool(recorded) and hmac.compare_digest(str(recorded), str(presented or ""))


def _commit_managed_result(
    db: Any,
    task_id: str,
    claim_token: str,
    *,
    success: bool,
    output: str,
    errors: List[str],
    backend_session_id: Optional[str],
    artifact_path: Optional[str] = None,
) -> Any:
    """Classify through the SHARED helper and commit atomically via
    ``complete_turn`` (terminal status + result + native id + active identity in
    one transaction). Used by the managed result route and by quiescence
    reconciliation of a spooled terminal result, so the two never diverge."""
    from .turn_queue import TurnQueueError

    effective_success, downgraded = classify_completion_outcome(success, output, errors)
    result_dict = {
        "success": effective_success,
        "output": output,
        "errors": errors,
        "backend_session_id": backend_session_id,
        "error_detail": downgraded,
    }
    if effective_success:
        status, error = "completed", None
    else:
        status = "failed"
        error = "; ".join(errors) if errors else (
            f"backend error result ({downgraded})" if downgraded else "worker reported failure"
        )
    try:
        completion = db.complete_turn(
            task_id=task_id,
            claim_token=claim_token,
            result=result_dict,
            status=status,
            native_session_id=backend_session_id,
            error=error,
            artifact_path=artifact_path,
        )
    except TurnQueueError as e:
        raise HTTPException(status_code=getattr(e, "status_code", 409), detail=str(e))
    _hint_turn_scheduler()  # [A82 Stage 4a] slot freed → next head may activate
    return completion


def _hint_turn_scheduler() -> None:
    """[A82 Stage 4a] Post-commit hint to the gateway turn scheduler (a no-op
    when none runs in this process). Hints never carry authority."""
    try:
        from .turn_scheduler import notify_turn_queue_changed

        notify_turn_queue_changed()
    except Exception:  # noqa: BLE001 — the 3 s fallback still covers it
        logger.debug("event=turn_scheduler_hint_failed", exc_info=True)


class ManagedTerminalResult(_ManagedResultFields):
    """A durable terminal result presented as recovery evidence (m1): it must be
    a real result envelope — an explicit boolean ``success`` plus its output —
    not merely any non-null value."""

    success: bool


class QuiescenceObservationPayload(BaseModel):
    """An authenticated carrier observation of backend quiescence for a held
    (recovery_required) managed turn (design §6). Bound to the execution attempt:
    node/process/native identity + a terminal/stop signal, or a durable terminal
    result. Node registration/offline status is NOT quiescence — the validator
    rejects a bare boolean/offline label."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    quiescent: bool = False
    terminal: bool = False
    terminal_status: Optional[str] = Field(default=None, max_length=32)
    native_session_id: Optional[str] = Field(default=None, max_length=256)
    # [A82 Stage 3 rework] How the carrier knows the backend is stopped:
    #   backend_terminal  — native execution identity + terminal observation;
    #   backend_quiescent — the SAME live carrier process (observer incarnation ==
    #                       claim incarnation) observed its backend quiescent;
    #   carrier_restarted — a NEW carrier process (registered incarnation) that
    #                       reaped the old incarnation's backend children at boot.
    stop_evidence: Optional[str] = Field(default=None, max_length=32)
    observer_incarnation: Optional[str] = Field(default=None, max_length=128)
    # carrier_restarted only: {"pid": int, "observed": "absent"|"pid_reused"|"rebooted"}
    # — the carrier's proof that the attempt's recorded backend process is gone.
    process_proof: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None


def _registered_incarnation(db: Any, node_id: str) -> Optional[str]:
    node = get_registry().get(node_id)
    if node is not None and node.incarnation_id:
        return node.incarnation_id
    row = db.get_node(node_id) if hasattr(db, "get_node") else None
    return (row or {}).get("incarnation_id")


@app.post("/tasks/{task_id}/quiescence", dependencies=[Depends(_require_auth), Depends(_guard_managed_body)])
def record_quiescence_observation(
    task_id: str, payload: QuiescenceObservationPayload
) -> Dict[str, Any]:
    """Record a carrier quiescence observation for a held managed turn and
    auto-reconcile it (design §6). If the observation carries a durable terminal
    ``result``, the turn is reconciled to that outcome; otherwise a valid
    quiescent+terminal observation resolves the recovery hold to the observed
    terminal status. An insufficient observation (bare boolean / offline) is
    refused (409) — the hold is retained with missing evidence. This is the
    RECORDED evidence the operator resolve-recovery endpoint (Stage 6) consumes;
    the carrier cannot supply a bare boolean."""
    from .turn_queue import TurnQueueError

    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    task = db.get_task(task_id)
    if not task or int(task.get("queue_protocol") or 0) != 1:
        raise HTTPException(status_code=404, detail="no managed turn")
    # [A82 Stage 3 rework, m1] Evidence must be bound to THIS attempt: the
    # recorded token and the carrier node that holds the claim.
    if not _token_matches(task.get("claim_token"), payload.claim_token):
        raise HTTPException(status_code=409, detail="quiescence observation for a superseded or foreign attempt")
    if str(task.get("claimed_by") or "") != payload.node_id:
        raise HTTPException(status_code=409, detail="quiescence observation from a carrier that does not hold the claim")
    if payload.result is not None:
        # A durable spooled terminal result: validate it is a real result
        # envelope, then reconcile it through the SAME atomic completion as the
        # managed result route (result + native id + active identity).
        try:
            res = ManagedTerminalResult.model_validate(payload.result)
        except ValidationError:
            raise HTTPException(status_code=422, detail="result evidence is not a terminal result envelope")
        outcome = _commit_managed_result(
            db, task_id, payload.claim_token,
            success=res.success, output=res.output, errors=res.errors,
            backend_session_id=res.backend_session_id or payload.native_session_id,
            artifact_path=res.artifact_path,
        )
        return {"status": "reconciled", "task_id": outcome.task_id, "resolved_status": outcome.status}
    # No result: a quiescence observation must carry an explicit terminal/stop
    # observation bound to a verifiable carrier identity. Without a result the
    # outcome is not success — only failed/cancelled may be recorded.
    if not (payload.quiescent and payload.terminal):
        raise HTTPException(status_code=409, detail="insufficient quiescence evidence (requires quiescent + terminal)")
    kind = payload.stop_evidence or "backend_terminal"
    registered = _registered_incarnation(db, payload.node_id)
    claim_inc = task.get("claim_incarnation")
    if kind == "backend_terminal":
        ok = bool((payload.native_session_id or "").strip())
    elif kind == "backend_quiescent":
        ok = bool(payload.observer_incarnation) and payload.observer_incarnation == claim_inc == registered
    elif kind == "carrier_restarted":
        proof = payload.process_proof or {}
        ok = (
            bool(payload.observer_incarnation)
            and payload.observer_incarnation == registered
            and payload.observer_incarnation != claim_inc
            and isinstance(proof.get("pid"), int)
            and proof.get("observed") in ("absent", "pid_reused", "rebooted")
        )
    else:
        ok = False
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="insufficient quiescence evidence (unverifiable stop evidence / carrier identity)",
        )
    if payload.terminal_status not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail="terminal_status must be failed or cancelled without a durable result",
        )
    evidence: Dict[str, Any] = {
        "task_id": task_id,
        "claim_token": payload.claim_token,
        "node_id": payload.node_id,
        "quiescent": True,
        "terminal": True,
        "terminal_status": payload.terminal_status,
        "native_session_id": payload.native_session_id,
        "stop_evidence": kind,
        "observer_incarnation": payload.observer_incarnation,
        "process_proof": payload.process_proof,
        "source": "carrier",
    }
    try:
        outcome = db.resolve_recovery(
            task_id=task_id,
            claim_token=payload.claim_token,
            quiescence_evidence=evidence,
            resolved_status=payload.terminal_status,
        )
    except TurnQueueError as e:
        raise HTTPException(status_code=getattr(e, "status_code", 409), detail=str(e))
    _hint_turn_scheduler()
    return {"status": "reconciled", "task_id": outcome.task_id, "resolved_status": outcome.resolved_status}


@app.post("/tasks/{task_id}/release", dependencies=[Depends(_require_auth)])
def release_task(task_id: str, payload: ClaimPayload) -> Dict[str, str]:
    """Release a claimed task back to pending (worker graceful shutdown).

    Only the claiming worker can release its own claim. The stale-claim reaper
    handles hard-killed workers that don't call this endpoint.
    """
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    _refuse_if_managed(db, task_id)
    ok = db.release_task(task_id, payload.node_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Task not claimed by this node or not in claimed state")
    return {"status": "released", "task_id": task_id}


def _reconcile_result_telemetry(task_id: str) -> None:
    """Reconcile telemetry after a worker has received its result acknowledgement."""
    db = get_db()
    if db is None:
        return
    try:
        TelemetryStore(db).reconcile(turn_id=task_id, since_hours=0)
    except Exception:
        logger.debug("event=telemetry_reconcile_after_result_failed task_id=%s", task_id, exc_info=True)


@app.post("/tasks/{task_id}/result", dependencies=[Depends(_require_auth)])
def submit_result(
    task_id: str,
    payload: ExecutionResultPayload,
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]  # FastAPI injects by type; None lets direct callers run reconcile inline
) -> Dict[str, str]:
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    task = _refuse_if_managed(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    # Terminal check first: if the task is already done, accept any late
    # result as stale regardless of who sends it. This is essential for
    # workers that were superseded by the reaper or another worker — they
    # must get "accepted (stale)" instead of 403.
    task_status = task.get("status")
    if task_status in ("completed", "failed", "failed_node_offline"):
        logger.debug(
            "event=submit_result_stale task_id=%s status=%s node=%s — ignoring late result",
            task_id, task_status, payload.node_id,
        )
        return {"status": "accepted (stale)", "task_id": task_id}

    claimed_by = task.get("claimed_by")
    if claimed_by and claimed_by != payload.node_id:
        raise HTTPException(
            status_code=403,
            detail=f"Task {task_id!r} was claimed by {claimed_by!r}, not {payload.node_id!r}",
        )
    # Trust boundary (defense in depth): a worker running pre-fix driver code can
    # report success=True while `output` is actually a backend error string (the
    # "Prompt is too long" bug — an is_error ResultMessage stored as a reply).
    # Independently re-validate here so the gateway never persists such a turn as
    # completed even before the worker is redeployed.
    # [A82 Stage 3] Classify through the shared helper so legacy and managed
    # paths apply the identical success-downgrade rule.
    effective_success, downgraded_error_class = classify_completion_outcome(
        payload.success, payload.output, payload.errors
    )
    if downgraded_error_class:
        logger.warning(
            "event=submit_result_downgraded task_id=%s node=%s error_class=%s "
            "— worker reported success but output is a backend error string "
            "(likely pre-fix driver); recording as failure",
            task_id, payload.node_id, downgraded_error_class,
        )

    result_dict = {
        "success": effective_success,
        "output": payload.output,
        "errors": payload.errors,
        "files_modified": payload.files_modified,
        "execution_time": payload.execution_time,
        "timestamp": payload.timestamp,
        "return_code": payload.return_code,
        "backend_session_id": payload.backend_session_id,
        "driver_type": payload.driver_type,
        "driver_status": payload.driver_status,
        "cache_health": payload.cache_health,
        "cache_unhealthy_count": payload.cache_unhealthy_count,
        "previous_backend_session_ids": payload.previous_backend_session_ids,
        "usage": payload.usage,
        "telemetry_invocation_id": payload.telemetry_invocation_id,
        "error_detail": payload.error_detail or downgraded_error_class,
        "inspect": payload.inspect,
    }
    session_id = task.get("session_id")
    # A close_session control task carries a session_id for pinning, but it is
    # not a conversational turn — the session is already CLOSED. Skip the turn
    # event so it doesn't render a phantom "turn" against a closed session.
    is_control_action = task.get("action") in ("close_session", "cancel_codex")
    if effective_success:
        db.complete_task(task_id, result_dict, payload.artifact_path)
        # Append event for the session if present
        if session_id and not is_control_action:
            db.append_event(
                session_id=session_id,
                task_id=task_id,
                success=True,
                execution_time=payload.execution_time,
            )
    else:
        if payload.errors:
            error_str = "; ".join(payload.errors)
        elif downgraded_error_class:
            error_str = f"backend error result ({downgraded_error_class})"
        else:
            error_str = "worker reported failure"
        db.fail_task(task_id, error_str, result=result_dict, artifact_path=payload.artifact_path)
        if session_id and not is_control_action:
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
    if payload.usage is not None:
        db.enrich_task(task_id, usage=payload.usage)
    if background_tasks is None:
        _reconcile_result_telemetry(task_id)
    else:
        background_tasks.add_task(_reconcile_result_telemetry, task_id)
    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Stale-claim reaper (T4) — runs as a background coroutine during the
# task server's lifetime.
# ---------------------------------------------------------------------------

def _reap_stale_claims_once() -> None:
    """Synchronously sweep stale claims; callers must keep this off the event loop.

    A task claim is stale when claimed_at is older than `lease_sec` AND one of:
    - the claiming node is offline or gone (original condition), OR
    - the claiming node is online but its incarnation_id changed since the
      claim was made — meaning the worker was hard-restarted in-place (e.g.
      pm2 restart) and the old process's claim will never complete.

    The fast path for restart-in-place is NodeRegistry.register(), which
    releases orphaned claims immediately on re-registration. This reaper is
    the safety net for cases where the fast path was missed (e.g. gateway
    restart between worker death and re-registration).
    """
    db = get_db()
    if db is None:
        return
    from config import config as _cfg
    lease_sec = getattr(_cfg.mesh, "claim_lease_sec", 300)
    max_runtime_sec = int(getattr(_cfg.mesh, "claim_max_runtime_sec", 1800) or 0)
    if max_runtime_sec <= 0:
        max_runtime_sec = int(getattr(_cfg.system, "task_timeout", 0) or 1800)
    stale = db.list_stale_claims(
        lease_sec=lease_sec,
        live_state_max_age_sec=getattr(_cfg.mesh, "routing_live_state_max_age_sec", 90),
        active_task_max_runtime_sec=max_runtime_sec,
    )
    for row in stale:
        task_id = row.get("id", "?")
        claimed_by = row.get("claimed_by", "?")
        claimed_at = row.get("claimed_at", "?")
        reason = row.get("_stale_reason", "unknown")
        if reason == "active_task_over_max_runtime":
            db.fail_task(
                task_id,
                f"remote task exceeded mesh max runtime while still active on {claimed_by}",
                status="failed",
            )
            logger.warning(
                "event=stale_claim_failed task_id=%s claimed_by=%s claimed_at=%s reason=%s",
                task_id, claimed_by, claimed_at, reason,
            )
        elif not _should_release_stale_claim(row, max_runtime_sec=max_runtime_sec):
            logger.warning(
                "event=stale_claim_release_deferred task_id=%s claimed_by=%s claimed_at=%s reason=%s max_runtime_sec=%s",
                task_id, claimed_by, claimed_at, reason, max_runtime_sec,
            )
        else:
            db.release_task(task_id, claimed_by)
            logger.info(
                "event=stale_claim_released task_id=%s claimed_by=%s claimed_at=%s reason=%s",
                task_id, claimed_by, claimed_at, reason,
            )


async def _stale_claim_reaper_loop(interval_sec: int = 30) -> None:
    """Periodically sweep stale claims without blocking gateway request handling."""
    logger.info("event=stale_claim_reaper_started interval=%ds", interval_sec)
    try:
        while True:
            try:
                await asyncio.to_thread(_reap_stale_claims_once)
            except Exception as e:
                logger.debug("event=stale_claim_reaper_error err=%s", e)
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        logger.info("event=stale_claim_reaper_stopped")


# ---------------------------------------------------------------------------
# File staging endpoints — server holds files briefly so remote workers can pull
# ---------------------------------------------------------------------------

@app.post("/files", dependencies=[Depends(_require_auth)])
async def stage_file(file: UploadFile = File(...)) -> Dict[str, str]:
    """Accept a file upload and park it in a staging slot.

    Returns {file_id, filename}. The remote worker fetches it via GET /files/{file_id}
    and deletes it via DELETE /files/{file_id} once saved locally.
    """
    import re as _re
    file_id = uuid.uuid4().hex[:16]
    dest_dir = _STAGING_ROOT / file_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    # The client-supplied name becomes a path segment; a raw name with "../" escaped
    # the staging root (security review P0-1). Mirror the control-API upload policy:
    # sanitize to a safe charset, then enforce containment via resolve()/relative_to().
    safe_name = _re.sub(r"[^\w.\-]", "_", file.filename or "upload")[:200] or "upload"
    if not safe_name.strip("._"):
        safe_name = "upload"
    dest = (dest_dir / safe_name).resolve()
    try:
        dest.relative_to(dest_dir.resolve())
    except ValueError:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="dangerous_filename")
    content = await file.read()
    dest.write_bytes(content)
    logger.info("event=file_staged file_id=%s filename=%s size=%d", file_id, safe_name, len(content))
    return {"file_id": file_id, "filename": safe_name}


@app.get("/files/{file_id}", dependencies=[Depends(_require_auth)])
def get_staged_file(file_id: str) -> FileResponse:
    staging = _STAGING_ROOT / file_id
    if not staging.exists():
        raise HTTPException(status_code=404, detail="Staged file not found")
    files = [f for f in staging.iterdir() if f.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="Staged file not found")
    f = files[0]
    return FileResponse(str(f), filename=f.name, media_type="application/octet-stream")


@app.delete("/files/{file_id}", dependencies=[Depends(_require_auth)])
def delete_staged_file(file_id: str) -> Dict[str, str]:
    staging = _STAGING_ROOT / file_id
    if staging.exists():
        shutil.rmtree(staging)
        logger.info("event=staged_file_deleted file_id=%s", file_id)
    return {"status": "deleted", "file_id": file_id}


# ---------------------------------------------------------------------------
# Job endpoints (T3 — Watched Jobs)
# ---------------------------------------------------------------------------


@app.post("/jobs", dependencies=[Depends(_require_auth)])
def register_job(payload: RegisterJobPayload) -> Dict[str, Any]:
    """Register a new watched job.

    Two modes:
    - command: worker spawns the command detached and monitors it.
    - attach_pid: job is already running; worker monitors the existing PID.
    """
    import uuid
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    hb_mode = str(payload.cache_heartbeat or "auto").strip().lower()
    if hb_mode not in ("auto", "on", "off"):
        raise HTTPException(status_code=422, detail={"ok": False, "reason": "invalid_cache_heartbeat"})
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    db.register_job(
        job_id=job_id,
        node_id=payload.node_id,
        label=payload.label,
        session_id=payload.session_id,
        command=payload.command,
        cwd=payload.cwd,
        log_path=payload.log_path,
        notify=payload.notify,
        notify_agent=payload.notify_agent,
    )
    runtime_sec = payload.expected_runtime_sec
    auto_below_interval = (
        hb_mode == "auto"
        and runtime_sec is not None
        and runtime_sec < cache_heartbeat_interval_sec()
    )
    if (
        payload.session_id
        and hb_mode != "off"
        and (payload.notify_agent or hb_mode == "on")
        and not auto_below_interval
    ):
        db.ensure_cache_heartbeat_owner(
            payload.session_id,
            reason="watched_job",
            owner_type="job",
            owner_id=job_id,
            expected_runtime_sec=runtime_sec,
        )
    if payload.attach_pid is not None:
        # Record the PID immediately so the worker watcher monitors it without spawning.
        db.start_job(job_id, pid=payload.attach_pid, pgid=0, log_path=payload.log_path)
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
    db.start_job(
        job_id,
        payload.pid,
        payload.pgid,
        payload.log_path,
        started_epoch=payload.started_epoch,
        observed_command=payload.observed_command,
    )
    return {"status": "started", "job_id": job_id}


@app.post("/jobs/{job_id}/probe", dependencies=[Depends(_require_auth)])
def record_job_probe(job_id: str, payload: JobProbePayload) -> Dict[str, str]:
    """Worker records its latest liveness/identity probe for a running job."""
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
    db.record_job_probe(
        job_id,
        observed_command=payload.observed_command,
        observed_started_epoch=payload.observed_started_epoch,
        probe_error=payload.probe_error,
    )
    return {"status": "recorded", "job_id": job_id}


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
    if payload.status == "lost":
        db.fail_job(
            job_id,
            payload.tail or "watched job process identity could not be verified",
            status="lost",
        )
    elif payload.exit_code == 0:
        db.complete_job(job_id, payload.exit_code, payload.tail)
    else:
        err = f"exit code {payload.exit_code}"
        if payload.tail:
            err = f"{err}: {payload.tail[:500]}"
        db.fail_job(job_id, err)

    return {"status": "accepted", "job_id": job_id}


@app.post("/sessions/{session_id}/proactive-turn", dependencies=[Depends(_require_auth)])
def report_proactive_turn(session_id: str, payload: ProactiveTurnPayload) -> Dict[str, str]:
    """A worker reports an autonomous turn — the live SDK session continued on
    its own after a run_in_background job finished. Persist it as a first-class
    conversation turn and (if embedded) trigger the gateway's notification
    fan-out so the user is actively reached, not just updated on next load."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    # [A74/P2-6] Ownership: a session pinned to a node may only receive proactive
    # turns from that node. Unpinned sessions (no machine_id) stay open to any
    # worker — a legit worker may execute any unpinned task, so nothing legit
    # breaks. The full identity fix rides on A71 per-node credentials.
    pinned = session.get("machine_id")
    if pinned and payload.node_id != pinned:
        raise HTTPException(
            status_code=403,
            detail=f"Session {session_id!r} is pinned to node {pinned!r}, not {payload.node_id!r}",
        )

    text = (payload.output or "").strip()
    if not text:
        # An autonomous turn that produced only tool activity / no user-facing
        # text — nothing to deliver. Acknowledge without creating an empty turn.
        return {"status": "empty", "session_id": session_id}

    task_id = f"proactive_{uuid.uuid4().hex[:12]}"
    machine_id = session.get("machine_id") or payload.node_id
    db.record_proactive_turn(
        task_id=task_id,
        session_id=session_id,
        backend=payload.backend or session.get("backend") or "claude",
        machine_id=machine_id,
        reply_text=text,
        usage=payload.usage,
    )
    logger.info(
        "event=proactive_turn_recorded session_id=%s task_id=%s node=%s chars=%d",
        session_id, task_id, payload.node_id, len(text),
    )

    hook = _PROACTIVE_HOOK
    if hook is not None:
        try:
            hook(session_id, task_id, text, payload.backend_session_id)
        except Exception:
            logger.warning(
                "event=proactive_hook_failed session_id=%s task_id=%s",
                session_id, task_id, exc_info=True,
            )
    return {"status": "accepted", "session_id": session_id, "task_id": task_id}


@app.get("/jobs", dependencies=[Depends(_require_auth)])
def list_jobs(
    node_id: Optional[str] = None,
    status: Optional[str] = None,
    session_id: Optional[str] = None,
    ownership: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    if ownership not in (None, "all", "unowned"):
        raise HTTPException(status_code=400, detail="invalid_ownership")
    ownership_filter = None if ownership in (None, "all") else ownership
    if session_id and ownership_filter == "unowned":
        raise HTTPException(status_code=400, detail="session_id_conflicts_with_unowned")
    db = get_db()
    if db is None:
        return []
    return db.list_jobs(
        node_id=node_id,
        status=status,
        session_id=session_id,
        ownership=ownership_filter,
        limit=limit,
    )


@app.get("/jobs/{job_id}", dependencies=[Depends(_require_auth)])
def get_job(job_id: str) -> Dict[str, Any]:
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job
