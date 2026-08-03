#!/usr/bin/env python3
"""
MCP server — ai-team Manager tool surface (M3 Phase 3.0, F4 spike).

Exposes the two minimal tools a gateway-spawned *Manager* session needs to
orchestrate a *worker* session, per docs/M3_MANAGER_INVOCATION_SPEC.md §2.2 / §6:

  * dispatch_worker(objective, ...)  -> POST /api/instructions  (start a worker task)
  * wait_for_worker(task_id|flow_run_id, ...) -> long-poll GET /api/flows/{id}
        until the worker's flow reaches a terminal / attention status.

Modeled EXACTLY on scripts/mcp_jobs.py (stdio JSON-RPC MCP server; loads the
project .env; bearer-token urllib to the gateway). The one difference: these tools
talk to the CONTROL API (default 127.0.0.1:9003, DASHBOARD_TOKEN) — NOT the :9002
task server that mcp_jobs uses (WORKER_TOKEN).

Design invariants (why this is safe for the F4 spike):
  * wait_for_worker is a pure read-only long-poll from THIS subprocess. It never
    holds a worker task slot, so a Manager waiting on a child cannot starve the
    slot the child needs (the §6 anti-starvation criterion — verify live in 3.0).
  * dispatch_worker only wraps the existing, auth-guarded, Level-3-gated
    POST /api/instructions. It introduces no new dispatch path.

LINEAGE (A32): POST /api/instructions now accepts an optional parent_flow_run_id.
   dispatch_worker forwards it so a child dispatched here records a parent edge in
   /api/flows (the §6 "child flow visible ... with lineage" clause). This is a
   SHADOW record wired through the M2 substrate: it is persisted only when the
   gateway runs with HARNESS_FLOW_DRIVE ON (it is, in the live env); with the flag
   OFF the server no-ops the stamp and the field is silently ignored. Either way
   nothing reads the edge to drive execution — so a Manager should confirm lineage
   via /api/flows rather than assume it, and always review the child's committed
   git diff (never a self-reported summary).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Bootstrap: load the project .env the same way the worker / mcp_jobs does
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Load project .env into os.environ before anything else runs."""
    project_root = Path(__file__).resolve().parent.parent
    # Ensure the repo root is importable REGARDLESS of the launching interpreter or
    # cwd. This MCP server is spawned by the session driver, which may use a bare
    # interpreter with no editable install (`.pth`) and a cwd outside the repo — in
    # that case the lazy `from config.models import ...` in dispatch_worker raises
    # `No module named 'config'` and breaks every new-worker dispatch. Put the repo
    # root on sys.path up front so `config`/`src` resolve from source unconditionally.
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    ai_team_env = os.environ.get("AI_TEAM_ENV_FILE", "")
    env_path = Path(ai_team_env) if ai_team_env else (project_root / ".env")

    if not env_path.exists():
        print(f"[mcp_manager] WARNING: .env not found at {env_path}", file=sys.stderr, flush=True)
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


_bootstrap()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _base_url() -> str:
    """Control API base URL.

    Resolution order (no per-node secrets, no hand-pasted host):
      1. DASHBOARD_URL — explicit override, wins outright.
      2. CONTROLLER_URL host + DASHBOARD_PORT — reuse the mesh host this node
         already reaches (the same tailnet controller mcp_jobs.py talks to). The
         control API rides on the same gateway host as the task server, just on a
         different port, so a node already in the mesh needs zero extra config.
      3. 127.0.0.1:DASHBOARD_PORT — local gateway fallback.
    """
    explicit = os.environ.get("DASHBOARD_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("DASHBOARD_PORT", "9003").strip() or "9003"
    controller = os.environ.get("CONTROLLER_URL", "").strip()
    if controller:
        host = urllib.parse.urlsplit(controller).hostname
        if host:
            return f"http://{host}:{port}"
    return f"http://127.0.0.1:{port}"


def _token() -> str:
    """Control API bearer token — DASHBOARD_TOKEN, falling back to WORKER_TOKEN
    (mirrors control_api._dashboard_token())."""
    return os.environ.get("DASHBOARD_TOKEN", "") or os.environ.get("WORKER_TOKEN", "")


def _token_candidates() -> List[str]:
    """Ordered, de-duplicated bearer credentials to try against the control API.

    A Manager on a mesh node may hold EITHER the gateway-local DASHBOARD_TOKEN or
    only the shared mesh WORKER_TOKEN. We can't know which one the gateway will
    accept from here, so we try DASHBOARD_TOKEN first (the local/gateway happy path)
    and fall back to WORKER_TOKEN on a 401. The control API accepts both mesh-internal
    secrets, so whichever this node actually shares with the gateway authenticates —
    no token ever has to be copied between nodes."""
    out: List[str] = []
    for tok in (os.environ.get("DASHBOARD_TOKEN", ""), os.environ.get("WORKER_TOKEN", "")):
        if tok and tok not in out:
            out.append(tok)
    return out


# Terminal vocab mirrors src/control/work_read_model.py so we agree with the
# server on what "done" / "needs attention" means (kept in sync deliberately).
_DONE_STATUSES = {"closed", "superseded", "done", "complete", "completed",
                  "failed", "error", "cancelled", "canceled"}
_ATTENTION_STATUSES = {"blocked", "rework", "rework_requested", "needs_decision",
                       "awaiting_operator", "awaiting_approval", "review",
                       "in_review", "review_requested"}

_MAX_OBJECTIVE_CHARS = 8000
_MAX_PATH_CHARS = 1000
_MAX_ID_CHARS = 128
_MAX_FILES = 100
_MAX_FILE_CHARS = 1000

# wait_for_worker is an IN-TURN BLOCKING poll: while it runs the Manager session is
# BUSY and can neither react to other finished workers nor talk to the operator. So
# the ceiling is deliberately short — a wait that reaches it RETURNS CONTROL (with a
# "re-call or inspect the timeline" note) instead of hanging the whole session. A
# 3600s ceiling once left a Manager blocked for an hour on a worker that had already
# finished; 600s converts that into "block ≤10min, then hand control back". The
# durable, event-driven replacement (wake-on-completion, no blocking) is M3.4 — see
# docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md.
_WAIT_TIMEOUT_DEFAULT = 180.0
_WAIT_TIMEOUT_MAX = 600.0
_POLL_INTERVAL_DEFAULT = 3.0
_POLL_INTERVAL_MIN = 1.0

# [A33] Transient-blip tolerance: a long wait must not abort on a single gateway
# hiccup. Tolerate this many CONSECUTIVE poll failures (a clean poll resets the
# streak) before giving up. Still bounded by the overall deadline.
_MAX_CONSECUTIVE_POLL_ERRORS = 5

# ---------------------------------------------------------------------------
# HTTP  (single choke point — tests monkeypatch this)
# ---------------------------------------------------------------------------

def _api_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: float = 20.0) -> Dict[str, Any]:
    """One bearer-authenticated JSON request to the control API.

    Raises RuntimeError with a clean message on any failure (never leaks a bare
    urllib traceback into the MCP reply).

    On a 401 (Invalid token) it retries once with the alternate configured token, so
    a node that holds only the shared mesh WORKER_TOKEN (and not the gateway-local
    DASHBOARD_TOKEN) still authenticates. See _token_candidates()."""
    tokens = _token_candidates()
    if not tokens:
        raise RuntimeError("DASHBOARD_TOKEN/WORKER_TOKEN not set — cannot reach control API")
    url = f"{_base_url()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    last_auth_error: Optional[str] = None
    for token in tokens:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 401 and token is not tokens[-1]:
                # Try the next candidate credential before giving up.
                last_auth_error = f"HTTP {e.code} on {method} {path}: {detail}"
                continue
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail}") from e
        except Exception as e:
            raise RuntimeError(f"Could not reach control API at {url}: {e}") from e
    # All candidates returned 401.
    raise RuntimeError(last_auth_error or f"HTTP 401 on {method} {path}")


# ---------------------------------------------------------------------------
# Validation helpers (pure — unit-tested directly)
# ---------------------------------------------------------------------------

def _bounded_text(value: Any, name: str, max_chars: int, *, required: bool = True) -> Optional[str]:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{name} cannot be empty")
    if len(text) > max_chars:
        raise ValueError(f"{name} is too long (max {max_chars} characters)")
    return text or None


def _bounded_files(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("files must be a list of strings")
    if len(value) > _MAX_FILES:
        raise ValueError(f"files has too many entries (max {_MAX_FILES})")
    out: List[str] = []
    for item in value:
        f = _bounded_text(item, "files entry", _MAX_FILE_CHARS)
        if f:
            out.append(f)
    return out or None


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def classify_status(status: Optional[str]) -> str:
    """Map a raw flow status to one of: done | attention | active | unknown.

    Kept in sync with work_read_model's status sets."""
    s = (status or "").strip().lower()
    if not s:
        return "unknown"
    if s in _DONE_STATUSES:
        return "done"
    if s in _ATTENTION_STATUSES:
        return "attention"
    return "active"


# ---------------------------------------------------------------------------
# Tool: dispatch_worker
# ---------------------------------------------------------------------------

def _dispatch_worker(args: Dict[str, Any]) -> str:
    objective = _bounded_text(args.get("objective"), "objective", _MAX_OBJECTIVE_CHARS)
    session_id = _bounded_text(args.get("session_id"), "session_id", _MAX_ID_CHARS, required=False)
    cwd = _bounded_text(args.get("cwd"), "cwd", _MAX_PATH_CHARS, required=False)
    files = _bounded_files(args.get("files"))
    parent_flow_run_id = _bounded_text(
        args.get("parent_flow_run_id"), "parent_flow_run_id", _MAX_ID_CHARS, required=False)
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=False)
    node_id = _bounded_text(args.get("node_id"), "node_id", _MAX_ID_CHARS, required=False)
    backend = _bounded_text(args.get("backend"), "backend", _MAX_ID_CHARS, required=False) or "claude"
    # [Worker role] Explicit, opt-in role signal. When 'worker', the NEW observable
    # worker session below is stamped with role_boot='worker' so its driver boots
    # the Worker role (worker.md + worker tools). ABSENT ⇒ a legacy tier-0 worker
    # (byte-identical). This is DISTINCT from the case_id JOIN, which only sets the
    # membership marker case_role='worker' — that alone never promotes a worker.
    role = _bounded_text(args.get("role"), "role", _MAX_ID_CHARS, required=False)
    # [Cockpit] Per-worker model tiering. The Manager must deliberately choose the
    # boot model for every NEW worker; omission would silently resolve to the costly
    # Claude default. `model` reaches the new worker session through
    # CreateSessionBody.model at the create seam below. It CANNOT retro-set the
    # model of a reused session_id because the SDK client is cached at boot.
    model = _bounded_text(args.get("model"), "model", _MAX_ID_CHARS, required=False)

    # [DROP-2] Observable worker sessions. A worker must be a REAL, openable session
    # (case_role=worker, joined to the Manager's Case) — not a sessionless run_oneoff
    # whose whole transcript is one prompt+reply blob invisible at the session level.
    # When no existing session is reused AND we have a repo (cwd) to root one in, open a
    # real worker session first via the existing POST /api/sessions, then submit the
    # objective INTO it below (the `case_id` join stamps case_role=worker). Fall back to
    # the legacy one-off only when there is no repo to open a session in — surfaced in the
    # reply so it is never a silent regression.
    # [ADR-0001] Canonical rule: an AGENT (worker) is always spawned as a persistent SDK
    # session — never a sessionless one-off. A dispatch with neither a reused session_id
    # NOR a cwd would POST /api/instructions with no session, and the orchestrator runs a
    # sessionless task through run_oneoff → ClaudePrintResumeDriver: the legacy `claude -p`
    # CLI driver (no persistent client, no prompt cache, full context rebuilt every turn).
    # Refuse it here instead of silently burning quota — the Manager must pass `cwd` (opens
    # a real, observable SDK worker session) or `session_id` (reuse a warm worker). See
    # docs/adr/0001-canonical-sdk-driver-for-agent-spawn.md.
    if not session_id and not cwd:
        raise ValueError(
            "dispatch_worker refuses a sessionless dispatch: pass `cwd` (the Case repo, so a "
            "real observable SDK worker session is opened) or `session_id` (to reuse a warm "
            "worker). Without either, the worker would run on the legacy `claude -p` CLI "
            "driver — no persistent client, no prompt cache. See ADR-0001."
        )
    if not session_id and not model:
        raise ValueError(
            "dispatch_worker requires an explicit model selection when opening a NEW worker "
            "session. Classify the task and retry with model='haiku', 'sonnet', 'opus', or "
            "another configured worker model; do not rely on the Claude default."
        )
    if not session_id:
        from config.models import is_advisory, validate

        validated_model = validate(backend, model)
        if validated_model is None and not is_advisory(backend):
            raise ValueError(
                f"dispatch_worker refuses unknown model {model!r} for backend {backend!r}; "
                "choose a configured worker model."
            )
        model = validated_model

    opened_session = False
    if not session_id and cwd:
        # CreateSessionBody.backend is REQUIRED (no default) — omitting it 422s and
        # silently drops the observable-session path back to a legacy one-off.
        sess_body: Dict[str, Any] = {"repo_path": cwd, "backend": backend}
        if model:
            # CreateSessionBody.model is optional and flows create_session → session
            # row → ClaudeAgentOptions(model=…). This is the ONLY correct seam for a
            # per-worker model — /api/instructions has no model field and would drop it.
            sess_body["model"] = model
        if node_id:
            sess_body["node_id"] = node_id
        if role == "worker":
            # Opt the NEW worker session into a role-ful boot. role_boot is a
            # create-time signal, so it only applies when we open a session here;
            # reusing an existing session_id cannot retro-stamp it.
            sess_body["role_boot"] = "worker"
        sess_result = _api_request("POST", "/api/sessions", sess_body)
        new_sess = sess_result.get("session") if isinstance(sess_result, dict) else None
        new_sid = new_sess.get("session_id") if isinstance(new_sess, dict) else None
        if not isinstance(new_sid, str) or not new_sid.strip() or len(new_sid) > _MAX_ID_CHARS:
            raise RuntimeError(
                "dispatch_worker could not open a worker session: /api/sessions returned no valid "
                "session_id. Refusing to fall back to a sessionless worker dispatch."
            )
        session_id = new_sid
        opened_session = True

    body: Dict[str, Any] = {"description": objective}
    if session_id:
        body["session_id"] = session_id
    if cwd:
        body["cwd"] = cwd
    if files:
        body["target_files"] = files
    if case_id:
        # [A38] Manager→worker MEMBERSHIP: the worker task JOINS the Manager's
        # existing Case (a `task` link on that Case), rather than spawning its own
        # child Case. This is the M3.1 default — the Manager passes its OWN case_id.
        body["case_id"] = case_id
    if parent_flow_run_id:
        # [A32] The endpoint now accepts this and stamps it onto the child's
        # flow_runs row via the M2 substrate — but ONLY when the gateway runs with
        # HARNESS_FLOW_DRIVE ON (a SHADOW record; nothing reads it to drive work).
        # Use for a genuine child-CASE lineage edge; use case_id (above) to make the
        # worker JOIN the Manager's Case instead.
        body["parent_flow_run_id"] = parent_flow_run_id

    result = _api_request("POST", "/api/instructions", body)
    task_id = result.get("task_id", "?")
    session = result.get("session") or {}
    sess_id = session.get("session_id") if isinstance(session, dict) else None

    # [A46] Durable wait relay. If the worker JOINED a Case, record a durable
    # pending-wait marker so a Manager that crashes/restarts mid-wait can
    # RECONCILE which workers it was still waiting on (the completion signal —
    # task.finished — is already durable; this makes the WAIT durable too).
    # Best-effort: a relay failure (incl. the 404 when DURABLE_RELAY_ENABLED is
    # OFF, or an unavailable gateway) must NEVER break the dispatch itself.
    wait_relay_note: Optional[str] = None
    if case_id and task_id and task_id != "?":
        try:
            _api_request(
                "POST", f"/api/cases/{urllib.parse.quote(case_id)}/waits",
                {"task_id": task_id},
            )
            wait_relay_note = (
                "durable wait recorded — recoverable after a restart via "
                "reconcile_waits(case_id)."
            )
        except RuntimeError:
            wait_relay_note = None  # relay disabled/unavailable — silent, non-fatal

    resolved_sid = sess_id or session_id
    if opened_session:
        session_line = f"{resolved_sid} (NEW observable worker session — openable/resumable in the UI)"
    elif resolved_sid:
        session_line = f"{resolved_sid} (reused existing session)"
    else:
        session_line = "(one-off, no session — pass cwd to open an observable worker session)"
    lines = [
        f"Dispatched worker task: {task_id}",
        f"Objective: {objective}",
        f"Session:   {session_line}",
        f"CWD:       {cwd or '(session/default)'}",
        f"Files:     {', '.join(files) if files else '(none)'}",
    ]
    if model:
        if opened_session:
            lines.append(f"Model:     {model} (this worker session boots on it)")
        else:
            lines.append(
                f"Model:     {model} REQUESTED but NOT applied — a reused session keeps its "
                f"boot model; open a NEW worker session (omit session_id, pass cwd) to tier the model."
            )
    if case_id:
        lines.append(
            f"case_id: {case_id} — the worker JOINS this (the Manager's) Case as a member "
            f"task; it does NOT spawn a child Case. Worker completion leaves the Case OPEN "
            f"(Task finished != Case completed)."
        )
        if wait_relay_note:
            lines.append(f"relay: {wait_relay_note}")
    if parent_flow_run_id:
        lines.append(
            f"parent_flow_run_id: {parent_flow_run_id} — sent as the Manager→worker "
            f"lineage edge (recorded when the gateway runs HARNESS_FLOW_DRIVE ON; it is a "
            f"SHADOW record — confirm via /api/flows, don't assume)."
        )
    lines.append("")
    if case_id:
        # [A38] A JOINED worker has NO flow_run of its own — its completion is a
        # `task.finished` event on the Manager's Case timeline. wait_for_worker must
        # therefore be given the Case as flow_run_id (task_id alone can't resolve a
        # flow that does not exist); it filters the Case timeline by this task_id.
        lines.append(
            f"Next: after you have dispatched the workers for this batch, arm_wait_group("
            f"case_id='{case_id}', member_task_ids=[…, '{task_id}'], condition='ANY') and RETURN "
            f"control — the harness re-enters this Case with a review turn as each worker finishes, "
            f"and a wake never interrupts a live operator turn. Only fall back to wait_for_worker("
            f"task_id='{task_id}', flow_run_id='{case_id}') for a single synchronous wait when you "
            f"have nothing else to do. (A joined worker has no own flow_run, so task_id ALONE cannot "
            f"resolve it.) Neither holds a task slot."
        )
    else:
        lines.append(
            f"Next: call wait_for_worker(task_id='{task_id}') to block until the worker's "
            f"flow reaches a terminal/attention status. That poll does NOT hold a task slot."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: wait_for_worker
# ---------------------------------------------------------------------------

def _resolve_flow_run_id(task_id: str) -> Optional[str]:
    """Find the newest flow_run for a task via GET /api/flows?task_id=."""
    result = _api_request("GET", f"/api/flows?task_id={urllib.parse.quote(task_id)}&limit=1")
    flows = result.get("flows") or []
    if flows:
        return flows[0].get("flow_run_id")
    return None


def _terminal_task_event(
    flow_run_id: str, task_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """[A37] Detect a dispatched worker's completion from the `task.finished` event.

    Post-A37, a task ending records an authoritative ``task.finished`` case event
    but NO LONGER writes ``flow_runs.status`` (a Case closes only via close_case).
    So a plain worker dispatch signals "the turn finished" via this event, not via
    status — poll the case timeline for it. Returns ``{"kind","outcome"}`` (kind:
    done|attention) for the matching event, or None if the turn has not finished.
    Read-only; a transport failure propagates as RuntimeError so the poll loop's
    blip tolerance handles it uniformly."""
    detail = _api_request(
        "GET", f"/api/work/{urllib.parse.quote(flow_run_id)}/timeline",
    )
    events = detail.get("events") or []
    matches = [
        e for e in events
        if e.get("event_type") == "task.finished"
        and (not task_id or e.get("entity_id") == task_id)
    ]
    if not matches:
        return None
    payload = matches[-1].get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = None
    outcome = str(payload.get("outcome")) if isinstance(payload, dict) and payload.get("outcome") else "success"
    return {"kind": "done" if outcome == "success" else "attention", "outcome": outcome}


def _wait_for_worker(args: Dict[str, Any]) -> str:
    task_id = _bounded_text(args.get("task_id"), "task_id", _MAX_ID_CHARS, required=False)
    flow_run_id = _bounded_text(args.get("flow_run_id"), "flow_run_id", _MAX_ID_CHARS, required=False)
    if not task_id and not flow_run_id:
        raise ValueError("wait_for_worker requires task_id or flow_run_id")

    timeout = _clamp_float(args.get("timeout"), _WAIT_TIMEOUT_DEFAULT, _POLL_INTERVAL_MIN, _WAIT_TIMEOUT_MAX)
    poll_interval = _clamp_float(args.get("poll_interval"), _POLL_INTERVAL_DEFAULT, _POLL_INTERVAL_MIN, 60.0)

    deadline = time.monotonic() + timeout
    resolved_id = flow_run_id
    last_status: Optional[str] = None
    polls = 0
    consecutive_errors = 0
    last_error: Optional[str] = None

    while True:
        polls += 1
        # [A33] Tolerate transient gateway blips: a single poll failure must not
        # abort a long wait. Only the HTTP transport (_api_request → RuntimeError)
        # is caught here; validation (ValueError) already ran before the loop.
        try:
            if resolved_id is None and task_id is not None:
                resolved_id = _resolve_flow_run_id(task_id)

            if resolved_id is not None:
                detail = _api_request("GET", f"/api/flows/{urllib.parse.quote(resolved_id)}")
                flow = detail.get("flow") or {}
                last_status = flow.get("status")
                kind = classify_status(last_status)
                if kind in ("done", "attention"):
                    stage = flow.get("current_stage")
                    return (
                        f"Worker flow {resolved_id} reached: {kind.upper()}\n"
                        f"status={last_status!r} current_stage={stage!r}\n"
                        f"task_id={task_id or '(unknown)'} polls={polls}\n"
                        + ("\nNeeds attention (blocked/review/decision) — not a clean completion; "
                           "the Manager should inspect the case before continuing."
                           if kind == "attention" else
                           "\nTerminal. Review the worker's committed diff in git before closing "
                           "the case (do NOT trust a self-reported summary).")
                    )
                # [A37] Honest closure: task-end no longer flips flow_runs.status
                # (a Case closes only via close_case). A plain worker dispatch
                # signals its turn finished via the `task.finished` event — poll for
                # it so wait_for_worker still terminates on real completion.
                tev = _terminal_task_event(resolved_id, task_id)
                if tev is not None:
                    stage = flow.get("current_stage")
                    ekind = tev["kind"]
                    return (
                        f"Worker flow {resolved_id} reached: {ekind.upper()} (task.finished)\n"
                        f"task_outcome={tev['outcome']!r} current_stage={stage!r}\n"
                        f"task_id={task_id or '(unknown)'} polls={polls}\n"
                        + ("\nWorker turn finished cleanly; the Case remains OPEN "
                           "(Task finished != Case completed). Review the committed diff "
                           "in git, then close the Case authoritatively via close_case "
                           "once the objective is truly met."
                           if ekind == "done" else
                           "\nWorker turn FAILED; the Case remains open for the Manager to "
                           "inspect and decide (rework / close).")
                    )
            consecutive_errors = 0  # a clean poll resets the streak
        except RuntimeError as exc:
            consecutive_errors += 1
            last_error = str(exc)
            if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                return (
                    f"ERROR: wait_for_worker gave up after {consecutive_errors} consecutive "
                    f"poll failures ({polls} polls). Last error: {last_error}. The worker may "
                    f"still be running — inspect /api/work manually and re-call if needed."
                )
            # Otherwise fall through: sleep and retry until the deadline.

        if time.monotonic() >= deadline:
            where = resolved_id or (f"(unresolved flow for task {task_id})" if task_id else "(no id)")
            err_note = f" last poll error={last_error!r}." if last_error else ""
            return (
                f"TIMEOUT after {timeout:.0f}s ({polls} polls). "
                f"Worker flow {where} last status={last_status!r} (still active/unresolved).{err_note} "
                f"The worker may still be running — re-call wait_for_worker or inspect "
                f"/api/work manually. NOTE: if the flow_run row never appears, confirm "
                f"HARNESS_FLOW_DRIVE is ON and that a plain worker dispatch writes a flow "
                f"row (open question for the live spike — see AGENT_31)."
            )

        # Sleep, but never past the deadline.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(poll_interval, remaining))


# ---------------------------------------------------------------------------
# Tool: reconcile_waits  (A46 — recover outstanding worker waits after a restart)
# ---------------------------------------------------------------------------

def _reconcile_waits(args: Dict[str, Any]) -> str:
    """[A46/M3.3] Recover the Manager's outstanding worker waits from the durable
    ledger after a crash/restart.

    ``wait_for_worker`` is an in-process poll — if the Manager/gateway crashes
    mid-wait, that wait is lost. This tool asks the gateway to reconcile the Case's
    durable ``worker.wait_pending`` markers against the already-durable
    ``task.finished`` events: finished workers are RESOLVED (cleared), still-open
    ones are returned as PENDING so the Manager can re-arm a fresh
    ``wait_for_worker`` for each. Idempotent — safe to call repeatedly. A 404 means
    the durable relay is disabled on the gateway (DURABLE_RELAY_ENABLED OFF)."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/waits/reconcile")
    if not result.get("ok"):
        return (
            f"reconcile_waits did NOT run on Case {case_id}: {result.get('reason')}. "
            f"(A 'durable_relay_disabled' reason means DURABLE_RELAY_ENABLED is OFF.)"
        )
    resolved = result.get("resolved") or []
    pending = result.get("pending") or []
    lines = [
        f"Reconciled outstanding worker waits for Case {case_id}:",
        f"  resolved (worker turn finished): {len(resolved)}",
        f"  pending  (still running):        {len(pending)}",
    ]
    for r in resolved:
        lines.append(f"    ✓ {r.get('task_id')} → outcome={r.get('outcome')!r} (wait cleared)")
    for p in pending:
        lines.append(f"    … {p.get('task_id')} still open — re-arm with wait_for_worker(task_id='{p.get('task_id')}', flow_run_id='{case_id}')")
    if not resolved and not pending:
        lines.append("  (no outstanding waits — nothing to recover.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: arm_wait_group  (M3.4 — autonomous Case continuation)
# ---------------------------------------------------------------------------

def _arm_wait_group(args: Dict[str, Any]) -> str:
    """[M3.4] Arm a wait-GROUP over a set of dispatched workers so the harness
    autonomously RE-ENTERS this Case when the group is satisfied — no manual poke.

    ``condition``: ANY (wake on each new completion, coalescing simultaneous ones,
    until drained), ALL/NAMED (wake once when every member has finished). When the
    group is satisfied over the finished-but-unconsumed members, the gateway
    delivers ONE coalesced review turn to this (live+idle) Manager session. Use this
    INSTEAD of serially long-polling ``wait_for_worker`` when you want the Case to
    continue itself across worker completions. A 404/disabled reason means
    CASE_CONTINUATION_ENABLED is OFF on the gateway."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    group_id = _bounded_text(args.get("wait_group_id"), "wait_group_id", _MAX_ID_CHARS, required=True)
    condition = (_bounded_text(args.get("condition"), "condition", 16, required=False) or "ANY").upper()
    if condition not in ("ANY", "ALL", "NAMED"):
        return f"arm_wait_group: condition must be ANY|ALL|NAMED, got {condition!r}."
    members = args.get("member_task_ids") or []
    if not isinstance(members, list) or not members:
        return "arm_wait_group requires a non-empty member_task_ids list (the dispatched worker task_ids)."
    members = [str(m) for m in members][:256]
    body = {"wait_group_id": group_id, "condition": condition, "member_task_ids": members}
    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/wait-group", body)
    if not result.get("ok"):
        return (
            f"arm_wait_group did NOT arm on Case {case_id}: {result.get('reason')}. "
            "(A 404/disabled reason means CASE_CONTINUATION_ENABLED is OFF on the gateway.)"
        )
    return (
        f"Armed wait-group {group_id!r} ({condition}) over {len(members)} worker task(s) on "
        f"Case {case_id}. When satisfied, the harness autonomously re-enters this Case with ONE "
        "coalesced review turn — you do not need to serially long-poll wait_for_worker."
    )


# ---------------------------------------------------------------------------
# Tool: get_case  (minimal Case-aware read for the M3.1 vertical slice)
# ---------------------------------------------------------------------------

def _get_case(args: Dict[str, Any]) -> str:
    """Read the Manager's Case: status + completion_criteria + current stage.

    Read-only over GET /api/flows/{case_id}. The minimum Case awareness the loop
    needs to decide close vs. rework — it does NOT close anything (closure is the
    authoritative close_case gateway op)."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    detail = _api_request("GET", f"/api/flows/{urllib.parse.quote(case_id)}")
    flow = detail.get("flow") or {}
    status = flow.get("status")
    criteria = flow.get("completion_criteria")
    stage = flow.get("current_stage")
    objective = flow.get("objective_lock") or flow.get("objective")
    # [A52] completion_criteria may be the dual-shape object {"round_cap": N,
    # "criteria": …} when a round_cap was set — unpack it so the Manager reads the
    # human criteria (not a JSON blob) and sees the cap on its own line.
    criteria_display, round_cap_line = criteria, None
    if isinstance(criteria, str) and criteria.strip().startswith("{"):
        try:
            obj = json.loads(criteria)
            if isinstance(obj, dict) and "round_cap" in obj:
                criteria_display = obj.get("criteria")
                round_cap_line = f"round_cap:           {obj.get('round_cap')!r} (autonomous-continuation backstop)"
        except Exception:
            pass
    lines = [
        f"Case {case_id}",
        f"status:              {status!r} (a Case with status NULL/open is still IN PROGRESS — "
        "a finished worker Task does NOT close it)",
        f"current_stage:       {stage!r}",
        f"completion_criteria: {criteria_display!r}",
        *([round_cap_line] if round_cap_line else []),
        f"objective:           {objective!r}",
        "",
        "Decide from git evidence + these criteria: close (via close_case) only when the "
        "criteria are truly met; otherwise rework/derive/block.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: get_case_brief  (A54 — durable single-call Case reconstruction)
# ---------------------------------------------------------------------------

def _get_case_brief(args: Dict[str, Any]) -> str:
    """[A54] The FULL working state of a Case from the DB alone (read-only over
    GET /api/cases/{case_id}/brief).

    This is the Manager's single 'where am I on this Case' read when it has lost
    its in-process context (compaction, restart, respawn): objective + criteria +
    round cap + rounds used + every dispatched worker (finished? latest verdict?) +
    outstanding/ready waits + armed wait-groups and whether each is satisfied — all
    reconstructed from the durable ledger, not lost memory. Prefer this over
    get_case when resuming a Case; get_case is the minimal status read."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    result = _api_request("GET", f"/api/cases/{urllib.parse.quote(case_id)}/brief")
    brief = result.get("brief") or {}
    if not brief:
        return f"get_case_brief: no Case found for {case_id!r} (unknown or unavailable)."

    workers = brief.get("workers") or []
    groups = brief.get("wait_groups") or []
    open_waits = brief.get("open_waits") or []
    ready_waits = brief.get("ready_waits") or []
    latest_review = brief.get("latest_review") or None
    lines = [
        f"Case {brief.get('case_id')} — reconstructed from the DB (durable state, not memory).",
        f"objective:           {brief.get('objective')!r}",
        f"status:              {brief.get('status')!r} (NULL/open ⇒ still IN PROGRESS)",
        f"current_stage:       {brief.get('current_stage')!r}",
        f"completion_criteria: {brief.get('completion_criteria')!r}",
        f"rounds:              {brief.get('rounds_used')}/{brief.get('round_cap')} used "
        f"({brief.get('rounds_remaining')} remaining before the continuation cap)",
        f"latest_review:       {latest_review!r}",
        "",
        f"Dispatched workers ({len(workers)}):",
    ]
    for w in workers:
        rev = w.get("latest_review")
        lines.append(
            f"  • task {w.get('task_id')} — "
            f"{'finished' if w.get('finished') else 'in-flight'}"
            + (f" outcome={w.get('outcome')!r}" if w.get('finished') else "")
            + (f" review={rev.get('verdict')!r}" if rev else "")
        )
    if not workers:
        lines.append("  (none dispatched yet.)")
    lines.append("")
    lines.append(
        f"Waits — open (still running): {open_waits or '[]'}; "
        f"ready (finished, reconcile them): {ready_waits or '[]'}"
    )
    lines.append(f"Armed wait-groups ({len(groups)}):")
    for g in groups:
        lines.append(
            f"  • {g.get('wait_group_id')} ({g.get('condition')}) over "
            f"{len(g.get('members') or [])} member(s) — "
            f"{'SATISFIED' if g.get('satisfied') else 'waiting'}"
            + (f", present={g.get('presented_task_ids')}" if g.get('satisfied') else "")
        )
    if not groups:
        lines.append("  (no live wait-groups.)")
    lines.append("")
    lines.append(
        "Resume from THIS state: reconcile any 'ready' waits, re-arm/continue the live "
        "groups, review finished workers' git diffs, and decide close vs. rework."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: read_session_history  (pull a prior session's real conversation on demand)
# ---------------------------------------------------------------------------

_HISTORY_TURNS_DEFAULT = 30
_HISTORY_TURNS_MAX = 200
_HISTORY_OUTPUT_MAX_CHARS = 60000


def _read_session_history(args: Dict[str, Any]) -> str:
    """Read a specific session's real conversation (read-only over
    GET /api/sessions/{session_id}/messages).

    This is how a Manager familiarizes itself with a prior line of work BEYOND the
    bounded excerpt it may have been seeded with at boot — e.g. the session it was
    forked from (its `continued_from`), or any session the operator names. Returns
    the per-turn You:/Agent: transcript oldest→newest, bounded so the read cannot
    itself blow the context window. Use `limit` to take the most recent N turns."""
    session_id = _bounded_text(args.get("session_id"), "session_id", _MAX_ID_CHARS, required=True)
    raw_limit = args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else _HISTORY_TURNS_DEFAULT
    except (TypeError, ValueError):
        limit = _HISTORY_TURNS_DEFAULT
    limit = max(1, min(limit, _HISTORY_TURNS_MAX))
    data = _api_request(
        "GET",
        f"/api/sessions/{urllib.parse.quote(session_id)}/messages?limit={limit}",
    )
    messages = data.get("messages") or []
    if not messages:
        return f"Session {session_id}: no conversation turns found (empty or unknown session)."
    lines = [
        f"Conversation history for session {session_id} — "
        f"{len(messages)} most-recent turn(s), oldest→newest:"
    ]
    for i, m in enumerate(messages, 1):
        ts = (m.get("timestamp") or m.get("completed_at") or "").strip()
        instr = (m.get("instruction") or "").strip()
        reply = (m.get("result") or "").strip()
        if instr:
            lines.append(f"\n[{i}] {ts}\nYou: {instr}")
        if reply:
            lines.append(f"Agent: {reply}")
    out = "\n".join(lines)
    if len(out) > _HISTORY_OUTPUT_MAX_CHARS:
        # Keep the most recent tail; tell the Manager to page with a smaller limit.
        marker = "…(older turns omitted to fit — request fewer turns via `limit`)…\n"
        out = marker + out[-(_HISTORY_OUTPUT_MAX_CHARS - len(marker)):]
    return out


# ---------------------------------------------------------------------------
# Tool: open_case  (M3.3 — let ONE Manager session own many Cases sequentially)
# ---------------------------------------------------------------------------

def _open_case(args: Dict[str, Any]) -> str:
    """Open a NEW Case on the Manager's OWN session (POST /api/cases).

    This is what lets a single persistent Manager session run the full loop for
    several objectives in a row — open → dispatch → review → close → open the next
    — instead of spawning a fresh session (and re-paying its boot context) per Case.
    Requires the Manager's own ``session_id`` and a checkable ``completion_criteria``
    that close_case will later demand. Returns the new case_id to use with
    dispatch_worker(case_id=...), record_review, get_case, and close_case."""
    objective = _bounded_text(args.get("objective"), "objective", _MAX_OBJECTIVE_CHARS)
    session_id = _bounded_text(args.get("session_id"), "session_id", _MAX_ID_CHARS, required=True)
    completion_criteria = _bounded_text(
        args.get("completion_criteria"), "completion_criteria", _MAX_OBJECTIVE_CHARS, required=False)

    body: Dict[str, Any] = {"objective": objective, "session_id": session_id}
    if completion_criteria:
        body["completion_criteria"] = completion_criteria

    # [M3.4/A52] Optional autonomous-continuation round cap. Validate it here so a
    # bad value is a clean tool error, not a 422 from the route.
    round_cap_raw = args.get("round_cap")
    round_cap: Optional[int] = None
    if round_cap_raw is not None:
        try:
            round_cap = int(round_cap_raw)
        except (TypeError, ValueError):
            return f"open_case: round_cap must be a positive integer, got {round_cap_raw!r}."
        if round_cap <= 0:
            return f"open_case: round_cap must be a positive integer, got {round_cap}."
        body["round_cap"] = round_cap

    result = _api_request("POST", "/api/cases", body)
    case_id = result.get("case_id", "?")
    cap_line = f"round_cap: {round_cap} (autonomous-continuation backstop)\n" if round_cap else ""
    return (
        f"Opened Case {case_id} on session {session_id}.\n"
        f"Objective: {objective}\n"
        f"completion_criteria: {completion_criteria or '(none — set one so close_case can verify done)'}\n"
        f"{cap_line}\n"
        f"This is YOUR Case now. dispatch_worker(case_id='{case_id}') to run a worker into it, "
        f"then arm_wait_group(case_id='{case_id}', …) to be re-entered on completion instead of "
        f"block-polling. record_review after verifying its git diff, and close_case('{case_id}') "
        f"when the criteria are truly met. When you close it, this session stays alive — open_case "
        f"again for the next objective."
    )


# ---------------------------------------------------------------------------
# Tool: close_case  (the Manager's Decision surface — A37 authoritative close)
# ---------------------------------------------------------------------------

def _close_case(args: Dict[str, Any]) -> str:
    """Authoritatively close the Manager's Case via A37 ``close_case``.

    A REFUSAL (unmet completion_criteria / open child work / pending approval) is a
    normal decision signal, not an error — the Manager must resolve it and retry.
    ``criteria_reconciliation`` is an optional list recording each criterion as met
    or waived-with-reason."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    outcome = _bounded_text(args.get("outcome"), "outcome", 32, required=False) or "closed"
    reconciliation = args.get("criteria_reconciliation")
    body: Dict[str, Any] = {"outcome": outcome}
    if reconciliation is not None:
        if not isinstance(reconciliation, list):
            raise ValueError("criteria_reconciliation must be a list")
        body["criteria_reconciliation"] = reconciliation

    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/close", body)
    ok = bool(result.get("ok"))
    closed = bool(result.get("closed"))
    reason = result.get("reason")
    if ok and closed:
        return f"Case {case_id} CLOSED (outcome={outcome!r})."
    if ok and not closed:
        return f"Case {case_id} was already terminal — idempotent no-op."
    return (
        f"Close REFUSED for Case {case_id}: {reason}. This is a DECISION SIGNAL, not an error — "
        f"resolve it (finish/verify the work in git, reconcile or waive-with-reason each "
        f"completion criterion, close any open child work, or resolve the pending approval) and "
        f"retry close_case."
    )


# ---------------------------------------------------------------------------
# Tool: record_review  (M3.2 — the Manager's review verdict emitter)
# ---------------------------------------------------------------------------

_REVIEW_VERDICTS = ("accepted", "rework_requested", "waived")


def _record_review(args: Dict[str, Any]) -> str:
    """Record the Manager's review verdict on a Case as a ``review.*`` flow_event.

    ``verdict`` is required and must be one of accepted|rework_requested|waived;
    ``reason`` is an optional short note. POSTs to /api/cases/{case_id}/review.
    A 404 means the emitter is disabled on the gateway (REVIEW_EMITTER_ENABLED OFF)."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    verdict = _bounded_text(args.get("verdict"), "verdict", 32, required=True)
    if verdict not in _REVIEW_VERDICTS:
        raise ValueError(
            f"verdict must be one of {', '.join(_REVIEW_VERDICTS)} (got {verdict!r})"
        )
    reason = _bounded_text(args.get("reason"), "reason", _MAX_OBJECTIVE_CHARS, required=False)

    body: Dict[str, Any] = {"verdict": verdict, "reason": reason}
    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/review", body)
    ok = bool(result.get("ok"))
    if ok:
        return (
            f"Recorded review verdict {verdict!r} on Case {case_id} "
            f"(event_type={result.get('event_type')})."
        )
    return (
        f"record_review did NOT record on Case {case_id}: {result.get('reason')}. "
        f"Resolve and retry."
    )


# ---------------------------------------------------------------------------
# Tools: M4 spec authoring → scored review → decomposer (A56)
# ---------------------------------------------------------------------------

# R1 rubric dimensions surfaced to the reviewer tool. Kept in sync with
# src.control.db.SPEC_REVIEW_DIMENSIONS (the db layer is authoritative for grading).
_SPEC_REVIEW_DIMENSIONS = (
    "objective_clarity",
    "scope_boundaries",
    "decomposability",
    "acceptance_testability",
    "dependency_correctness",
    "risks_and_assumptions",
)


def _publish_spec(args: Dict[str, Any]) -> str:
    """[A56/M4] Author a feature spec ONTO your Case as durable evidence.

    Before dispatching workers for a feature-sized intent, author a spec (reuse the
    draft_packet contract: real_objective vs literal_request vs interpreted_task +
    forced assumptions/drift_risks). It is recorded as an ``artifact`` link + a
    ``spec.authored`` event. This does NOT grade the spec — a SEPARATE reviewer seat
    scores it (record_spec_review). A 404/disabled reason means SPEC_AUTHORING_ENABLED
    is OFF on the gateway."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    spec_id = _bounded_text(args.get("spec_id"), "spec_id", _MAX_ID_CHARS, required=True)
    body_text = _bounded_text(args.get("body"), "body", _MAX_OBJECTIVE_CHARS, required=True)
    title = _bounded_text(args.get("title"), "title", 512, required=False)
    body = {"spec_id": spec_id, "body": body_text, "title": title}
    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/spec", body)
    if not result.get("ok"):
        return (
            f"publish_spec did NOT record on Case {case_id}: {result.get('reason')}. "
            "(A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF on the gateway.)"
        )
    return (
        f"Authored spec {spec_id!r} on Case {case_id} (durable artifact + spec.authored "
        "event). Next: have a SEPARATE reviewer score it via record_spec_review before decomposing."
    )


def _publish_artifact(args: Dict[str, Any]) -> str:
    """[A56/M4] Publish an arbitrary durable artifact (kind/title/uri) onto your Case
    as evidence (an ``artifact`` link + ``artifact.published`` event). A 404/disabled
    reason means SPEC_AUTHORING_ENABLED is OFF on the gateway."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    artifact_id = _bounded_text(args.get("artifact_id"), "artifact_id", _MAX_ID_CHARS, required=True)
    kind = _bounded_text(args.get("kind"), "kind", 64, required=False) or "artifact"
    title = _bounded_text(args.get("title"), "title", 512, required=False)
    uri = _bounded_text(args.get("uri"), "uri", _MAX_PATH_CHARS, required=False)
    body = {"artifact_id": artifact_id, "kind": kind, "title": title, "uri": uri}
    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/artifacts", body)
    if not result.get("ok"):
        return (
            f"publish_artifact did NOT record on Case {case_id}: {result.get('reason')}. "
            "(A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF on the gateway.)"
        )
    return f"Published artifact {artifact_id!r} (kind={kind}) on Case {case_id}."


def _record_spec_review(args: Dict[str, Any]) -> str:
    """[A56/M4] Score a Case's spec against the R1 rubric — this is the SEPARATE
    plan-reviewer seat, NOT the author grading its own spec. ``scores`` maps each of
    six dimensions (objective_clarity, scope_boundaries, decomposability,
    acceptance_testability, dependency_correctness, risks_and_assumptions) to 0–2.
    The verdict is COMPUTED (≥8/12 AND no zero on objective_clarity or decomposability),
    not taken on your word: a below-threshold or critical-zero score BLOCKS decomposition.
    A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF on the gateway."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    spec_id = _bounded_text(args.get("spec_id"), "spec_id", _MAX_ID_CHARS, required=True)
    reason = _bounded_text(args.get("reason"), "reason", _MAX_OBJECTIVE_CHARS, required=False)
    reviewer = _bounded_text(args.get("reviewer"), "reviewer", 64, required=False) or "reviewer"
    scores = args.get("scores")
    if not isinstance(scores, dict) or not scores:
        return (
            "record_spec_review requires a `scores` object mapping each rubric dimension to 0-2: "
            + ", ".join(_SPEC_REVIEW_DIMENSIONS)
        )
    body = {"spec_id": spec_id, "scores": scores, "reason": reason, "reviewer": reviewer}
    result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/spec-review", body)
    if not result.get("ok"):
        return (
            f"record_spec_review did NOT record on Case {case_id}: {result.get('reason')}. "
            "(A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF on the gateway.)"
        )
    verdict = result.get("verdict")
    gate = "UNLOCKS decomposition" if result.get("passed") else "BLOCKS decomposition (fix the spec + re-score)"
    return (
        f"Scored spec {spec_id!r} on Case {case_id}: {result.get('total')}/{result.get('max')} "
        f"(threshold {result.get('threshold')}), verdict={verdict!r} — {gate}. "
        f"critical_zero={result.get('critical_zero')} missing={result.get('missing')} "
        f"out_of_range={result.get('out_of_range')}."
    )


def _decompose_case(args: Dict[str, Any]) -> str:
    """[A56/M4] Expand an APPROVED objective into a task-DAG on this ONE Case — N
    task_attached links carrying dependency edges, NOT N scattered child cases. Each
    task is ``{task_key, objective, depends_on: [task_key,...], ...hints}``. REFUSED
    (structured reason) unless the spec's latest scored review PASSED, and unless the
    DAG is acyclic + every depends_on names a known task_key. Creates ZERO new
    flow_runs. A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF."""
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)
    spec_id = _bounded_text(args.get("spec_id"), "spec_id", _MAX_ID_CHARS, required=True)
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "decompose_case requires a non-empty `tasks` list ({task_key, objective, depends_on})."
    if len(tasks) > 256:
        return "decompose_case: too many tasks (max 256). Under-decompose over over-decompose."
    body = {"spec_id": spec_id, "tasks": tasks}
    # A blocked decomposition (unapproved spec / cyclic / malformed DAG) returns HTTP 422
    # whose structured detail _api_request folds into the RuntimeError message — surface
    # it as a clean refusal rather than leaking a traceback.
    try:
        result = _api_request("POST", f"/api/cases/{urllib.parse.quote(case_id)}/decompose", body)
    except RuntimeError as e:
        return (
            f"decompose_case REFUSED on Case {case_id}: {e}. "
            "(spec_not_approved => get a passing spec-review first; cyclic_dependencies => break the "
            "cycle; unknown_dependency/invalid_task_keys => fix the DAG; a 404 => SPEC_AUTHORING_ENABLED OFF.)"
        )
    order = result.get("order") or []
    return (
        f"Decomposed Case {case_id} into {len(result.get('task_keys') or [])} task_attached DAG node(s) "
        f"(ZERO new flow_runs). Dispatch order (topological): {order}. Dispatch each in order as its "
        "dependencies complete."
    )


# ---------------------------------------------------------------------------
# Tool: release_worker  (A48 — the Manager's explicit worker-close decision)
# ---------------------------------------------------------------------------

def _release_worker(args: Dict[str, Any]) -> str:
    """Close exactly ONE named worker session when the Manager decides it is done.

    Worker sessions are kept WARM by default (closing a Case no longer tears them
    down) so a follow-up dispatch is a cheap resume. This tool is the ONLY path that
    ends a worker's process — call it deliberately, per worker, never reflexively.
    Ownership guard: before closing, verify the target session against the
    authoritative session→case affiliation index (GET /api/work/affiliations/sessions).
    The target must (a) exist as an affiliated session, (b) carry role 'worker', and
    (c) belong to THIS Manager's Case (``case_id``). Any failure is returned as a
    structured refusal (not an exception), so a Manager can never close an arbitrary
    session, a non-worker, or a worker of a different Case. Only a verified worker of
    the caller's own Case reaches the POST /api/sessions/{session_id}/close."""
    session_id = _bounded_text(args.get("session_id"), "session_id", _MAX_ID_CHARS, required=True)
    case_id = _bounded_text(args.get("case_id"), "case_id", _MAX_ID_CHARS, required=True)

    # Ownership guard over the authoritative session→case index. SessionView.to_dict()
    # does NOT expose case_role/current_case_id, so the /api/sessions list is unusable
    # here — the affiliations endpoint is the one authoritative source.
    index = _api_request("GET", "/api/work/affiliations/sessions")
    affiliations = index.get("affiliations") or []
    row = next((a for a in affiliations if a.get("session_id") == session_id), None)
    if row is None:
        return (
            f"release_worker REFUSED: session {session_id} is not an affiliated session "
            f"(unknown or standalone — not a member of any Case). Nothing closed."
        )
    role = row.get("role")
    if role != "worker":
        return (
            f"release_worker REFUSED: session {session_id} has role {role!r}, not 'worker' "
            f"— release_worker only closes worker sessions. Nothing closed."
        )
    owner_case = row.get("flow_run_id")
    if owner_case != case_id:
        return (
            f"release_worker REFUSED: session {session_id} belongs to Case {owner_case!r}, "
            f"not your Case {case_id!r} — you may only release a worker of your own Case. "
            f"Nothing closed."
        )

    # Verified worker of the caller's Case — a 200 from /close is always ok. A 404
    # (already-closed / unknown session) surfaces as RuntimeError from _api_request;
    # return the same structured-refusal shape rather than leak an exception.
    try:
        _api_request("POST", f"/api/sessions/{urllib.parse.quote(session_id)}/close")
    except RuntimeError as exc:
        return (
            f"release_worker did NOT close session {session_id}: {exc}. "
            f"The session may already be closed or the backend rejected the close; "
            f"resolve and retry."
        )
    return (
        f"Released worker session {session_id} (Case {case_id}) — its backend process is "
        f"now CLOSED. A later follow-up would be a COLD re-open (fresh boot), so only "
        f"release a worker you have decided is truly done."
    )


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------
_TOOLS = [
    {
        "name": "dispatch_worker",
        "description": (
            "Dispatch a bounded task to a WORKER as a real gateway task (separate from the "
            "Manager's own session — never a sub-agent). Thin wrapper over the existing, "
            "auth-guarded, Level-3-gated POST /api/instructions. Returns the worker's task_id; "
            "track it with wait_for_worker. Provide a professional, not-overstated objective. "
            "If session_id is given the work runs in that existing worker session; otherwise, when "
            "you pass cwd, a NEW observable worker session is opened (case_role=worker, joined to "
            "your Case) that you and the operator can open, read, and resume — always prefer this "
            "over a blind one-off. Pass your own flow_run id as parent_flow_run_id to record the "
            "Manager→worker lineage edge (visible in /api/flows). When opening a NEW worker session "
            "(`cwd`, no `session_id`), you MUST explicitly set `model` as a task-fit decision: haiku "
            "for narrow, easily verified work; sonnet for most bounded implementation and fixes; "
            "opus for architecture, high-risk, security-sensitive, or ambiguous work. This is the "
            "supported way to tier per job; do NOT shell out to `claude -p --model` via "
            "watch_job, which spawns an off-substrate process with no session, no Case link, and no "
            "telemetry. `model` applies only to a NEWLY opened worker session (a reused session_id "
            "keeps its boot model)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The bounded task for the worker (becomes the instruction description). Ground it; do not overstate scope."},
                "session_id": {"type": "string", "description": "Existing worker session to run in. Omit to open a NEW observable worker session (when cwd is given)."},
                "cwd": {"type": "string", "description": "Working directory / repo path. Pass it (typically your Case's repo) so a real, openable worker session can be opened; without it the dispatch falls back to a sessionless one-off. RESOLVED ON THE TARGET NODE'S filesystem (see node_id) — a path that exists on your node but not on the gateway host requires node_id set to the node that actually holds the repo."},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Target files to focus the worker on (optional)."},
                "case_id": {"type": "string", "description": "The Manager's OWN Case id. Pass it to make the worker JOIN this Case (member task, shared membership) instead of spawning a child Case — the M3.1 default. Worker completion leaves the Case OPEN."},
                "node_id": {"type": "string", "description": "Node to pin the NEW worker session to: the worker boots on THIS node and cwd is resolved on ITS filesystem, so pin the node that actually holds the repo (e.g. a node worker so the session survives a gateway restart). Pass the node's EXACT node_id as shown by /api/nodes — it is matched exactly, NOT a fuzzy display-name lookup. OMIT ONLY if the repo lives on the gateway host itself: omitting routes the worker to the gateway host (__local__ — NOT the Manager's own node), where the gateway validates cwd against its own allowed_root and rejects a missing/outside path up front with invalid_repo_path. (A pinned remote node skips that up-front check — a bad cwd there surfaces at the worker's first turn instead.)"},
                "role": {"type": "string", "description": "Set to 'worker' to boot the NEW worker session with the canonical Worker role (worker.md identity + worker tools), gated by MANAGER_ROLE_ENABLED. Omit for a legacy tier-0 worker. Only applies when a new session is opened (with cwd); it cannot retro-stamp a reused session_id."},
                "model": {"type": "string", "description": "REQUIRED when opening a NEW worker session (cwd with no session_id): explicitly choose the task-fit boot model. Use 'haiku' for narrow/easy-to-verify work, 'sonnet' for most bounded implementation and fixes, and 'opus' for architecture, high-risk, security-sensitive, or ambiguous work. The choice is sent only to session creation. Ignored when a session_id is reused because that worker keeps its boot model."},
                "parent_flow_run_id": {"type": "string", "description": "Use ONLY for a genuine child-CASE lineage edge (child→parent in /api/flows). To keep the worker inside the Manager's Case, use case_id instead."},
            },
            "required": ["objective"],
        },
    },
    {
        "name": "open_case",
        "description": (
            "Open a NEW Case on YOUR OWN Manager session (POST /api/cases). This is how a single "
            "persistent Manager session takes on another objective without spawning a fresh session "
            "— open -> dispatch_worker(case_id) -> arm_wait_group -> review -> close_case -> open the "
            "next. Provide your own session_id and a checkable completion_criteria (close_case will "
            "demand it). Pass round_cap to bound an autonomous continuation loop. Returns the new "
            "case_id. Use when you finish one Case and want to start the next in the same "
            "conversation, or when the operator hands you a new objective."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The objective for the new Case. Ground it; do not overstate scope."},
                "session_id": {"type": "string", "description": "YOUR OWN Manager session id (the session this Case is owned by)."},
                "completion_criteria": {"type": "string", "description": "The checkable done-gate close_case will require (e.g. 'tests green; diff reviewed; PR opened')."},
                "round_cap": {"type": "integer", "description": "Optional autonomous-continuation backstop: the MAX number of Wake-Dispatcher re-entries (arm_wait_group) before the Case escalates instead of looping. A safety bound, not a tuning knob — set a small value (e.g. 6-10) for a live autonomous run. Omit to use the engine default (50)."},
            },
            "required": ["objective", "session_id"],
        },
    },
    {
        "name": "get_case",
        "description": (
            "Read the Manager's Case (read-only over GET /api/flows/{case_id}): status, "
            "current_stage, completion_criteria, objective. A Case with an open/NULL status is "
            "still in progress — a finished worker Task does NOT close it. Use before deciding "
            "close vs. rework; closure itself is the authoritative close_case gateway operation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Case (flow_run) id to inspect — the Manager's own case."},
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "get_case_brief",
        "description": (
            "Reconstruct the FULL working state of a Case from the DB ALONE (read-only over "
            "GET /api/cases/{case_id}/brief) — the Manager's single 'where am I on this Case' "
            "read after a context reset (compaction, restart, respawn). Returns objective + "
            "completion_criteria + round cap + rounds used + every DISPATCHED worker (finished? "
            "outcome? latest review verdict?) + outstanding/ready worker waits + every ARMED "
            "wait-group and whether it is currently satisfied. Prefer this over get_case when "
            "resuming a Case you have lost the in-memory picture of; get_case is the minimal "
            "status-only read. Read-only — it decides nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Case (flow_run) id to reconstruct — the Manager's own case."},
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "read_session_history",
        "description": (
            "Read a specific session's REAL conversation, oldest→newest (read-only over "
            "GET /api/sessions/{session_id}/messages). This is how you familiarize yourself "
            "with a prior line of work BEYOND the bounded excerpt seeded at boot — pass the "
            "session you were forked from (your continued_from), or any session the operator "
            "names, to pull its actual turns. Output is bounded; use `limit` to take the most "
            "recent N turns and page if a session is long. Reference only — verify against git."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session whose conversation to read (e.g. the session you were forked from)."},
                "limit": {"type": "number", "description": f"Most recent N turns to return (default {_HISTORY_TURNS_DEFAULT}, max {_HISTORY_TURNS_MAX})."},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "close_case",
        "description": (
            "Authoritatively close the Manager's Case (A37 close_case) — the Decision surface. "
            "REFUSES (returns a reason, not an error) while completion_criteria are unreconciled, "
            "a child flow is still open, or a required approval is pending: a finished worker Task "
            "does NOT close the Case, only this call does. Pass criteria_reconciliation to record "
            "each criterion met or waived-with-reason. Verify the work in git BEFORE closing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Case (flow_run) id to close — the Manager's own case."},
                "outcome": {"type": "string", "description": "Terminal status: 'closed' (default) or 'cancelled'."},
                "criteria_reconciliation": {
                    "type": "array",
                    "description": "Optional list reconciling each completion criterion. Each entry needs a \"status\": e.g. [{\"criterion\":\"tests green\",\"status\":\"met\"}] or [{\"criterion\":\"docs updated\",\"status\":\"waived\",\"reason\":\"out of scope\"}]. A boolean \"met\" is NOT accepted — the Case will refuse to close.",
                    "items": {"type": "object"},
                },
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "record_review",
        "description": (
            "Record the Manager's review verdict on a Case as a canonical review.* event on "
            "the Case audit trail (the M3.2 verdict emitter). verdict must be one of "
            "accepted | rework_requested | waived; reason is an optional short note. Use this "
            "AFTER reviewing the worker's committed git diff to make the Decision explicit: "
            "'accepted' records approval, 'rework_requested' records that changes are needed "
            "(and blocks close_case until a later accept/waive supersedes it), 'waived' records "
            "an accepted-as-is with a reason. A 404 means the emitter is disabled on the gateway."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Case (flow_run) id being reviewed — the Manager's own case."},
                "verdict": {"type": "string", "enum": list(_REVIEW_VERDICTS), "description": "The review verdict: accepted | rework_requested | waived."},
                "reason": {"type": "string", "description": "Optional short note explaining the verdict (required in spirit for a waive)."},
            },
            "required": ["case_id", "verdict"],
        },
    },
    {
        "name": "wait_for_worker",
        "description": (
            "Block (read-only long-poll) until a dispatched worker's flow reaches a terminal "
            "status (done/failed/cancelled) or an attention status (blocked/review/needs-decision), "
            "or until timeout. Give task_id (preferred) or flow_run_id. **For a worker dispatched "
            "into your Case (dispatch_worker with case_id), pass BOTH task_id AND flow_run_id=<your "
            "case_id>** — a joined worker has no flow_run of its own, so task_id alone cannot resolve "
            "it; the poll then watches your Case timeline for that task's task.finished. This poll "
            "does NOT hold a worker task slot, so waiting here cannot starve the slot the worker "
            "needs. On return, verify the worker's committed diff in git — never trust a self-reported summary. "
            "\n\n**Do NOT serially long-poll a whole batch.** While this call runs your session is BUSY — you "
            "cannot review other finished workers or answer the operator. After dispatching several workers, "
            "prefer ONE short wait (or check get_case / the Case timeline for task.finished events) and process "
            "whichever worker is already done, rather than blocking the full timeout on one worker while the "
            "others sit finished. The ceiling is intentionally short; on TIMEOUT you get control back to re-check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by dispatch_worker."},
                "flow_run_id": {"type": "string", "description": "The flow_run id. REQUIRED (alongside task_id) for a worker joined into your Case — pass your own case_id. Optional otherwise (skips task->flow resolution)."},
                "timeout": {"type": "number", "description": f"Max seconds to wait (default {int(_WAIT_TIMEOUT_DEFAULT)}, max {int(_WAIT_TIMEOUT_MAX)})."},
                "poll_interval": {"type": "number", "description": f"Seconds between polls (default {int(_POLL_INTERVAL_DEFAULT)}, min {int(_POLL_INTERVAL_MIN)})."},
            },
        },
    },
    {
        "name": "reconcile_waits",
        "description": (
            "Recover your OUTSTANDING worker waits after a crash/restart (M3.3 durable "
            "relay). wait_for_worker is an in-process poll, so a Manager/gateway crash "
            "mid-wait loses it. This asks the gateway to reconcile your Case's durable "
            "worker.wait_pending markers against the already-durable task.finished events: "
            "finished workers are RESOLVED (cleared) and still-open ones are returned as "
            "PENDING so you can re-arm a fresh wait_for_worker for each. Idempotent — safe "
            "to call repeatedly. Call it when you resume a Case and are unsure which workers "
            "you were still waiting on. A 404/disabled reason means DURABLE_RELAY_ENABLED is OFF."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Manager's OWN Case id whose outstanding worker waits to reconcile."},
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "arm_wait_group",
        "description": (
            "Arm a wait-GROUP over dispatched workers so the harness AUTONOMOUSLY re-enters "
            "this Case when the group is satisfied — the M3.4 alternative to serially "
            "long-polling wait_for_worker. condition ANY = wake on each new completion "
            "(coalescing simultaneous ones) until drained; ALL/NAMED = wake ONCE when every "
            "member has finished. On satisfaction the gateway delivers ONE coalesced review "
            "turn to this live+idle Manager session. Use it when you want the Case to continue "
            "itself across worker completions instead of blocking. A 404/disabled reason means "
            "CASE_CONTINUATION_ENABLED is OFF on the gateway."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Manager's OWN Case id the group belongs to."},
                "wait_group_id": {"type": "string", "description": "A name for this group (e.g. 'batch-1'). Arming the same group id again is idempotent."},
                "condition": {"type": "string", "description": "ANY | ALL | NAMED. Default ANY (good for a single worker or 'wake me on each completion')."},
                "member_task_ids": {"type": "array", "items": {"type": "string"}, "description": "The dispatched worker task_ids that make up the group."},
            },
            "required": ["case_id", "wait_group_id", "member_task_ids"],
        },
    },
    {
        "name": "publish_spec",
        "description": (
            "Author a feature SPEC onto your Case before decomposing (M4). For a feature-sized "
            "intent, do NOT dive straight into dispatch: author a spec (reuse the draft_packet "
            "contract — real_objective vs literal_request vs interpreted_task + forced "
            "assumptions/drift_risks), recorded as a durable artifact + spec.authored event. "
            "This does NOT grade the spec; a SEPARATE reviewer scores it (record_spec_review). "
            "A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Manager's OWN Case id the spec belongs to."},
                "spec_id": {"type": "string", "description": "A stable id for this spec (e.g. 'spec-feature-x')."},
                "body": {"type": "string", "description": "The authored spec text (draft_packet structure)."},
                "title": {"type": "string", "description": "Optional short title for the spec."},
            },
            "required": ["case_id", "spec_id", "body"],
        },
    },
    {
        "name": "publish_artifact",
        "description": (
            "Publish an arbitrary durable artifact (kind/title/uri) onto your Case as evidence "
            "(artifact link + artifact.published event). A 404/disabled reason means "
            "SPEC_AUTHORING_ENABLED is OFF."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Manager's OWN Case id."},
                "artifact_id": {"type": "string", "description": "A stable id for the artifact."},
                "kind": {"type": "string", "description": "Free label for the artifact kind (default 'artifact')."},
                "title": {"type": "string", "description": "Optional short title."},
                "uri": {"type": "string", "description": "Optional pointer (path/URL) to the artifact body."},
            },
            "required": ["case_id", "artifact_id"],
        },
    },
    {
        "name": "record_spec_review",
        "description": (
            "Score a Case's spec against the R1 rubric — the SEPARATE plan-reviewer seat, NOT "
            "the author grading its own spec. `scores` maps six dimensions (objective_clarity, "
            "scope_boundaries, decomposability, acceptance_testability, dependency_correctness, "
            "risks_and_assumptions) each to 0-2. The verdict is COMPUTED (>=8/12 AND no zero on "
            "objective_clarity or decomposability): a below-threshold or critical-zero score "
            "BLOCKS decomposition. A 404/disabled reason means SPEC_AUTHORING_ENABLED is OFF."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Case id whose spec is being scored."},
                "spec_id": {"type": "string", "description": "The spec id being scored (from publish_spec)."},
                "scores": {
                    "type": "object",
                    "description": "Map each rubric dimension to an integer 0-2.",
                    "properties": {d: {"type": "integer"} for d in _SPEC_REVIEW_DIMENSIONS},
                },
                "reason": {"type": "string", "description": "Optional note explaining the score."},
                "reviewer": {"type": "string", "description": "The reviewing seat (should differ from the author)."},
            },
            "required": ["case_id", "spec_id", "scores"],
        },
    },
    {
        "name": "decompose_case",
        "description": (
            "Expand an APPROVED objective into a task-DAG on this ONE Case — N task_attached "
            "links with dependency edges, NOT N scattered child cases (orphan flow_runs are the "
            "anti-goal M2.5 exists to prevent). Each task is {task_key, objective, depends_on: "
            "[task_key,...], ...hints}. REFUSED unless the spec's latest scored review PASSED and "
            "the DAG is acyclic + every depends_on names a known task_key. Creates ZERO new "
            "flow_runs. Dispatch the returned topological order in sequence. A 404/disabled reason "
            "means SPEC_AUTHORING_ENABLED is OFF."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The Manager's OWN Case id to decompose."},
                "spec_id": {"type": "string", "description": "The APPROVED spec id (must have a passing spec-review)."},
                "tasks": {
                    "type": "array",
                    "description": "The DAG nodes.",
                    "items": {"type": "object"},
                },
            },
            "required": ["case_id", "spec_id", "tasks"],
        },
    },
    {
        "name": "release_worker",
        "description": (
            "Close ONE worker session when YOU have decided that worker is truly done — the "
            "Manager's explicit worker-close decision. Worker sessions are kept WARM by default "
            "(closing a Case no longer closes them), so a follow-up dispatch is a cheap resume; "
            "releasing one ENDS its backend process, and any later question to it becomes a COLD "
            "re-open. Never release reflexively and never as a side-effect of closing the Case — "
            "release only the specific worker you have judged finished. Pass your OWN case_id: the "
            "target is verified against the authoritative session→case index and REFUSED unless it "
            "is a worker session that is a member of your Case (ownership check). Closes exactly "
            "the named session via POST /api/sessions/{session_id}/close."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The worker session id to close (from dispatch_worker). Exactly this session is closed — nothing else."},
                "case_id": {"type": "string", "description": "The Manager's OWN Case id. Used to verify the target is a worker session joined to YOUR Case before closing — a session that is unknown, not a worker, or a member of a different Case is refused."},
            },
            "required": ["session_id", "case_id"],
        },
    },
]

_TOOL_IMPLS = {
    "dispatch_worker": _dispatch_worker,
    "wait_for_worker": _wait_for_worker,
    "open_case": _open_case,
    "get_case": _get_case,
    "get_case_brief": _get_case_brief,
    "read_session_history": _read_session_history,
    "close_case": _close_case,
    "record_review": _record_review,
    "reconcile_waits": _reconcile_waits,
    "arm_wait_group": _arm_wait_group,
    "publish_spec": _publish_spec,
    "publish_artifact": _publish_artifact,
    "record_spec_review": _record_spec_review,
    "decompose_case": _decompose_case,
    "release_worker": _release_worker,
}

# ---------------------------------------------------------------------------
# MCP protocol — JSON-RPC 2.0 over stdio  (identical shape to mcp_jobs.py)
# ---------------------------------------------------------------------------

def _send(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj), flush=True)


def _reply(id_: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _reply_error(id_: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _dispatch(req: Dict[str, Any]) -> None:
    method: str = req.get("method", "")
    id_: Optional[Any] = req.get("id")

    if method == "initialize":
        _reply(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "manager", "version": "1.0.0"},
        })

    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass  # fire-and-forget

    elif method == "tools/list":
        _reply(id_, {"tools": _TOOLS})

    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        impl = _TOOL_IMPLS.get(name)
        if impl is None:
            _reply_error(id_, -32601, f"Unknown tool: {name!r}")
            return
        try:
            text = impl(arguments)
            _reply(id_, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # noqa: BLE001 — surface as an MCP tool error, never crash
            print(f"[mcp_manager] {name} failed: {exc}", file=sys.stderr, flush=True)
            _reply(id_, {
                "content": [{"type": "text", "text": f"Error in {name}: {exc}"}],
                "isError": True,
            })

    else:
        if id_ is not None:
            _reply_error(id_, -32601, f"Unknown method: {method!r}")


def main() -> None:
    print("[mcp_manager] ready", file=sys.stderr, flush=True)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
            _dispatch(req)
        except json.JSONDecodeError as exc:
            _reply_error(None, -32700, f"Parse error: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[mcp_manager] internal error: {exc}", file=sys.stderr, flush=True)
            _reply_error(None, -32603, f"Internal error: {exc}")


if __name__ == "__main__":
    main()
