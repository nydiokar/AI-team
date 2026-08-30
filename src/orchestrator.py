"""
orchestrator.py — TaskOrchestrator
====================================
Central gateway coordinator: receives instructions from every inbound surface,
routes them to the right backend (local or remote mesh node), manages session
lifecycle, and drives the M3 Manager/Worker automation loop.

ARCHITECTURE — data flow
------------------------
  Inbound surfaces         Entry points              Execution
  ─────────────────────    ──────────────────────    ──────────────────────────
  Telegram / Web UI   ──► submit_instruction()  ──► _enqueue_task()
  .task.md file drop  ──► _handle_new_task_file()    │
  MCP manager tool    ──► invoke_manager()            ▼
                                                  _task_worker()  (pool)
                                                      │
                                              ┌───────┴────────┐
                                              ▼                ▼
                                  _dispatch_or_run_local()  _process_task_remote()
                                  (local backend)           (mesh node)
                                              │
                                              ▼
                                      _write_artifacts()
                                      _emit_event() / _emit_turn_telemetry()

  Autonomous (M3.4)   ──► _wake_dispatcher_loop() ──► _continue_case_once()
                                                   ──► _finalize_continuation()

SECTIONS (in source order)
--------------------------
  1.  Result parsing & text extraction       (static helpers)
  2.  Backend resolution                     (_resolve_task_backend)
  3.  Startup recovery                       (restart scan, stale-session heal)
  4.  Wake dispatcher / case continuation    (M3.4, flag CASE_CONTINUATION_ENABLED)
  5.  Stale-busy reconciler + reattach       (M3 mesh, flag MESH_ENABLED)
  6.  Job completion poller                  (T3 watched-jobs, flag MESH_ENABLED)
  7.  Lifecycle — start / stop               (embedded servers, worker pool)
  8.  Task creation & enqueue                (_make_task, _enqueue_task, flow-run record)
  9.  Case management                        (open/close/arm/wait — M3/M3.4)
  10. Manager invocation                     (invoke_manager — M3.1)
  11. Flow tracking                          (flow_runs / flow_events / flow_links — M1/M2)
  12. Task submission & context injection    (submit_instruction ★, compact/restart ctx)
  13. Worker loop                            (_task_worker coroutine pool)
  14. Task execution — local path            (process_task ★)
  15. Task execution — remote path           (_process_task_remote)
  16. Artifact write                         (_write_artifacts)
  17. Error classification & retry           (_classify_error, _get_retry_strategy)
  18. Events & telemetry                     (_emit_event, _emit_turn_telemetry)
  19. Status / session recording             (get_status, _write_session_summary)
  20. Mesh routing                           (_run_backend_local, _dispatch_to_node)
  21. Mesh DB shadow-write helpers           (_mesh_enqueue_task, reconcile)
  22. Proactive turns (reach-back)           (_handle_proactive_turn, _notify_proactive_turn)

FEATURE FLAGS (runtime-gated, all default OFF unless noted)
------------------------------------------------------------
  HARNESS_FLOW_DRIVE          — write flow_runs stage transitions (M1/M2, shadow)
  MANAGER_ROLE_ENABLED        — enable M3 Manager role boot via /api/manager
  REVIEW_EMITTER_ENABLED      — emit review.* events on record_review()
  CASE_CONTINUATION_ENABLED   — wake-dispatcher autonomous continuation (M3.4)
  DURABLE_RELAY_ENABLED       — persist worker.wait_pending markers for crash recovery
  HARNESS_LEVEL3_GUARD        — admission gate for level-3 harness tasks
  QUOTA_COORDINATOR_ENABLED   — observe-only quota/session-window coordinator
"""
import asyncio
import json
import logging
import time
import shutil
import subprocess
import re
import socket
import threading
from pathlib import Path
from typing import Callable, Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from src.core.timeutil import now_iso, parse_iso
import uuid
import random
import contextlib

import sys
import os

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from src.core import (
    ITaskOrchestrator, Task, TaskResult, TaskStatus, TaskType, TaskPriority,
    SessionStatus,
)
from src.services import (
    TaskParser, AsyncFileWatcher, SessionStore, PathResolver,
    SessionService, WorkflowService, NotificationService,
)
from src.bridges import LlamaMediator
from src.backends.registry import build_backends
from config import config
from src.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)


#: [quota-resume] The error classes that mean "the provider refused the turn
#: because the account's quota/rate window is spent" — a PAUSE, not a failure of
#: the agent or the harness. The single vocabulary every quota-aware branch reads
#: (session status, Case pause record, resume proposal, retry policy).
QUOTA_PAUSE_ERROR_CLASSES: Tuple[str, ...] = ("usage_limit", "rate_limit")

#: [transient-resume] Error classes that mean "a transient PROVIDER-side failure,
#: not a broken session or a spent quota" — an Anthropic 5xx (``api_error_status
#: >= 500`` ⇒ ``upstream_error``, which is exactly what a 529 Overloaded lands as;
#: see ``_classify_error``). Deliberately NOT ``network``: that class mixes local
#: driver deaths (``terminated process``, ``cannot write to``) that are not a
#: provider overload and must not be auto-retried into a loop. A turn in one of
#: these classes that goes terminal (its in-process burst retries exhausted) keeps
#: the session REUSABLE and PAUSES the Case on a short escalating backoff.
TRANSIENT_PAUSE_ERROR_CLASSES: Tuple[str, ...] = ("upstream_error",)

#: [transient-resume] The escalating fixed backoff (seconds) between auto-retries
#: of a transient-paused Case. A 529 carries no ``resetsAt`` — the provider gives
#: no reopen instant — so unlike the quota seam this is a timer, bounded by its own
#: length: the Nth pause in the rolling window waits ``[N-1]`` seconds, and once N
#: exceeds ``len(...)`` the retry budget is spent (``flow.transient_pause_exhausted``
#: + escalate). Four attempts spanning ~8.5 min covers a typical overload window.
TRANSIENT_PAUSE_BACKOFF_SEC: Tuple[int, ...] = (30, 60, 120, 300)

#: [transient-resume] Rolling window over which consecutive transient pauses are
#: counted to pick the backoff / detect budget exhaustion. Long enough to hold the
#: whole escalating sequence, short enough that an unrelated 529 hours later starts
#: fresh at attempt 1 — so the bound self-resets without a 'retry finally
#: succeeded' hook (worker task.finished events on the same Case cannot be used as
#: that signal, they are not the Manager's own turn).
TRANSIENT_PAUSE_WINDOW_SEC: int = 900

#: [transient-resume] Cap on how much of the failed turn's instruction is stored on
#: the pause ledger so the retry can re-run the EXACT turn. A Manager continuation/
#: operator instruction is modest; anything larger falls back to a generic
#: re-derive-from-ledger retry rather than bloating flow_events.
_TRANSIENT_FAILED_PROMPT_CAP: int = 16_000

#: Approval ``action`` vocabulary for the two Case-level operator decisions.
#: Both are Case-scoped (no task_id, no session_id) — see ApprovalService.request's
#: ``case_id`` argument.
CASE_RESPAWN_APPROVAL_ACTION = "case_manager_respawn"
CASE_RESUME_APPROVAL_ACTION = "case_resume"

CACHE_HEARTBEAT_PROMPT = (
    "[cache-heartbeat]\n"
    "The gateway is waking this session only to keep the Claude Code prompt cache warm\n"
    "while you are waiting on durable long-running work.\n\n"
    "Do not call tools. Do not inspect files. Do not make decisions.\n"
    "Reply exactly CACHE_HEARTBEAT_OK.\n\n"
    "If you are not actually waiting on useful work, reply exactly STOP_CACHE_HEARTBEAT."
)

#: [quota-resume] How a paused Case comes back.
#:   ``in_place``      — one turn into the SAME (still alive, AWAITING_INPUT)
#:                       Manager session. Keeps the full conversation; the
#:                       provider re-writes the whole prompt cache because the
#:                       cache TTL expired hours ago, which is the expensive part.
#:   ``fresh_manager`` — a NEW role-full Manager session on the SAME Case,
#:                       reconstructed from the Case ledger (get_case_brief). The
#:                       cheap path: it carries the objective, criteria, worker
#:                       verdicts and open waits, not the transcript. This is the
#:                       ONLY correct choice when the old session is dead.
#: Neither is a fork: both stay on the same ``flow_run_id`` and never re-open or
#: re-scope the objective.
CASE_RESUME_MODES: Tuple[str, ...] = ("in_place", "fresh_manager")

#: [quota-resume] When ``in_place`` is the ECONOMICALLY right resume, despite the
#: cache-rewrite risk. Either condition is sufficient:
#:   * the pause is younger than the provider's prompt-cache TTL (~1h) ⇒ the cache
#:     is still warm, so resuming the live session re-writes nothing;
#:   * the session's recent cache writes are small ⇒ even a cold rewrite is cheap,
#:     and keeping the full conversation is worth more than the tokens.
#: Otherwise ``fresh_manager`` — rebuilt from the Case brief, which is the whole
#: reason that mode exists.
RESUME_IN_PLACE_CACHE_WARM_SEC: int = 3600
RESUME_IN_PLACE_MAX_CACHE_TOKENS: int = 100_000


#: [quota-resume] The wall-clock reset instant the provider states IN WORDS on its
#: own refusal: "You've hit your session limit · resets 8:20pm (Europe/Kiev)".
#: Requires an explicit IANA zone in parentheses ON PURPOSE — a bare "resets
#: 4:30pm" is printed in the timezone of whatever machine ran the CLI (often a
#: remote node, not this gateway), so interpreting it here would silently invent a
#: boundary hours off. Unqualified wording stays unparsed, exactly as before.
_LIMIT_RESET_CLOCK_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*\(([A-Za-z]+/[A-Za-z_\-+]+)\)",
    re.IGNORECASE,
)

#: A five-hour window can never reopen more than ~5h out, and no quota refusal
#: names a boundary a day away. Anything beyond this is a misparse, not a reset.
_LIMIT_RESET_MAX_AHEAD = timedelta(hours=25)

#: [quota-resume] How long a pause with NO recorded reset instant may wait for a
#: telemetry reading younger than itself before it gives up and asks anyway.
#: Longer than the five-hour window, short of the weekly one: past this, quota is
#: either back or the instrument is dead, and both mean "ask the operator".
_QUOTA_BLIND_WAIT_MAX = timedelta(hours=5, minutes=30)

#: Tolerance for the ordinary race where the observer polls in the same moment
#: the provider refuses the turn — such a reading is contemporaneous, not stale.
_QUOTA_RESTORE_READING_GRACE = timedelta(seconds=60)


def _reset_at_from_limit_text(text: str, *, now: Optional[datetime] = None) -> Optional[str]:
    """[quota-resume] The provider's own reset instant, recovered from the WORDS of
    its refusal, as a UTC ISO string — or ``None``.

    This exists because the structured ``rate_limit_event.resetsAt`` is NOT
    present on every path (notably a turn executed on a remote mesh node), while
    the human-readable phrase is. Live on 2026-08-19 the harness held
    "resets 8:20pm (Europe/Kiev)" — i.e. 17:20Z, the correct answer to the second
    — and used it only to render a label, so the Case recorded ``reset_at: null``
    and proposed a resume an hour before quota actually returned.

    The named zone is authoritative; the DATE is not stated, so the next
    occurrence of that wall-clock time is taken (a small grace window means a
    boundary that just passed reads as "now", not "tomorrow").
    """
    if not isinstance(text, str) or not text.strip():
        return None
    match = _LIMIT_RESET_CLOCK_RE.search(text)
    if match is None:
        return None
    hour_s, minute_s, meridiem, zone_name = match.groups()
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        return None
    try:
        hour = int(hour_s)
        minute = int(minute_s or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    if meridiem.lower() == "p":
        hour = hour if hour == 12 else hour + 12
    elif hour == 12:
        hour = 0
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < current - timedelta(minutes=5):
        candidate += timedelta(days=1)
    if candidate - current > _LIMIT_RESET_MAX_AHEAD:
        return None
    return candidate.astimezone(timezone.utc).isoformat()


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """[quota-resume] Tolerant ISO → tz-aware UTC datetime, ``None`` on anything
    unparseable. Quota telemetry is an EXTERNAL reading: a missing/garbage
    timestamp must degrade the decision, never raise into a dispatcher tick."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def is_quota_pause_result(result: TaskResult) -> bool:
    """[quota-resume] True when a turn ended because the quota window is spent.

    Keyed ONLY on the structured ``error_class`` that ``_classify_error`` /
    ``classify_error_text`` already derive from the SDK's ``api_error_status``
    (429) and the provider's limit wording. Deliberately NOT a second free-text
    scan of ``output``: the agent's own reply legitimately contains phrases like
    "rate limit" when it is *writing code about* rate limiting, and a text gate
    there would silently convert a genuine hard failure into a "paused" Case.
    One classifier, one vocabulary.
    """
    if result.success:
        return False
    return str(getattr(result, "error_class", "") or "").lower() in QUOTA_PAUSE_ERROR_CLASSES


def is_transient_provider_pause_result(result: TaskResult) -> bool:
    """[transient-resume] True when a turn ended on a terminal transient
    provider-side failure (an Anthropic 5xx / 529 Overloaded ⇒ ``upstream_error``).

    Keyed ONLY on the structured ``error_class`` the classifier already derived
    from the SDK's ``api_error_status`` — never a second free-text scan (same
    discipline as :func:`is_quota_pause_result`: the agent's own reply can contain
    the words "server error" while writing code, and a text gate there would
    silently pause a genuine hard failure). By the time this is read the in-process
    burst retries have already been spent, so reaching here means the overload
    outlived the quick-retry budget and the Case should pause on the longer
    backoff.
    """
    if result.success:
        return False
    return str(getattr(result, "error_class", "") or "").lower() in TRANSIENT_PAUSE_ERROR_CLASSES


def _is_salvaged_backend_finalization_error(result: TaskResult) -> bool:
    """True when a backend failed its terminal wrap-up after producing useful work.

    The task still failed for audit/review purposes. The distinction is only for
    the reusable session status: do not leave the session in ERROR when the
    operator has a salvaged reply/file evidence to inspect and can continue.

    Gates (all must hold):
      - the terminal result is a backend ``error_during_execution`` — the SDK
        ends the turn normally after the agent's work, so this is NOT a hard
        execution failure. The marker lives in the raw transcript (raw_stdout),
        or in the diagnostic tail (raw_stderr / error_detail) for older rows
        where the gateway mirrored raw_stdout=output;
      - ``output`` is the driver's honest salvaged reply, i.e. it starts with the
        salvage banner AND carries agent content beyond that banner (a bare
        error string like "backend failed" is not salvage).

    A quota pause is NOT salvage and is deliberately not handled here: it keeps
    the session alive via :func:`is_quota_pause_result` in
    :func:`_session_status_after_result`, but it must keep reporting the turn as
    failed (no work was produced) — see :func:`_reclassify_salvaged_turn_success`.

    Deliberately does NOT depend on ``error_class == "backend_error"``: every
    call site reclassifies via ``_classify_error`` (which never returns that
    value) — the original gate could never fire. And it does NOT require
    ``files_modified``: work that is already committed leaves it empty while the
    agent still produced a real reply.
    """
    if result.success:
        return False
    output = (result.output or "").strip()
    if not output:
        return False
    raw = (
        f"{result.raw_stdout or ''}\n{result.raw_stderr or ''}\n"
        f"{getattr(result, 'error_detail', '') or ''}"
    ).lower()
    if not (
        '"subtype": "error_during_execution"' in raw
        or '"subtype":"error_during_execution"' in raw
    ):
        return False
    from src.backends.claude_driver import SALVAGE_ERROR_BANNER

    if not output.startswith(SALVAGE_ERROR_BANNER):
        return False
    return len(output) > len(SALVAGE_ERROR_BANNER)


def _reclassify_salvaged_turn_success(result: TaskResult) -> TaskResult:
    """Flip a salvaged-but-terminally-errored turn to success.

    ``_is_salvaged_backend_finalization_error`` already tells the session layer
    to resume (``AWAITING_INPUT``) for these turns: the SDK's terminal wrap-up
    failed *after* the agent finished real, deliverable work, OR the failure is
    a quota/rate limit (not a real execution failure). Task status, turn
    telemetry, session history, and the ``mesh_tasks`` row all key off
    ``result.success`` independently of that session-status check, so without
    this fixup they keep surfacing the turn as failed to the operator even
    though nothing about the agent's or harness's actual outcome failed.
    ``errors``/``raw_stdout``/``raw_stderr``/``error_class`` are left untouched
    so the original signal is still there for anyone who inspects the turn —
    only the terminal success flag changes.
    """
    if _is_salvaged_backend_finalization_error(result):
        result.success = True
    return result


def _session_status_after_result(result: TaskResult, *, cancel_requested: bool = False) -> SessionStatus:
    # [quota-resume] A quota pause keeps the session REUSABLE: nothing about the
    # session broke, the provider simply refused the turn until the window
    # resets, so ERROR would be a lie that also makes the Case unresumable
    # (the Wake-Dispatcher only ever wakes an AWAITING_INPUT Manager). The turn
    # itself still reports failed — see is_quota_pause_result.
    if result.success or _is_salvaged_backend_finalization_error(result) or is_quota_pause_result(result):
        return SessionStatus.AWAITING_INPUT
    # [transient-resume] A terminal transient provider 5xx (529 Overloaded) did not
    # break the session — the provider refused the turn while overloaded — so ERROR
    # would be a lie that also strands the Case (the Wake-Dispatcher only ever wakes
    # an AWAITING_INPUT Manager). Flag-gated so OFF ⇒ byte-identical (ERROR as
    # before); the flag read only fires on the rare transient turn, never per
    # result. The turn itself still reports failed — see is_transient_provider_pause_result.
    if is_transient_provider_pause_result(result):
        try:
            from src.control.db import transient_provider_resume_enabled
            if transient_provider_resume_enabled():
                return SessionStatus.AWAITING_INPUT
        except Exception:
            pass
    if cancel_requested or any("cancelled" in str(error).lower() for error in (result.errors or [])):
        return SessionStatus.CANCELLED
    return SessionStatus.ERROR


def _session_dispatch_payload(session: Any) -> Dict[str, Any]:
    """Serialize a Session into the ``payload["session"]`` dict a worker node
    reconstructs via ``_make_session_from_payload``.

    ``case_role`` MUST travel here: the node's driver applies the Manager
    role boot (``_role_boot`` — role prompt + scoped manager tools) off
    ``session.case_role``. Omitting it made a Manager pinned to a node boot as
    a bare, role-less session (the A43 finding) — the whole point of this fix.

    ``role_boot`` MUST travel for the identical reason: it is the Worker-role
    tier opt-in the driver's ``_role_boot`` reads to apply the Worker role prompt
    + tools. Dropping it makes a node-pinned role-ful worker boot role-less
    (same carrier-coupling class as ``case_role``).

    ``model`` MUST be resolved here (gateway-side), not sent as the raw
    ``session.model`` field. A remote node has no visibility into the
    gateway's ``CLAUDE_DEFAULT_MODEL`` — an unresolved/empty model let the
    node's own local Claude CLI installation pick its own default, which can
    silently diverge from the gateway's configured default.
    """
    from config.models import resolve_model

    return {
        "session_id": session.session_id,
        "backend": session.backend,
        "repo_path": session.repo_path,
        "backend_session_id": session.backend_session_id,
        "model": resolve_model(session),
        "effort": getattr(session, "effort", None),
        "machine_id": session.machine_id,
        "telegram_chat_id": session.telegram_chat_id,
        "telegram_thread_id": session.telegram_thread_id,
        "owner_user_id": session.owner_user_id,
        "last_user_message": session.last_user_message,
        "driver_type": session.driver_type,
        "driver_status": session.driver_status,
        "cache_health": session.cache_health,
        "cache_unhealthy_count": session.cache_unhealthy_count,
        "previous_backend_session_ids": session.previous_backend_session_ids or [],
        "case_role": getattr(session, "case_role", None) or None,
        "current_case_id": getattr(session, "current_case_id", None) or None,
        "role_boot": getattr(session, "role_boot", None) or None,
    }


def resolve_control_api_hosts(control_api_host: str, tailscale_ip: str) -> list[str]:
    """Bind hosts for the embedded Control API (UI + read/control surface).

    Fail-closed, and NEVER the LAN/public interface by default — the UI serves the
    dashboard token in-page (``control_api._mount_web_ui``), so any bound-but-
    untrusted interface hands that token to whoever can reach it.

      - An explicit ``CONTROL_API_HOST`` is honored verbatim (single bind). This is
        the operator override; ``0.0.0.0`` here is a deliberate LAN-exposure choice.
      - Otherwise bind BOTH ``127.0.0.1`` (local clients — the in-gateway Manager
        MCP, health probes, an SSH tunnel) AND this node's Tailscale IP (a remote
        mesh Manager, reachable only by tailnet peers). The LAN interface stays
        unbound, keeping the in-page token inside the two trusted zones.

    Returns the ordered, de-duplicated list of hosts to bind.
    """
    explicit: str = (control_api_host or "").strip()
    if explicit:
        return [explicit]
    hosts: list[str] = ["127.0.0.1"]
    ts: str = (tailscale_ip or "").strip()
    if ts and ts not in hosts:
        hosts.append(ts)
    return hosts


class HarnessAdmissionBlocked(Exception):
    """Raised by `_enqueue_task` when the task-harness Level-3 admission gate
    refuses a task at the queue choke point (flag on + `harness_level: 3` +
    not `approved: true`).

    Raised — not returned — so no caller can mistake a blocked task for an
    accepted one (there is no `task_id` to hand back). Callers that face an
    operator (Telegram, control API) catch this and surface a clear
    "needs operator approval" result instead of a generic error.
    """

    def __init__(self, task_id: str, reason: str = "harness_level3_needs_approval"):
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"task {task_id} blocked at admission: {reason}")


class TaskOrchestrator(ITaskOrchestrator):
    """Central gateway coordinator (see module docstring for the full nav map).

    Responsibilities (current, post-M3):
    - Receive instructions from Telegram, Web UI, .task.md files, and the MCP
      manager tool surface; enqueue them through a single admission gate.
    - Route session tasks to the right backend (local pool or mesh-remote node)
      while enforcing hard session-affinity (pinned sessions NEVER relocate).
    - Drive the M3 Manager/Worker automation loop: role-boot a Manager session,
      dispatch workers, gate on review.* verdicts, close on criteria met.
    - Run the M3.4 Wake-Dispatcher: autonomous case continuation without operator
      pokes (CASE_CONTINUATION_ENABLED flag; default OFF).
    - Persist artifacts (mesh_tasks DB canonical; results/*.json fallback),
      maintain artifact index, emit structured events and LLM-turn telemetry.
    - Own the embedded task server (mesh) and embedded control API (Web UI).

    Threading / async model:
    - File-system events arrive from a watchdog thread → marshalled onto the
      asyncio event loop via asyncio.Queue.
    - Worker coroutines (_task_worker) run as asyncio.Tasks against the loop.
    - Background loops (wake-dispatcher, stale-busy reconciler, job poller) are
      also asyncio.Tasks on the same loop — NEVER spawn threads for these.
    - The mesh task server runs embedded on this same event loop (MESH_ENABLED).

    Key seams / do-not-cross rules:
    - session.machine_id is a HARD correctness boundary: a pinned session's turn
      runs on that machine or waits — never silently relocates.
    - DB (mesh.db) is canonical. state/sessions/<id>.json is the fallback/audit
      trail — it is NEVER deleted and NEVER the sole source of truth.
    - All new feature behavior is flag-gated (default OFF ⇒ byte-identical legacy).
      See FEATURE FLAGS in the module docstring.
    """
    
    def __init__(self):
        # Initialize core components
        self.task_parser = TaskParser()
        self.file_watcher = AsyncFileWatcher(config.system.tasks_dir)
        self.llama_mediator = LlamaMediator()
        self.session_store = SessionStore()
        # Lazily constructed on first use (mirrors self.quota_coordinator) — DB
        # readiness at __init__ time is not guaranteed. See _ensure_approval_service.
        self.approval_service = None
        self.session_service = SessionService(
            self.session_store,
            remote_close_dispatcher=self._dispatch_remote_close,
        )
        self.workflow_service = WorkflowService()
        self._backends = build_backends()
        from src.control.telemetry_sink import build_runtime_telemetry_sink
        self._telemetry_sink = build_runtime_telemetry_sink(
            node_id=socket.gethostname(),
            logs_dir=config.system.logs_dir,
            is_gateway=True,
        )
        with contextlib.suppress(Exception):
            replay = getattr(self._telemetry_sink, "replay_spool", None)
            if callable(replay):
                replay()
        
        # Task management
        self.task_queue = asyncio.Queue(maxsize=config.system.max_queue_size)
        self.active_tasks: Dict[str, Task] = {}
        self.task_results: Dict[str, TaskResult] = {}
        
        # System state
        self.running = False
        self.worker_tasks: List[asyncio.Task] = []
        # Embedded mesh task server (started only when MESH_ENABLED) — shares the
        # gateway event loop so get_registry() is the same singleton dispatch uses.
        self._embedded_task_server = None
        # Embedded control API (read surface for the Web UI) — shares the gateway
        # event loop so it reads the live SessionService / NodeRegistry. U1.
        # A list: the API may bind more than one interface (loopback + Tailscale IP)
        # so local clients and a remote mesh Manager reach it without exposing the LAN.
        self._embedded_control_apis: list = []
        
        # Component status
        self.component_status = {
            "claude_available": False,
            "llama_available": False,
            "file_watcher_running": False
        }
        # In-memory lock to prevent duplicate processing of the same task file
        self._inflight_paths: set[str] = set()
        
        logger.info("TaskOrchestrator initialized")

        # ------------------------------------------------------------------
        # Remaining init: queue state, reconcile handles, background-task
        # handles, and optional interfaces. All flag-gated subsystems are
        # built ONLY when their flag is ON so a disabled path has zero
        # side-effects (no DB file, no background coroutine, no import cost).
        # ------------------------------------------------------------------
        self.validation_engine = ValidationEngine()
        # Ensure logs directory exists for event emission
        try:
            Path(config.system.logs_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # Queue persistence
        self._state_path = Path(config.system.logs_dir) / "state.json"
        self._pending_files: set[str] = set()
        self._load_state()
        # Artifact index path (task_id -> latest artifact path)
        self._artifact_index_path = Path(config.system.results_dir) / "index.json"
        # Lazy-initialized context loader (simple functional helper encapsulated here)
        self._context_loader = None
        # Task ids that have already had compact prior-context injected into their
        # prompt (opt-in `continues:` continuation). Instance-local so the guard
        # never leaks into task.metadata / the remote payload / persisted artifacts.
        self._compact_injected_ids: set[str] = set()
        # Job completion polling (T3)
        self._last_job_poll = now_iso()
        self._last_remote_job_poll = now_iso()
        self._remote_job_poll_started_epoch = time.time()
        self._processed_terminal_jobs: set[str] = set()
        self._watched_jobs_cache_lock = threading.Lock()
        self._watched_jobs_remote_cache: Dict[
            tuple[Optional[str], Optional[str], int],
            tuple[float, Dict[str, List[Dict[str, Any]]]],
        ] = {}
        self._watched_jobs_remote_cache_ttl_sec = 2.0
        # Cancellation and runtime tracking
        self._task_cancel_events: Dict[str, asyncio.Event] = {}
        self._running_exec_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_interrupted_tasks: set[str] = set()
        self._stale_busy_reconcile_task: Optional[asyncio.Task] = None
        self._mesh_reconcile_in_progress: bool = False
        # [M3.4] Wake-Dispatcher loop handle (autonomous Case continuation).
        self._wake_dispatcher_task: Optional[asyncio.Task] = None
        
        # Initialize Telegram interface if configured
        self.telegram_interface = None
        if config.telegram.bot_token:
            try:
                from src.telegram.interface import TelegramInterface
                self.telegram_interface = TelegramInterface(
                    bot_token=config.telegram.bot_token,
                    orchestrator=self,
                    allowed_users=config.telegram.allowed_users
                )
                logger.info("Telegram interface initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize Telegram interface: {e}")
                self.telegram_interface = None
        else:
            logger.info("Telegram interface not configured (no bot token)")

        # Notification dispatcher — single call site for all outbound
        # notifications (Telegram today, Web UI tomorrow).  Passes self
        # so the notifer reads ``self.telegram_interface`` dynamically.
        self.notifier = NotificationService(orchestrator=self)
        self._build_quota_coordinator()

    def _build_quota_coordinator(self) -> None:
        """Initialize the quota-window coordinator (QUOTA_COORDINATOR_ENABLED flag).

        Built ONLY when the flag is ON — the coordinator's store eagerly creates
        state/quota_windows.db in its constructor, so gating construction here keeps
        the disabled path a genuine zero-side-effect no-op (no DB file, no background
        task, no import cost) — byte-identical when off.

        Sets self.quota_coordinator and self.quota_digest_subscriber.
        """
        self.quota_coordinator = None
        self.quota_digest_subscriber = None
        try:
            from src.control.db import runtime_flag_enabled
            quota_enabled = runtime_flag_enabled("QUOTA_COORDINATOR_ENABLED")
        except Exception:
            quota_enabled = bool(getattr(getattr(config, "quota", None), "enabled", False))
        if not quota_enabled:
            return
        try:
            from src.services.quota_window_coordinator import build_quota_coordinator_from_config
            event_handlers = []
            try:
                from src.control.db import runtime_flag_enabled as _flag_enabled
                digest_enabled = _flag_enabled("QUOTA_DIGEST_TELEGRAM_ENABLED")
            except Exception:
                digest_enabled = bool(getattr(getattr(config, "quota", None), "digest_telegram_enabled", False))
            if digest_enabled:
                from src.services.quota_digest import QuotaTelegramDigestSubscriber
                digest = QuotaTelegramDigestSubscriber(
                    notifier=self.notifier,
                    chat_id=config.telegram.notification_chat_id,
                    interval_sec=getattr(config.quota, "digest_interval_sec", 3600),
                )
                self.quota_digest_subscriber = digest
                event_handlers.append(digest.handle_event)
            self.quota_coordinator = build_quota_coordinator_from_config(
                enabled=True, event_handlers=event_handlers
            )
        except Exception as e:
            logger.warning(f"Failed to initialize quota coordinator: {e}")
            self.quota_coordinator = None
        self._build_quota_prewarmer()

    def _build_quota_prewarmer(self) -> None:
        """Initialize the window prewarmer (QUOTA_PREWARM_ENABLED flag).

        Strictly downstream of the coordinator: it reads telemetry from the
        coordinator's store and activates through the coordinator's adapter, so
        with the coordinator off there is nothing to build. Off ⇒ no object, no
        task, no model turn — the same zero-side-effect posture as above.
        """
        self.quota_prewarmer = None
        if self.quota_coordinator is None:
            return
        try:
            from src.control.db import runtime_flag_enabled
            if not runtime_flag_enabled("QUOTA_PREWARM_ENABLED"):
                return
            from src.services.quota_window_prewarmer import build_prewarmer_from_config
            self.quota_prewarmer = build_prewarmer_from_config(
                coordinator=self.quota_coordinator,
                enabled=True,
                event_sink=lambda name, payload: self._emit_event(name, None, payload),
            )
        except Exception as e:
            logger.warning(f"Failed to initialize quota prewarmer: {e}")
            self.quota_prewarmer = None

    # ===========================================================================
    # RESULT PARSING & TEXT EXTRACTION
    # Static helpers that extract or format user-visible text from TaskResult /
    # raw backend payloads. No I/O, no side-effects — safe to call anywhere.
    # ===========================================================================

    @staticmethod
    def _extract_text_from_payload(payload: Any) -> str:
        """Best-effort extraction of a user-visible answer from structured payloads."""
        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                return ""
            if text.startswith("{") or text.startswith("["):
                try:
                    return TaskOrchestrator._extract_text_from_payload(json.loads(text))
                except Exception:
                    return text
            return text

        if isinstance(payload, list):
            for item in reversed(payload):
                text = TaskOrchestrator._extract_text_from_payload(item)
                if text:
                    return text
            return ""

        if not isinstance(payload, dict):
            return ""

        for key in ("result", "content", "output", "message", "text"):
            value = payload.get(key)
            text = TaskOrchestrator._extract_text_from_payload(value)
            if text:
                return text

        for key in ("messages", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                text = TaskOrchestrator._extract_text_from_payload(value)
                if text:
                    return text

        return ""

    @classmethod
    def _extract_rate_limit_info(cls, result: TaskResult) -> Optional[Dict[str, Any]]:
        """Parse the first rejected rate_limit_event from raw_stdout NDJSON, or None."""
        stdout = getattr(result, "raw_stdout", "") or ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line or "rate_limit_event" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "rate_limit_event":
                continue
            info = obj.get("rate_limit_info", {})
            if info.get("status") == "rejected":
                return info
        return None

    @classmethod
    def _extract_result_terminal_signal(cls, result: TaskResult) -> Tuple[str, Optional[int]]:
        """Parse the terminal ``{"type": "result", ...}`` NDJSON line for the
        SDK's own structured ``subtype``/``api_error_status`` fields (mirrored
        into ``raw_stdout`` by ``claude_driver._outcome_from_result``). These
        are a precise signal straight from the SDK — prefer them in
        ``_classify_error`` over guessing the failure reason from free text.
        Checks ``raw_stderr``/``error_detail`` too (older worker rows mirror
        the marker there instead of ``raw_stdout``). Returns ("", None) when
        no structured signal is present.
        """
        sources = (
            getattr(result, "raw_stdout", "") or "",
            getattr(result, "raw_stderr", "") or "",
            getattr(result, "error_detail", "") or "",
        )
        for source in sources:
            for line in source.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "result":
                    continue
                subtype = str(obj.get("subtype") or "")
                status = obj.get("api_error_status")
                status = status if isinstance(status, int) else None
                if subtype or status is not None:
                    return subtype, status
        return "", None

    @classmethod
    def _session_reply_text(cls, result: TaskResult) -> str:
        """User-facing text for Telegram session completions."""
        for candidate in (
            result.output,
            cls._extract_text_from_payload(result.parsed_output),
            result.raw_stdout,
        ):
            text = cls._extract_text_from_payload(candidate)
            if text:
                return text

        return (
            "Claude completed the run but returned no final reply text.\n\n"
            "Check the artifact JSON for raw stdout/stderr and backend metadata."
        )

    @classmethod
    def _failure_text(cls, result: TaskResult) -> str:
        """Aggregate likely error-bearing text from the result payload."""
        parts: List[str] = []

        def _append(value: Any) -> None:
            if value is None:
                return
            text = cls._extract_text_from_payload(value)
            if text:
                parts.append(text)
            elif isinstance(value, str) and value.strip():
                parts.append(value.strip())

        for err in (result.errors or []):
            _append(err)
        _append(getattr(result, "raw_stderr", ""))
        _append(getattr(result, "raw_stdout", ""))
        _append(getattr(result, "parsed_output", None))
        _append(getattr(result, "output", ""))
        return "\n".join(parts)

    @classmethod
    def _short_failure_reason(cls, result: TaskResult) -> str:
        """Return a concise, user-facing failure reason."""
        if result.success:
            return ""

        texts: List[str] = [str(err).strip() for err in (result.errors or []) if str(err).strip()]
        haystack = cls._failure_text(result)
        haystack_lower = haystack.lower()

        if "cancelled" in haystack_lower:
            return "Task cancelled"
        if cls._is_missing_backend_conversation(result):
            return "Claude session expired"
        if any(s in haystack_lower for s in ("rate_limit_event", "rate limit", "rate-limit", "too many requests", "hit your limit", "hit your session limit", "session limit", "usage limit", "\"error\":\"rate_limit\"", "overagestatus")):
            info = cls._extract_rate_limit_info(result)
            if info:
                limit_type = info.get("rateLimitType", "")
                resets_at = info.get("resetsAt")
                type_label = {"five_hour": "5-hour", "hourly": "hourly", "daily": "daily"}.get(limit_type, limit_type.replace("_", "-") if limit_type else "")
                prefix = f"Claude {type_label} usage limit reached" if type_label else "Claude usage limit reached"
                if resets_at:
                    try:
                        reset_dt = datetime.fromtimestamp(int(resets_at))
                        reset_str = reset_dt.strftime("%H:%M")
                        return f"{prefix} — resets at {reset_str}"
                    except Exception:
                        pass
                reset_match = re.search(r"resets?\s+([^\n\"\}·]{1,50})", haystack, flags=re.IGNORECASE)
                if reset_match:
                    return f"{prefix} — resets {reset_match.group(1).strip()}"
                return prefix
            reset_match = re.search(r"resets?\s+([^\n\"\}·]{1,50})", haystack, flags=re.IGNORECASE)
            if reset_match:
                return f"Claude usage limit reached — resets {reset_match.group(1).strip()}"
            return "Claude usage limit reached"
        if any(s in haystack_lower for s in ("prompt is too long", "blocking_limit", "context_window", "context window")):
            return "Session context full — use /compact or start a new session"
        if any(s in haystack_lower for s in ("not logged in", "authentication", "unauthorized", "forbidden")):
            return "Claude authentication error"
        if any(s in haystack_lower for s in ("timeout", "timed out", "inactivity")):
            # Pass through the richer error text when it's already actionable
            for t in texts:
                tl = t.lower()
                if "timed out" in tl or "timeout" in tl or "inactivity" in tl:
                    compact = " ".join(t.split())
                    if len(compact) > 20:
                        return compact[:300]
            return "Claude timeout"
        if any(s in haystack_lower for s in ("connection reset", "connection aborted", "network error", "temporarily unavailable", "service unavailable")):
            return "Claude network error"
        if any(isinstance(e, str) and "interactive_prompt_detected" in e for e in (result.errors or [])):
            return "Claude needs interactive approval"

        exit_code_text: Optional[str] = None
        for text in texts:
            low = text.lower()
            if low.startswith("claude exited with code "):
                exit_code_text = text  # defer — prefer any richer error first
                continue
            compact = " ".join(text.split())
            if compact:
                return compact[:120]

        # Surface raw_stderr before giving up — it often holds the real reason
        raw_stderr = (getattr(result, "raw_stderr", "") or "").strip()
        if raw_stderr:
            first_line = next((ln.strip() for ln in raw_stderr.splitlines() if ln.strip()), "")
            if first_line:
                suffix = f" ({exit_code_text})" if exit_code_text else ""
                return f"{first_line[:200]}{suffix}"

        # At minimum, be honest about the exit code instead of "Claude failed"
        if exit_code_text:
            return exit_code_text[:120]

        return "Claude failed"

    # ===========================================================================
    # BACKEND RESOLUTION
    # Thin helpers to map a task/session to the backend name string used by the
    # rest of the routing layer.
    # ===========================================================================

    def _resolve_task_backend(self, task: Task) -> str:
        """Resolve the backend associated with a task before it finishes."""
        session_id = (task.metadata or {}).get("session_id", "").strip()
        if session_id:
            session = self.session_store.get(session_id)
            if session and session.backend:
                return str(session.backend).strip().lower()
        backend_name = str((task.metadata or {}).get("backend") or "claude").strip().lower()
        return backend_name or "claude"

    @staticmethod
    def _backend_event_name(backend_name: str, phase: str) -> str:
        backend = (backend_name or "claude").strip().lower() or "claude"
        return f"{backend}_{phase}"

    # ===========================================================================
    # STARTUP RECOVERY
    # Called once during start() to heal sessions and tasks that were in-flight
    # when the gateway last exited. Two passes:
    #   1. Mark SDK-driver sessions on THIS host as driver_lost (they cannot be
    #      resumed — the driver process is gone).
    #   2. Scan BUSY sessions that have a completed mesh_tasks row and deliver
    #      the result as if the task just finished (_recover_completed_session).
    # ===========================================================================

    def _mark_local_claude_driver_sessions_lost_after_restart(self, host: str) -> int:
        """A gateway restart orphaned live SDK clients owned by this process."""
        marked = 0
        for session in self.session_store.list_all():
            if session.backend != "claude":
                continue
            if session.driver_type != "sdk" or session.driver_status != "live":
                continue
            if session.machine_id not in ("", host):
                continue
            if session.status not in (SessionStatus.IDLE, SessionStatus.AWAITING_INPUT):
                continue
            session.driver_status = "lost"
            # touch=False: marking a pooled SDK client lost after a restart is
            # internal plumbing, NOT operator activity — it must not rewrite
            # updated_at (that's what floated every open conversation to "now"
            # on restart).
            self.session_store.save(session, touch=False)
            marked += 1
        if marked:
            logger.warning("event=local_driver_sessions_marked_lost host=%s count=%d", host, marked)
        backend = self._backends.get("claude")
        marker = getattr(backend, "mark_sessions_lost", None)
        if callable(marker):
            with contextlib.suppress(Exception):
                marker()
        return marked

    async def _recover_stale_busy_sessions(self) -> None:
        """Recover BUSY sessions after a gateway restart.

        Uses the DB to distinguish three cases instead of blindly marking ERROR:
        1. Task completed in DB → restore session to AWAITING_INPUT, propagate result.
        2. Task still pending/claimed in DB → skip (worker will finish it).
        3. No DB record or DB unavailable → mark ERROR (legacy fallback).

        When DB is unavailable, falls back to the original behaviour: mark all
        stale BUSY sessions as ERROR.
        """
        host = socket.gethostname()
        active_task_ids = set(self.active_tasks.keys())
        self._mark_local_claude_driver_sessions_lost_after_restart(host)

        db = None
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            pass

        for session in self.session_store.list_all():
            # A18: a session caught mid-hold (PAUSED_PINNED_NODE_OFFLINE) by a
            # gateway restart has lost its in-memory liveness poll. It was never
            # dispatched off-host (the hold polls *before* dispatch), so there is
            # nothing to reattach — surface the honest, resumable terminal state
            # so the operator can retry / re-pin, instead of wedging it in a
            # transient PAUSED state forever. (Never occurs while the feature is
            # disabled, i.e. MESH_AFFINITY_OFFLINE_GRACE_SEC=0.)
            if session.status == SessionStatus.PAUSED_PINNED_NODE_OFFLINE:
                session.status = SessionStatus.PINNED_NODE_OFFLINE
                session.last_result_summary = (
                    "Pinned node was offline and the gateway restarted during the "
                    "affinity hold; retry when the node is back, or re-pin."
                )
                self.session_store.save(session)
                self._emit_event("affinity_hold_interrupted_by_restart", None, {
                    "session_id": session.session_id,
                    "machine_id": session.machine_id,
                })
                continue
            if session.status != SessionStatus.BUSY:
                continue
            is_remote = bool(session.machine_id and session.machine_id != host)
            if session.last_task_id and session.last_task_id in active_task_ids:
                continue

            task_id = session.last_task_id
            if db is not None and task_id:
                row = db.get_task(task_id)
                if row:
                    status = row.get("status")
                    if status == "completed":
                        # Task completed successfully while gateway was down.
                        # Restore session and propagate the result.
                        await self._recover_completed_session(session, row)
                        continue
                    elif status in ("pending", "claimed"):
                        # Worker is still working on it. For a remote (mesh)
                        # session the worker lives on another node and keeps
                        # running across our restart, so reattach a poll loop
                        # that will deliver its real result. For a local session
                        # the in-process worker is gone, so just defer.
                        if is_remote:
                            logger.info(
                                "event=session_recovery_reattach session_id=%s task_id=%s status=%s node=%s",
                                session.session_id, task_id, status, session.machine_id,
                            )
                            asyncio.create_task(self._reattach_remote_task(session, row))
                        else:
                            logger.info(
                                "event=session_recovery_deferred session_id=%s task_id=%s status=%s",
                                session.session_id, task_id, status,
                            )
                        continue
                    # Other terminal status (failed, failed_node_offline) — fall
                    # through to ERROR marking below.

            # A remote session with no usable DB row falls through to ERROR like
            # any other: we genuinely don't know the task's state.

            # No DB record available (or DB unavailable), or task failed.
            session.status = SessionStatus.ERROR
            session.last_result_summary = "Interrupted by gateway restart; partial changes may exist."
            self.session_store.save(session)
            result = TaskResult(
                task_id=task_id or f"session_{session.session_id}",
                success=False,
                output="",
                errors=["interrupted by gateway restart"],
                files_modified=[],
                execution_time=0.0,
                timestamp=now_iso(),
            )
            setattr(result, "backend_name", session.backend or "claude")
            self._write_session_summary(session, result)
            self._append_session_event(session.session_id, task_id or "", result)
            self._emit_event(
                "session_interrupted_recovered",
                None,
                {"session_id": session.session_id, "task_id": task_id, "backend": session.backend},
            )
            await self.notifier.notify_error(
                "Task interrupted by gateway restart",
                task_id=task_id or session.session_id,
                chat_id=session.telegram_chat_id,
            )

    async def _recover_completed_session(self, session: Any, task_row: Dict[str, Any]) -> None:
        """Restore a session whose task completed in DB while the gateway was down."""
        result_raw = task_row.get("result")
        result_dict: Dict[str, Any] = {}
        if result_raw:
            try:
                result_dict = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            except Exception:
                pass

        if not result_dict:
            logger.warning(
                "event=recovery_missing_result session_id=%s task_id=%s",
                session.session_id, task_row.get("id"),
            )

        exec_time = result_dict.get("execution_time", 0.0)
        if not isinstance(exec_time, (int, float)):
            exec_time = 0.0

        session.status = SessionStatus.AWAITING_INPUT
        full_out = (result_dict.get("output", "") or "").strip() or "Task completed (recovered)"
        session.last_result_summary = full_out[-400:] if len(full_out) > 400 else full_out
        session.last_files_modified = result_dict.get("files_modified") or []
        # Propagate the backend_session_id the worker established, exactly as the
        # live dispatch path does (_dispatch_to_node). Without this the recovered
        # session has no backend_session_id and the next turn can't resume the
        # remote Claude session — it would silently start a fresh one.
        recovered_bsid = result_dict.get("backend_session_id", "")
        if recovered_bsid:
            session.backend_session_id = recovered_bsid
        artifact_path = task_row.get("artifact_path") or ""
        if artifact_path:
            session.last_artifact_path = artifact_path
        session.task_history.append({
            "task_id": task_row["id"],
            "timestamp": result_dict.get("timestamp", now_iso()),
            "success": True,
            "execution_time": round(exec_time, 2),
            "user_message": session.last_user_message,
            "result_summary": full_out,
            "files_modified": session.last_files_modified[:20],
        })
        session.task_history = session.task_history[-20:]
        self.session_store.save(session)

        result = TaskResult(
            task_id=task_row["id"],
            success=True,
            output=result_dict.get("output", ""),
            errors=[],
            files_modified=result_dict.get("files_modified") or [],
            execution_time=exec_time,
            timestamp=now_iso(),
        )
        setattr(result, "backend_name", session.backend or "claude")
        self._write_session_summary(session, result)
        self._append_session_event(session.session_id, task_row["id"], result)
        self._emit_event(
            "session_recovered_completed",
            None,
            {"session_id": session.session_id, "task_id": task_row["id"], "backend": session.backend},
        )

        await self.notifier.notify_task_outcome(
            task_row["id"],
            result,
            session=session,
            chat_id=session.telegram_chat_id,
            prefix="_(recovered after a gateway restart)_\n\n",
        )

    # ===========================================================================
    # WAKE DISPATCHER — AUTONOMOUS CASE CONTINUATION  (M3.4)
    # Flag: CASE_CONTINUATION_ENABLED (default OFF ⇒ loop never starts).
    #
    # Allows a Manager to arm a wait-group over a dispatch set; when all members
    # finish the harness schedules ONE deterministic continuation row in
    # mesh_tasks (sentinel machine_id __manager_continuation__), atomically
    # claims it, and delivers ONE coalesced proactive review turn to the live
    # Manager session.  Bounded by round_cap; on exhaustion → flow.interrupted.
    #
    # Entry: _start_wake_dispatcher() (called from start())
    # Tick:  _wake_dispatcher_tick_once() → _continue_case_once() per open case
    # Land:  _finalize_continuation() → _notify_proactive_turn()
    # Kill:  interrupt_case() → sets case status=blocked, skipped next tick
    #
    # FUTURE EXTRACTION → CaseContinuationEngine (own file/class)
    #   Prerequisite: A54 (durable reconstruction) + A55 (crash-respawn) landed
    #   and proven live.  Do NOT extract mid-M3.4 — A54/A55 still touch these
    #   methods directly.  When stable, inject via:
    #     self._db_factory, self.session_store, self.notifier,
    #     self._notify_proactive_turn (callback), self._finalize_continuation.
    # ===========================================================================

    _CONTINUATION_TERMINAL_STATUSES = ("completed", "failed", "failed_node_offline")

    def _start_wake_dispatcher(self) -> None:
        """Start the periodic Wake-Dispatcher loop (mirrors the stale-busy
        reconciler). No-op unless mesh routing is active AND the continuation flag
        is ON — so with the flag OFF this is byte-identical to no loop at all."""
        from src.control.db import cache_heartbeat_active_enabled, case_continuation_enabled
        interval = int(getattr(config.mesh, "case_continuation_tick_interval_sec", 30) or 0)
        if (
            not config.mesh.enabled
            or interval <= 0
            or not (case_continuation_enabled() or cache_heartbeat_active_enabled())
        ):
            return
        if self._wake_dispatcher_task and not self._wake_dispatcher_task.done():
            return
        self._wake_dispatcher_task = asyncio.create_task(
            self._wake_dispatcher_loop(interval)
        )

    async def _wake_dispatcher_loop(self, interval_sec: int) -> None:
        logger.info("event=wake_dispatcher_started interval=%ds", interval_sec)
        try:
            while self.running:
                try:
                    await self._wake_dispatcher_tick_once()
                except Exception as e:
                    logger.debug("event=wake_dispatcher_tick_failed err=%s", e)
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            logger.info("event=wake_dispatcher_stopped")
            raise

    async def _wake_dispatcher_tick_once(self) -> int:
        """One Wake-Dispatcher pass over every open Case. Returns the number of
        proactive wake turns delivered this tick. Flag-gated (OFF ⇒ 0)."""
        from src.control.db import cache_heartbeat_active_enabled, case_continuation_enabled, get_db
        continuation_enabled = case_continuation_enabled()
        heartbeat_active = cache_heartbeat_active_enabled()
        if not continuation_enabled and not heartbeat_active:
            return 0
        try:
            db = get_db()
        except Exception:
            db = None
        if db is None:
            return 0
        delivered = 0
        if continuation_enabled:
            for case in db.list_open_cases():
                case_id = str(case.get("flow_run_id") or "")
                if not case_id:
                    continue
                try:
                    delivered += await self._continue_case_once(db, case_id)
                except Exception as e:
                    logger.debug("event=wake_dispatcher_case_failed case=%s err=%s", case_id, e)
        if heartbeat_active:
            try:
                delivered += await self._process_due_cache_heartbeats(db)
            except Exception as e:
                logger.debug("event=cache_heartbeat_tick_failed err=%s", e)
        return delivered

    def _cache_heartbeat_owner_live(self, db, owner: Dict[str, Any]) -> bool:
        reason = str(owner.get("reason") or "")
        owner_id = str(owner.get("owner_id") or "")
        if reason == "manual" or reason == "agent_requested":
            return True
        if reason == "watched_job":
            job = db.get_job(owner_id)
            return bool(job and str(job.get("status") or "") == "running")
        if reason == "case_wait_group":
            case_id, _, wait_group_id = owner_id.partition(":")
            if not case_id or not wait_group_id:
                return False
            row = db.get_flow_run(case_id)
            if row is not None and str(row.get("status") or "").strip().lower() in {"closed", "completed", "cancelled", "blocked"}:
                return False
            live = False
            for event in db.list_flow_events(case_id):
                if event.get("entity_type") != "wait_group" or event.get("entity_id") != wait_group_id:
                    continue
                if event.get("event_type") == "worker.wait_pending":
                    live = True
                elif event.get("event_type") == "worker.wait_resolved":
                    live = False
            return live
        return False

    def _sync_cache_heartbeat_state(self, db) -> None:
        db.expire_cache_heartbeat_state()
        for hb in db.list_cache_heartbeats(limit=50):
            if str(hb.get("status") or "") not in {"observe_only", "active"}:
                continue
            for owner in hb.get("owners") or []:
                if str(owner.get("status") or "") != "active":
                    continue
                if self._cache_heartbeat_owner_live(db, owner):
                    continue
                db.stop_cache_heartbeat_owner(
                    str(owner.get("session_id") or ""),
                    reason=str(owner.get("reason") or ""),
                    owner_type=str(owner.get("owner_type") or ""),
                    owner_id=str(owner.get("owner_id") or ""),
                    stop_reason="owner_no_longer_live",
                )

    def _cache_heartbeat_session_eligible(self, db, hb: Dict[str, Any]) -> Tuple[bool, str, Any]:
        session_id = str(hb.get("session_id") or "")
        heartbeat_id = str(hb.get("id") or "")
        session = self.session_store.get(session_id)
        if session is None:
            return False, "session_missing", None
        if session.backend != "claude":
            return False, "backend_not_claude", session
        if session.driver_type != "sdk":
            return False, "driver_not_sdk", session
        if not session.backend_session_id:
            return False, "missing_backend_session_id", session
        db.refresh_cache_heartbeat_from_recent_evidence(session_id)
        if heartbeat_id:
            refreshed = db.get_cache_heartbeat(heartbeat_id)
            next_due_at = str((refreshed or {}).get("next_due_at") or "")
            try:
                if next_due_at and parse_iso(next_due_at) > datetime.now(timezone.utc):
                    return False, "cache_fresh", session
            except Exception:
                pass
        if session.status != SessionStatus.AWAITING_INPUT:
            return False, "session_not_idle", session
        machine_id = str(getattr(session, "machine_id", "") or "")
        if machine_id:
            try:
                nodes = {str(n.get("node_id") or ""): n for n in db.list_nodes()}
                node = nodes.get(machine_id)
                if node is None or str(node.get("status") or "") != "online":
                    return False, "pinned_node_unavailable", session
            except Exception:
                return False, "pinned_node_unverified", session
        evidence = db.recent_cache_evidence(session_id)
        min_tokens = 0
        try:
            from src.control.db import cache_heartbeat_min_cache_tokens
            min_tokens = cache_heartbeat_min_cache_tokens()
        except Exception:
            min_tokens = 100_000
        token_sum = int((evidence or {}).get("cache_read_tokens") or 0) + int((evidence or {}).get("cache_creation_tokens") or 0)
        if token_sum < min_tokens:
            return False, "cache_below_threshold", session
        return True, "", session

    async def _process_due_cache_heartbeats(self, db) -> int:
        from src.control.db import (
            CACHE_HEARTBEAT_ACTION,
            CACHE_HEARTBEAT_MACHINE_SENTINEL,
            cache_heartbeat_active_enabled,
            cache_heartbeat_task_id,
        )
        if not cache_heartbeat_active_enabled():
            return 0
        db.refresh_cache_heartbeats_from_recent_evidence(limit=100)
        self._sync_cache_heartbeat_state(db)
        delivered = 0
        host = socket.gethostname()
        for hb in db.due_cache_heartbeats(limit=20):
            heartbeat_id = str(hb.get("id") or "")
            session_id = str(hb.get("session_id") or "")
            ok, reason, session = self._cache_heartbeat_session_eligible(db, hb)
            if not ok:
                if reason in {"session_missing", "backend_not_claude", "driver_not_sdk", "missing_backend_session_id"}:
                    db.stop_cache_heartbeat(heartbeat_id, reason)
                continue
            interval = max(60, int(hb.get("interval_sec") or 2700))
            slot_epoch = int(time.time() // interval) * interval
            lease_id = cache_heartbeat_task_id(session_id, slot_epoch)
            db.enqueue_task(
                lease_id,
                session_id=None,
                machine_id=CACHE_HEARTBEAT_MACHINE_SENTINEL,
                backend="claude",
                action=CACHE_HEARTBEAT_ACTION,
                payload={"heartbeat_id": heartbeat_id, "session_id": session_id, "slot_epoch": slot_epoch},
            )
            if not db.claim_task(lease_id, host):
                continue
            try:
                beat_number = int(hb.get("beat_count") or 0) + 1
                wake_task_id = await self.submit_instruction(
                    CACHE_HEARTBEAT_PROMPT,
                    session_id=session_id,
                    cwd=getattr(session, "repo_path", None),
                    source="cache_heartbeat",
                    extra_metadata={
                        "heartbeat_id": heartbeat_id,
                        "beat_number": beat_number,
                        "source": "cache_heartbeat",
                    },
                )
            except Exception as e:
                logger.warning("event=cache_heartbeat_deliver_failed heartbeat=%s err=%s", heartbeat_id, e)
                db.release_task(lease_id, host)
                continue
            db.record_cache_heartbeat_sent(heartbeat_id, wake_task_id)
            asyncio.create_task(self._finalize_cache_heartbeat(heartbeat_id, lease_id, wake_task_id, session_id))
            self._emit_event(
                "cache_heartbeat_delivered",
                None,
                {"heartbeat_id": heartbeat_id, "session_id": session_id, "task_id": wake_task_id},
            )
            delivered += 1
        return delivered

    async def _finalize_cache_heartbeat(
        self,
        heartbeat_id: str,
        lease_id: str,
        wake_task_id: str,
        session_id: str,
    ) -> None:
        from src.control.db import get_db
        try:
            db = get_db()
        except Exception:
            db = None
        if db is None:
            return
        deadline = time.time() + 180.0
        row: Optional[Dict[str, Any]] = None
        timed_out = True
        while self.running and time.time() < deadline:
            await asyncio.sleep(2)
            row = db.get_task(wake_task_id)
            if row is not None and row.get("status") in self._CONTINUATION_TERMINAL_STATUSES:
                timed_out = False
                break
            if row is None and wake_task_id not in self.active_tasks and wake_task_id in self.task_results:
                timed_out = False
                break
        if timed_out:
            # Still in flight past the poll window — this is not a failure, the
            # turn may land seconds later. Close out this lease without touching
            # heartbeat controller state so it isn't marked "failed" and stopped;
            # the deterministic slot_epoch lease bounds how often we retry.
            db.complete_task(
                lease_id,
                {"heartbeat_id": heartbeat_id, "wake_task_id": wake_task_id, "timed_out": True},
            )
            self._emit_event(
                "cache_heartbeat_poll_timeout",
                None,
                {"heartbeat_id": heartbeat_id, "session_id": session_id, "task_id": wake_task_id},
            )
            return
        result_dict: Dict[str, Any] = {}
        if row and row.get("result"):
            try:
                result_dict = json.loads(row.get("result") or "{}")
            except Exception:
                result_dict = {}
        result_obj = self.task_results.get(wake_task_id)
        usage = result_dict.get("usage") if isinstance(result_dict.get("usage"), dict) else {}
        if not usage and result_obj is not None and isinstance(getattr(result_obj, "usage", None), dict):
            usage = getattr(result_obj, "usage")
        success = bool(result_dict.get("success")) if result_dict else bool(getattr(result_obj, "success", False))
        output = str(result_dict.get("output") or getattr(result_obj, "output", "") or "")
        error_class = str(result_dict.get("error_class") or getattr(result_obj, "error_class", "") or "")
        cache_read = int((usage or {}).get("cache_read_input_tokens") or (usage or {}).get("cache_read") or 0)
        cache_creation = int((usage or {}).get("cache_creation_input_tokens") or (usage or {}).get("cache_creation") or 0)
        db.record_cache_heartbeat_result(
            heartbeat_id,
            wake_task_id,
            success=success,
            output=output,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            error_class=error_class,
        )
        db.complete_task(
            lease_id,
            {
                "heartbeat_id": heartbeat_id,
                "wake_task_id": wake_task_id,
                "success": success,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
            },
        )
        self._emit_event(
            "cache_heartbeat_recorded",
            None,
            {"heartbeat_id": heartbeat_id, "session_id": session_id, "task_id": wake_task_id, "success": success},
        )

    async def _continue_case_once(self, db, case_id: str) -> int:
        """Evaluate one Case: if a wait-group is satisfied, schedule + atomically
        claim the deterministic continuation row and deliver ONE coalesced wake
        turn to the bound live+idle Manager session. Returns 1 iff a turn was
        delivered, else 0. Enforces the round cap (escalates on exhaustion)."""
        from src.control.db import (
            CONTINUATION_MACHINE_SENTINEL, CONTINUATION_ACTION, continuation_task_id,
            _event_payload,
        )
        # [A53] A killed/interrupted Case (status 'blocked', set ONLY by the kill
        # path) is NOT auto-resumed by the Wake-Dispatcher — it awaits explicit
        # operator re-entry. Without this, cancelling in-flight workers would be
        # undone by the next satisfied-wait tick re-driving the very Case the
        # operator killed. ('blocked' has exactly one writer: interrupt_case.)
        _row = db.get_flow_run(case_id)
        if _row is not None and str(_row.get("status") or "").strip().lower() == "blocked":
            return 0
        # [quota-resume] A quota-PAUSED Case is not a normal Case this tick: while
        # the window is spent a wake would only burn another refused turn, and
        # once it reopens the RESUME (proposed/approved/auto) is what continues
        # it. Checked before satisfaction on purpose — the defect being fixed is
        # precisely that a paused Case could only ever come back if a wait-group
        # happened to satisfy later, at an unrelated moment.
        if await self._handle_quota_paused_case(db, case_id):
            return 0
        # [transient-resume] A transient-PAUSED Case (a Manager turn that died on a
        # 529 Overloaded) is not a normal Case this tick either: while the short
        # backoff is running a wake would just burn another overloaded turn, and
        # once it elapses the retry (not a wake) is what continues it. Checked
        # before satisfaction for the same reason the quota check is.
        if await self._handle_transient_paused_case(db, case_id):
            return 0
        tick = db.compute_continuation_tick(case_id)
        # [continuation-review-watermark] Retire one-shot groups the Manager already
        # drained by reviewing their members out-of-band (a tagged review.*, e.g.
        # during an operator poke that interleaved before the wake could fire). These
        # produce no wake — so without this they would dangle 'armed' forever and be
        # needlessly re-armed on a Manager resume. Discharge the obligation with a
        # plain wait_resolved marker (NO paid turn, NO round consumed). Idempotent:
        # once appended, the group carries a wait_resolved and leaves retire_only.
        for gid in tick.get("retire_only_groups", []) or []:
            db.append_flow_event(
                case_id, "worker.wait_resolved", "system",
                entity_type="wait_group", entity_id=gid,
                payload={"wait_group_id": gid, "outcome": "drained",
                         "reason": "reviewed_out_of_band"},
            )
            self._emit_event(
                "case_wait_group_review_drained", None,
                {"case_id": case_id, "wait_group_id": gid},
            )
        if not tick.get("satisfied"):
            return 0

        generation = int(tick["generation_next"])
        cap = db.case_round_cap(case_id)
        if generation > cap:
            # Round cap exhausted — escalate ONCE (idempotent: skip if already emitted).
            already = any(
                e.get("event_type") == "flow.interrupted"
                and (_event_payload(e) or {}).get("reason") == "round_cap_exhausted"
                for e in db.list_flow_events(case_id)
            )
            if not already:
                db.append_flow_event(
                    case_id, "flow.interrupted", "system",
                    payload={"reason": "round_cap_exhausted",
                             "round_cap": cap, "generation": generation},
                )
                self._emit_event(
                    "case_continuation_interrupted", None,
                    {"case_id": case_id, "round_cap": cap, "generation": generation},
                )
                await self._escalate_case_continuation_cap(case_id, cap, generation)
            return 0

        # The wake target must be a live Manager session that has RUN a turn and is
        # now WAITING for the next one — i.e. AWAITING_INPUT. A Manager that armed a
        # wait-group has by definition already run a turn, so that is the only state
        # a real wake target is ever in. IDLE is NOT a wake condition: it means a
        # freshly-created / just-reset / just-restored session that has never run a
        # turn (so it cannot own a satisfied group), and BUSY means a turn is already
        # in flight (the atomic claim below is the real single-flight gate; skipping
        # here just avoids a needless enqueue). A dead session is Job-3 territory
        # (crash-respawn). Requiring strictly IDLE — the original bug — made the
        # Wake-Dispatcher inert against every real Manager.
        session_id = db.case_manager_session_id(case_id)
        if not session_id:
            # Satisfied Case with NO manager link at all — headless. Surface it.
            await self._escalate_headless_case(db, case_id, None)
            return 0
        session = self.session_store.get(session_id)
        # A satisfied Case whose registered Manager session is GONE or CLOSED can
        # never self-continue: the wake would target a dead session and the finished
        # workers would strand SILENTLY (observed live 2026-08-01 — a live Manager on
        # a NEW case armed the wait on an OLD open case whose only manager link was a
        # closed session; the worker result surfaced to the operator, never the
        # Manager). Escalate ONCE instead of returning 0 in silence. A transient
        # non-waiting state (IDLE just-restored, BUSY mid-turn) is NOT a strand — it
        # resolves on its own — so only CLOSED/CANCELLED/missing escalates.
        if session is None or session.status in (
            SessionStatus.CLOSED, SessionStatus.CANCELLED,
        ):
            # [A55 / M3.4 Job 3] CRASH-RESPAWN. The bound Manager session is dead
            # (gone/closed) but the Case is genuinely open (the 'blocked' guard
            # above already excluded operator-halted Cases) and SATISFIED — its
            # finished workers would strand. Instead of only escalating, bring a
            # role-full Manager back ON THE SAME Case (reconstruct via A54's
            # get_case_brief, re-arm waits/groups, resume) under a strict
            # single-flight lease so a racing tick never double-respawns.
            if await self._handle_dead_manager_session(db, case_id, generation, session_id):
                # A respawn is owned for this Case this tick — either THIS tick won
                # the single-flight and respawned, or a concurrent tick did and we
                # lost the atomic claim. Either way a role-full Manager is (being)
                # brought back on the SAME Case; do NOT also escalate a strand or
                # enqueue a wake on the dead session_id. The new session boots and
                # is woken next tick (AWAITING_INPUT after its resume turn).
                return 0
            # Respawn genuinely not viable (continuation off — unreachable here —
            # or no placement node / spawn failed): fall through to the visible
            # strand escalation exactly as before A55.
            await self._escalate_headless_case(db, case_id, session_id)
            return 0
        if session.status != SessionStatus.AWAITING_INPUT:
            return 0

        cont_id = continuation_task_id(case_id, generation)
        presented = list(tick.get("presented_task_ids") or [])
        # Idempotent enqueue: a racing tick computes the SAME id ⇒ UNIQUE collapses
        # to one row. The continuation row is pinned to the reserved sentinel so no
        # worker/embedded claim scan can ever see it.
        # session_id is NULL on the row: a continuation is a scheduling TOKEN, not a
        # conversation turn — coupling it to the sessions FK would be wrong. The wake
        # target rides in the payload instead.
        db.enqueue_task(
            cont_id,
            session_id=None,
            machine_id=CONTINUATION_MACHINE_SENTINEL,
            backend=(session.backend or "claude"),
            action=CONTINUATION_ACTION,
            payload={"case_id": case_id, "generation": generation,
                     "session_id": session_id, "presented_task_ids": presented},
        )
        # Atomic lease — single winner. A racing dispatcher (or a redelivery while
        # the claim is still live) gets False and stops. No delivery on a lost claim.
        if not db.claim_task(cont_id, socket.gethostname()):
            return 0

        wake = self._render_wake_turn(case_id, presented)
        retired = [g["wait_group_id"] for g in tick.get("satisfied_groups", []) if g.get("retire")]
        try:
            wake_task_id = await self.submit_instruction(
                description=wake,
                session_id=session_id,
                cwd=session.repo_path,
                source="manager_continuation",
            )
        except Exception as e:
            # Delivery failed after the claim — release the lease so the next tick
            # can retry cleanly rather than stranding the row 'claimed'.
            logger.warning("event=wake_deliver_failed case=%s err=%s", case_id, e)
            db.release_task(cont_id, socket.gethostname())
            return 0

        # HARNESS-record consumption when the proactive turn returns (State 4).
        asyncio.create_task(self._finalize_continuation(
            case_id, cont_id, generation, presented, retired, wake_task_id, session_id,
        ))
        self._emit_event(
            "case_continuation_delivered", None,
            {"case_id": case_id, "generation": generation,
             "presented_task_ids": presented, "continuation_id": cont_id},
        )
        return 1

    def _render_wake_turn(self, case_id: str, presented: List[str]) -> str:
        """Compose the ONE coalesced Case-level wake message. Presents ALL
        newly-finished-unconsumed workers as a single turn (not one per worker)."""
        ids = ", ".join(presented) if presented else "(none)"
        return (
            "[continuation] Worker completion(s) are ready for your review on this "
            f"Case ({case_id}). Finished since your last turn: {ids}.\n"
            "Run your review gate IN ORDER — do not accept-and-relay. This first return "
            "is a DRAFT to challenge, not a result to forward. FIRST apply Gate 0 "
            "(relevance before rigor): does this delivery actually move THIS Case's "
            "objective, and can its output do what it claims? A rigorous answer to the "
            "wrong question is rework, not accept — a Gate 0 failure regardless of how "
            "diligent the work is. If it conflicts with the objective, redirect the "
            "worker with the corrected framing and report the redirect rather than asking "
            "permission for the obvious. THEN, only for work that passes Gate 0, verify "
            "the committed diff in git, score the six dimensions, record_review, and "
            "dispatch the next task / wait on remaining workers / close the Case if its "
            "completion_criteria are met. This turn was delivered autonomously by the "
            "harness — treat it exactly like an operator poke to continue the Case."
        )

    def _ensure_approval_service(self, db) -> None:
        """Lazily construct self.approval_service (DB readiness at __init__ time
        is not guaranteed — mirrors the quota_coordinator lazy-init pattern)."""
        if self.approval_service is not None:
            return
        from src.services.approval_service import ApprovalService
        self.approval_service = ApprovalService(
            db, on_approve=self._on_case_approval_resolved,
        )

    async def _on_case_approval_resolved(self, row: Dict[str, Any]) -> None:
        """ApprovalService on_approve callback: the operator's decision is what
        RUNS the gated Case action (the service is a durable queue, not a blocked
        coroutine). Routes by ``action``; anything else on the shared approval
        service is not ours — ignore it."""
        action = str(row.get("action") or "")
        if action not in (CASE_RESPAWN_APPROVAL_ACTION, CASE_RESUME_APPROVAL_ACTION):
            return
        try:
            payload = json.loads(row.get("payload") or "{}")
        except Exception:
            payload = {}
        case_id = str(payload.get("case_id") or "")
        if not case_id:
            logger.warning(
                "event=case_approval_payload_invalid action=%s approval_id=%s",
                action, row.get("id"),
            )
            return
        if action == CASE_RESUME_APPROVAL_ACTION:
            await self.resume_case(
                case_id,
                mode=str(payload.get("mode") or "") or None,
                actor="operator_approval",
                paused_task_id=str(payload.get("paused_task_id") or "") or None,
            )
            return
        generation = payload.get("generation")
        if generation is None:
            logger.warning(
                "event=respawn_approval_payload_invalid approval_id=%s", row.get("id"),
            )
            return
        from src.control.db import get_db
        db = get_db()
        await self._do_respawn_manager_for_case(
            db, case_id, int(generation), payload.get("dead_session_id"),
        )

    def _find_case_generation_approval(
        self, action: str, case_id: str, generation: int,
    ) -> Optional[Dict[str, Any]]:
        """Find any prior approval (pending OR resolved) for this exact
        (case_id, generation) — dedup key. Checking only 'pending' would let a
        REJECTED approval get silently re-requested on the very next tick, since
        nothing advances `generation` while the Manager stays dead."""
        if self.approval_service is None:
            return None
        for row in self.approval_service.list(limit=200):
            if row.get("action") != action:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:
                continue
            if payload.get("case_id") == case_id and payload.get("generation") == generation:
                return row
        return None

    def quota_window_state(self, provider: str = "claude") -> Dict[str, Any]:
        """[quota-resume] The harness's honest answer to "is this provider's quota
        window spent, and when does it reopen?".

        Reads the quota coordinator's LATEST SNAPSHOTS (not ``window_states``):
        the snapshot row is the only place ``limit_reached`` — the provider's own
        "you are cut off" bit — survives, and it also carries ``reset_at``.
        ``window_states`` derives a stricter ``telemetry_state`` used for
        AUTOMATION READINESS; using it here would call quota "restored" merely
        because the observer is between polls.

        Uses the store's bounded ``latest_snapshots()`` (one indexed row per
        bucket) rather than ``status()``, which additionally builds per-bucket
        reset HISTORY — fine for the diagnostics endpoint, far too heavy for a
        path that runs on every Wake-Dispatcher tick.

        Returns ``{exhausted, reset_at, observed_at, used_percent, bucket_id,
        evidence}``. ``evidence`` names WHY, and is what the UI shows the
        operator:
          * ``limit_reached``     — provider says the window is spent (authoritative)
          * ``reset_at_future``   — spent, and the recorded reset is still ahead
          * ``below_limit``       — observed under the limit ⇒ usable
          * ``reset_elapsed``     — the last spent reading's reset time has passed
          * ``no_telemetry``      — coordinator off/unavailable: NOT exhausted
        A missing instrument returns ``exhausted=False``: waiting forever on an
        instrument that may never report again is worse than proposing a resume
        the operator can decline.
        """
        out: Dict[str, Any] = {
            "provider": provider, "exhausted": False, "reset_at": None,
            "observed_at": None, "used_percent": None, "bucket_id": None,
            "evidence": "no_telemetry",
        }
        coord = getattr(self, "quota_coordinator", None)
        store = getattr(coord, "store", None) if coord is not None else None
        if store is None:
            return out
        try:
            reader = getattr(store, "latest_snapshots", None)
            snapshots = (
                reader() if callable(reader)
                else (store.status().get("latest_snapshots") or [])
            )
        except Exception:
            return out
        now = datetime.now(timezone.utc)
        best: Optional[Dict[str, Any]] = None
        for s in snapshots:
            if s.get("provider") != provider:
                continue
            spent = bool(s.get("limit_reached")) or (
                isinstance(s.get("used_percent"), (int, float)) and s["used_percent"] >= 100
            )
            if not spent:
                if best is None:
                    best = {**s, "_spent": False}
                continue
            # A spent bucket always wins: ANY exhausted window blocks the account.
            if best is None or not best.get("_spent"):
                best = {**s, "_spent": True}
        if best is None:
            return out
        reset_at = _parse_iso_utc(best.get("reset_at"))
        out.update({
            "reset_at": best.get("reset_at"),
            "observed_at": best.get("observed_at"),
            "used_percent": best.get("used_percent"),
            "bucket_id": best.get("bucket_id"),
        })
        if not best.get("_spent"):
            out["evidence"] = "below_limit"
            return out
        if reset_at is not None and reset_at <= now:
            # The spent reading is older than its own reset time — the window has
            # rolled over even if the observer has not polled since.
            out["evidence"] = "reset_elapsed"
            return out
        out["exhausted"] = True
        out["evidence"] = "limit_reached" if best.get("limit_reached") else "reset_at_future"
        return out

    def _quota_still_exhausted_for_claude(self) -> bool:
        """True ONLY when telemetry positively says a Claude window is still spent.
        Absence/staleness of that evidence returns False — fail OPEN to asking the
        operator rather than waiting forever on a silent instrument."""
        return bool(self.quota_window_state("claude").get("exhausted"))

    # =======================================================================
    # [quota-resume] QUOTA PAUSE → RESTORE → RESUME
    #
    # A Manager turn that dies on `usage_limit` is not a failure of anything:
    # the provider refused the turn until the account's window reopens. Before
    # this seam the harness had exactly ONE resume trigger — a satisfied
    # wait-group — so a quota-killed Case either stalled silently forever (no
    # workers in flight) or came back at an unrelated random moment (whenever
    # some worker happened to finish). Neither is a decision anyone made.
    #
    # The seam splits that into three honest parts:
    #   1. PAUSE     — durable, on the Case ledger (`flow.quota_paused`), so it
    #                  survives a gateway restart and is visible in the UI.
    #   2. RESTORE   — a telemetry fact (quota_window_state), not a timer.
    #   3. RESUME    — an ECONOMIC decision, because resuming a fat Manager
    #                  session re-writes its whole prompt cache (observed
    #                  200-300k tokens ≈ real money) and may buy nothing but the
    #                  words "case is closed". So restore does not resume: it
    #                  PROPOSES, with a cost estimate, and the operator decides
    #                  (or CASE_QUOTA_RESUME_AUTO decides, under a USD ceiling).
    #
    # Every entry point — auto-restore, approval, the operator's Resume button —
    # funnels into ONE leased `resume_case`, which is what structurally prevents
    # the observed "operator started a session, then the engine started another".
    # =======================================================================

    def _rate_limit_reset_iso(self, result: TaskResult) -> Optional[str]:
        """The reset instant the PROVIDER attached to its own refusal
        (``rate_limit_event.rate_limit_info.resetsAt``, epoch seconds), as UTC
        ISO — or None. This is ground truth about one account's window and needs
        no observer running."""
        info = self._extract_rate_limit_info(result) or {}
        raw = info.get("resetsAt")
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def _usage_limit_class(self, result: TaskResult) -> str:
        """Classify as ``usage_limit`` AND teach the quota store the boundary the
        provider attached to its own refusal (A78 §3). A 429 is the most exact
        quota signal that exists — it used to be thrown away, leaving the store
        to infer boundaries from polls. Best-effort/isolated: a telemetry write
        must never alter error classification or task execution. Idempotent per
        boundary inside the store."""
        try:
            from src.services.quota_window_coordinator import normalize_utc
            reset_iso = self._rate_limit_reset_iso(result)
            if reset_iso:
                coord = getattr(self, "quota_coordinator", None)
                store = getattr(coord, "store", None) if coord is not None else None
                if store is not None and hasattr(store, "record_refusal_snapshot"):
                    stored = store.record_refusal_snapshot(
                        provider=str(getattr(result, "backend", "") or "claude").lower(),
                        reset_at=normalize_utc(reset_iso),
                    )
                    if stored:
                        logger.info(
                            "event=quota_refusal_recorded backend=%s reset_at=%s",
                            getattr(result, "backend", "") or "claude", reset_iso,
                        )
        except Exception as e:
            logger.warning("event=quota_refusal_record_failed err=%s", e)
        return "usage_limit"

    def _record_quota_pause(self, task: "Task", result: TaskResult) -> None:
        """Record a Manager turn's quota death as a durable Case pause.

        Best-effort/isolated exactly like ``_flow_terminal_outcome`` (whose call
        site this mirrors): a ledger write can NEVER raise into task execution.
        No-op unless the flag is ON, the turn is a real quota pause, the task
        belongs to a Case, and the session that ran it IS that Case's Manager —
        a worker hitting quota is the worker's own retry problem, not a reason to
        propose bringing a Manager back.
        """
        try:
            from src.control.db import case_quota_resume_enabled, get_db
            if not self._harness_flow_drive_enabled() or not case_quota_resume_enabled():
                return
            if not is_quota_pause_result(result):
                return
            meta = getattr(task, "metadata", None) or {}
            case_id = meta.get(self._FLOW_RUN_META_KEY) or meta.get(self._CASE_ID_META_KEY)
            session_id = str(meta.get("session_id") or "").strip()
            if not case_id or not session_id:
                return
            db = get_db()
            if db is None:
                return
            if db.case_manager_session_id(case_id) != session_id:
                return
            if db.case_quota_pause(case_id) is not None:
                # Already paused (a second turn can fail the same way before the
                # window reopens) — one open pause per Case, keyed on the FIRST
                # paused task so the resume lease stays deterministic.
                return
            quota = self.quota_window_state(str(getattr(result, "backend", "") or "claude"))
            reason = self._short_failure_reason(result)[:256]
            # THREE INDEPENDENT SOURCES NAME THE SAME INSTANT. Take whichever is
            # present, in descending order of directness — on 2026-08-19 all
            # three said 17:20:00Z and the Case still recorded `reset_at: null`,
            # which is what proposed a resume an hour early:
            #   1. the structured refusal (`rate_limit_event.resetsAt`) — exact,
            #      needs no observer, but ABSENT on the mesh/remote-node path;
            #   2. the refusal's own words ("resets 8:20pm (Europe/Kiev)") — the
            #      same fact, and the only one available when 1 is missing;
            #   3. the observer's five-hour `reset_at`.
            #
            # Source 3 used to be gated on `quota["exhausted"]`, on the theory
            # that a healthy reading's reset_at would "park the Case until a
            # boundary that never blocked anything". That was the live defect: an
            # ANCHORED window's boundary does not move when utilisation changes,
            # so the healthy 15:39 reading (68% used) and the exhausted 17:05 one
            # named the SAME 17:20:00Z. What makes a boundary blocking is the
            # provider refusing this turn — which just happened — not whether our
            # last poll happened to catch 100%. A future reset instant is
            # therefore taken as-is; a past one is discarded (it belongs to a
            # window that has already rolled over).
            telemetry_parsed = _parse_iso_utc(quota.get("reset_at"))
            telemetry_reset = (
                quota.get("reset_at")
                if telemetry_parsed is not None
                and telemetry_parsed > datetime.now(timezone.utc)
                else None
            )
            reset_at = (
                self._rate_limit_reset_iso(result)
                or _reset_at_from_limit_text(reason)
                or telemetry_reset
            )
            db.append_flow_event(
                case_id, "flow.quota_paused", "system",
                entity_type="task", entity_id=getattr(task, "id", None),
                payload={
                    "session_id": session_id,
                    "paused_task_id": getattr(task, "id", None),
                    "error_class": str(getattr(result, "error_class", "") or ""),
                    "provider": quota.get("provider") or "claude",
                    "reset_at": reset_at,
                    "quota_evidence": quota.get("evidence"),
                    "reason": reason,
                },
            )
            self._emit_event(
                "case_quota_paused", task,
                {"case_id": case_id, "session_id": session_id, "reset_at": reset_at},
            )
            logger.info(
                "event=case_quota_paused case=%s session=%s task=%s reset_at=%s",
                case_id, session_id, getattr(task, "id", "?"), reset_at,
            )
        except Exception as e:
            logger.warning(
                "event=case_quota_pause_record_failed task_id=%s err=%s",
                getattr(task, "id", "?"), e,
            )

    def _record_transient_pause(self, task: "Task", result: TaskResult) -> None:
        """[transient-resume] Record a Manager turn's terminal transient-5xx death
        as a durable, bounded, self-healing Case pause.

        Mirrors ``_record_quota_pause`` (same call site, same Manager-only scope,
        same best-effort/isolated contract — a ledger write can NEVER raise into
        task execution). Differences, all because a 529 is an ephemeral overload
        rather than a spent window:

          * restore is a short escalating FIXED backoff (no ``resetsAt`` exists),
            and the retry is automatic + free (no cache-rewrite cost, no approval);
          * the retry is BOUNDED — the attempt number is counted over a rolling
            window; once it exceeds the backoff schedule the budget is spent, so a
            provider that is down for real escalates instead of looping forever.

        The failed turn's own instruction is captured (capped) so the retry
        re-runs the EXACT turn — a fresh operator instruction that 529'd must not
        be silently replaced by a generic 'continue from the ledger' turn.
        """
        try:
            from src.control.db import get_db, transient_provider_resume_enabled
            if not self._harness_flow_drive_enabled() or not transient_provider_resume_enabled():
                return
            if not is_transient_provider_pause_result(result):
                return
            meta = getattr(task, "metadata", None) or {}
            case_id = meta.get(self._FLOW_RUN_META_KEY) or meta.get(self._CASE_ID_META_KEY)
            session_id = str(meta.get("session_id") or "").strip()
            if not case_id or not session_id:
                return
            db = get_db()
            if db is None:
                return
            if db.case_manager_session_id(case_id) != session_id:
                # A worker (not this Case's Manager) hitting a 5xx is the worker's
                # own retry problem, not a reason to pause a Manager Case.
                return
            if db.transient_pause(case_id) is not None:
                # One open pause per Case: a second turn can fail the same way
                # before the backoff fires — the first pause already owns the retry.
                return
            now = datetime.now(timezone.utc)
            since_iso = (now - timedelta(seconds=TRANSIENT_PAUSE_WINDOW_SEC)).isoformat()
            prior = db.recent_transient_pause_count(case_id, since_iso)
            attempt = prior + 1
            ceiling = len(TRANSIENT_PAUSE_BACKOFF_SEC)
            task_id = getattr(task, "id", None)
            error_class = str(getattr(result, "error_class", "") or "")
            if attempt > ceiling:
                # Retry budget spent for this window — the overload is not
                # transient. Stop auto-retrying, escalate ONCE, and leave the
                # session AWAITING_INPUT so the operator can still poke it manually.
                db.append_flow_event(
                    case_id, "flow.transient_pause_exhausted", "system",
                    entity_type="task", entity_id=task_id,
                    payload={
                        "session_id": session_id, "attempts": prior,
                        "error_class": error_class,
                        "reason": self._short_failure_reason(result)[:256],
                    },
                )
                self._emit_event(
                    "case_transient_pause_exhausted", task,
                    {"case_id": case_id, "session_id": session_id, "attempts": prior},
                )
                logger.warning(
                    "event=case_transient_pause_exhausted case=%s session=%s attempts=%s",
                    case_id, session_id, prior,
                )
                return
            backoff = TRANSIENT_PAUSE_BACKOFF_SEC[min(attempt - 1, ceiling - 1)]
            retry_at = (now + timedelta(seconds=backoff)).isoformat()
            failed_prompt = str(getattr(task, "description", "") or "")
            truncated = len(failed_prompt) > _TRANSIENT_FAILED_PROMPT_CAP
            db.append_flow_event(
                case_id, "flow.transient_paused", "system",
                entity_type="task", entity_id=task_id,
                payload={
                    "session_id": session_id,
                    "paused_task_id": task_id,
                    "error_class": error_class,
                    "attempt": attempt,
                    "backoff_sec": backoff,
                    "retry_at": retry_at,
                    "failed_prompt": failed_prompt[:_TRANSIENT_FAILED_PROMPT_CAP],
                    "failed_prompt_truncated": truncated,
                    "reason": self._short_failure_reason(result)[:256],
                },
            )
            self._emit_event(
                "case_transient_paused", task,
                {"case_id": case_id, "session_id": session_id,
                 "attempt": attempt, "retry_at": retry_at, "backoff_sec": backoff},
            )
            logger.info(
                "event=case_transient_paused case=%s session=%s task=%s attempt=%s retry_at=%s",
                case_id, session_id, task_id, attempt, retry_at,
            )
        except Exception as e:
            logger.warning(
                "event=case_transient_pause_record_failed task_id=%s err=%s",
                getattr(task, "id", "?"), e,
            )

    def estimate_case_resume_cost(self, session_id: Optional[str]) -> Dict[str, Any]:
        """[quota-resume] What resuming THIS session is likely to cost, and how we
        know — the number that turns "resume?" into a decision.

        The dominant cost of resuming hours later is the prompt-cache REWRITE:
        the provider's cache TTL (~1h) has long expired, so the entire
        conversation is billed again at the cache-write rate. We do not have
        per-request context telemetry (``usage_granularity='invocation_total'``,
        ``usage_coverage='aggregate_only'``), so the context size is NOT directly
        observable. What IS observed is the largest ``cache_creation_tokens``
        this session recently wrote — see ``MeshDB.recent_cache_write``. That is
        used as the estimate and labelled as such
        (``basis='max_recent_turn_cache_creation'``), never dressed up as a
        measurement.

        Honesty rules: an unpriceable model or absent telemetry yields
        ``known=False`` + a reason, never a fabricated number.
        """
        out: Dict[str, Any] = {
            "known": False, "reason": "no_telemetry", "session_id": session_id,
            "model": None, "cache_creation_tokens": None, "usd": None,
            "basis": "max_recent_turn_cache_creation",
        }
        if not session_id:
            out["reason"] = "no_session"
            return out
        try:
            from src.control.db import get_db
            from src.services.pricing import TokenTotals, estimate_cost
            db = get_db()
            if db is None:
                out["reason"] = "db_unavailable"
                return out
            row = db.recent_cache_write(session_id)
            session = self.session_store.get(session_id)
            model = getattr(session, "model", None) if session is not None else None
            out["model"] = model
            if row is None:
                out["reason"] = "no_recorded_turn"
                return out
            tokens = int(row.get("cache_creation") or 0)
            out["cache_creation_tokens"] = tokens
            out["observed_at"] = row.get("observed_at")
            cost = estimate_cost(model or row.get("model"), TokenTotals(cache_creation=tokens))
            if not getattr(cost, "known", False):
                out["reason"] = getattr(cost, "reason", "model_not_priced") or "model_not_priced"
                return out
            out["known"] = True
            out["reason"] = ""
            out["usd"] = getattr(cost, "usd_cache_write", None)
            return out
        except Exception as e:
            logger.debug("event=resume_estimate_failed session=%s err=%s", session_id, e)
            out["reason"] = "estimate_failed"
            return out

    def _recommended_resume_mode(
        self, session_id: Optional[str], *, paused_at: Optional[str] = None,
    ) -> str:
        """Which resume mode the harness would pick.

        ``fresh_manager`` whenever the old session cannot carry the work (gone,
        closed, cancelled, errored) — a dead session must be reconstructed from
        the ledger, there is nothing to decide.

        For a LIVE session the choice is economic, and the deciding quantity is
        the prompt cache. Resuming ``in_place`` costs nothing extra while the
        provider's cache is still warm (~1h TTL), and stays cheap for a small
        conversation even after it goes cold. It becomes the expensive mode
        exactly when a fat session is resumed hours later — the 200-300k-token
        rewrite this seam exists to avoid. So: in_place when the pause is younger
        than the cache TTL **or** the session's recent cache writes are small;
        otherwise fresh_manager, rebuilt from the Case brief.

        ``paused_at`` is the pause's ledger timestamp; without it (an ordinary
        operator poke, no pause) only the size rule applies.
        """
        session = self.session_store.get(session_id) if session_id else None
        if session is None or session.status in (
            SessionStatus.CLOSED, SessionStatus.CANCELLED, SessionStatus.ERROR,
        ):
            return "fresh_manager"
        paused_dt = _parse_iso_utc(paused_at)
        if paused_dt is not None:
            age_sec = (datetime.now(timezone.utc) - paused_dt).total_seconds()
            if 0 <= age_sec < RESUME_IN_PLACE_CACHE_WARM_SEC:
                return "in_place"          # cache still warm — nothing to rewrite
        tokens = self.estimate_case_resume_cost(session_id).get("cache_creation_tokens")
        if isinstance(tokens, int) and tokens > RESUME_IN_PLACE_MAX_CACHE_TOKENS:
            return "fresh_manager"         # cold + fat ⇒ the expensive case
        if tokens is None:
            # Unmeasured session: assume the expensive case rather than spend on
            # an unknown. fresh_manager loses transcript detail, never money.
            return "fresh_manager"
        return "in_place"

    def _find_case_pause_approval(
        self, case_id: str, paused_task_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Any prior resume approval (pending OR resolved) for this exact pause.
        Keyed on the paused task: checking only 'pending' would let a REJECTED
        proposal be re-raised on the very next tick."""
        if self.approval_service is None:
            return None
        for row in self.approval_service.list(limit=200):
            if row.get("action") != CASE_RESUME_APPROVAL_ACTION:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:
                continue
            if (
                payload.get("case_id") == case_id
                and str(payload.get("paused_task_id") or "") == paused_task_id
            ):
                return row
        return None

    def case_resume_state(self, case_id: str) -> Dict[str, Any]:
        """[quota-resume] Everything the operator needs to decide about ONE Case:
        is it paused, why, when does quota come back, what would a resume cost,
        which mode is recommended, and is a decision already pending. Read-only —
        the UI panel and the ``/api/cases/{id}/resume-state`` endpoint.
        """
        from src.control.db import (
            case_quota_resume_auto, case_quota_resume_enabled, get_db,
        )
        state: Dict[str, Any] = {
            "case_id": case_id, "paused": False, "pause": None,
            "quota": self.quota_window_state("claude"),
            "manager_session_id": None, "manager_session_status": None,
            "recommended_mode": "fresh_manager", "estimate": None,
            "pending_approval": None, "auto": case_quota_resume_auto(),
            "enabled": case_quota_resume_enabled(),
            "modes": list(CASE_RESUME_MODES),
        }
        db = get_db()
        if db is None:
            return state
        session_id = db.case_manager_session_id(case_id)
        session = self.session_store.get(session_id) if session_id else None
        state["manager_session_id"] = session_id
        state["manager_session_status"] = (
            session.status.value if session is not None else None
        )
        pause = db.case_quota_pause(case_id)
        state["recommended_mode"] = self._recommended_resume_mode(
            session_id, paused_at=(pause or {}).get("paused_at"),
        )
        state["estimate"] = self.estimate_case_resume_cost(session_id)
        if pause is None:
            return state
        state["paused"] = True
        state["pause"] = pause
        try:
            self._ensure_approval_service(db)
            existing = self._find_case_pause_approval(
                case_id, str(pause.get("paused_task_id") or ""),
            )
        except Exception:
            existing = None
        if existing is not None:
            state["pending_approval"] = {
                "id": existing.get("id"), "status": existing.get("status"),
                "created_at": existing.get("created_at"),
                "resolved_at": existing.get("resolved_at"),
            }
        return state

    async def _handle_quota_paused_case(self, db, case_id: str) -> bool:
        """One Wake-Dispatcher pass over a Case's quota pause.

        Returns True iff the pause OWNS this Case this tick — the caller must
        then skip the normal wake path: while the window is spent, delivering a
        wake turn only burns another refused turn, and once it is restored the
        resume (not a wake) is the thing that continues the Case.

        Branches, in order:
          * flag OFF / no open pause      → False (pre-feature behaviour)
          * quota still spent             → True, silent (nothing to decide yet)
          * restored + decision pending   → True, silent (already asked)
          * restored + decision rejected  → close the pause, return False (the
            Case goes back to ordinary behaviour; the operator can still press
            Resume, or Block the Case if they want it to stop)
          * restored + approved           → resume (safety net if the on-approve
            dispatch was lost mid-crash; the lease makes it idempotent)
          * restored, auto ON, under the
            USD ceiling                   → resume
          * otherwise                     → propose (approval + event) → True
        """
        from src.control.db import (
            case_quota_resume_auto, case_quota_resume_auto_max_usd,
            case_quota_resume_enabled,
        )
        if not case_quota_resume_enabled():
            return False
        pause = db.case_quota_pause(case_id)
        if pause is None:
            return False
        paused_task_id = str(pause.get("paused_task_id") or "")
        quota = self.quota_window_state(str(pause.get("provider") or "claude"))
        if quota.get("exhausted"):
            return True
        # THE RESET INSTANT ON THE REFUSAL IS THE SCHEDULE. The provider told us,
        # at the moment it refused, exactly when this account's window reopens.
        # Nothing that arrives later is more accurate about that boundary, so it
        # gates the proposal — and only telemetry OBSERVED AFTER THE PAUSE may
        # overrule it (the window can genuinely reopen early if Anthropic resets
        # or re-anchors limits; a reading from before the pause obviously cannot
        # know the pause happened).
        #
        # This is the fix for the real hole: the observer's last poll can be
        # hours old and say `below_limit`, which is neither `exhausted` nor
        # `no_telemetry` — so a paused Case used to be proposed for resume on the
        # very next 30s tick, hours before quota actually returned, and approving
        # it bought another refused turn.
        recorded_reset = _parse_iso_utc(pause.get("reset_at"))
        if recorded_reset is not None and recorded_reset > datetime.now(timezone.utc):
            observed_at = _parse_iso_utc(quota.get("observed_at"))
            paused_at = _parse_iso_utc(pause.get("paused_at"))
            reopened_early = (
                quota.get("evidence") == "below_limit"
                and observed_at is not None
                and paused_at is not None
                and observed_at > paused_at
            )
            if not reopened_early:
                return True
        elif recorded_reset is None and not self._restore_reading_is_usable(quota, pause):
            # NO instant on record (a refusal that named none, with the observer
            # off or hours behind). The only thing left that could release the
            # pause is a telemetry reading — and a reading taken BEFORE the
            # provider refused cannot possibly know the window is spent. Waiting
            # is bounded below, not indefinite: past the longest window a Claude
            # account has, the pause fails OPEN and asks (see the helper).
            return True

        try:
            self._ensure_approval_service(db)
            existing = self._find_case_pause_approval(case_id, paused_task_id)
            if existing is not None:
                status = str(existing.get("status") or "")
                if status == "pending":
                    return True
                if status == "approved":
                    await self.resume_case(
                        case_id, actor="approval_recovery",
                        paused_task_id=paused_task_id,
                    )
                    return True
                # rejected/expired — the operator said no to THIS pause. Close it
                # so the pause does not silently disable the Case forever.
                db.append_flow_event(
                    case_id, "flow.quota_pause_declined", "operator",
                    entity_type="task", entity_id=paused_task_id or None,
                    payload={"approval_id": existing.get("id"), "status": status},
                )
                self._emit_event(
                    "case_quota_resume_declined", None,
                    {"case_id": case_id, "approval_id": existing.get("id")},
                )
                return False

            estimate = self.estimate_case_resume_cost(pause.get("session_id"))
            mode = self._recommended_resume_mode(
                pause.get("session_id"), paused_at=pause.get("paused_at"),
            )
            ceiling = case_quota_resume_auto_max_usd()
            usd = estimate.get("usd")
            under_ceiling = (
                ceiling <= 0 or (isinstance(usd, (int, float)) and usd <= ceiling)
            )
            if case_quota_resume_auto() and under_ceiling:
                await self.resume_case(
                    case_id, mode=mode, actor="quota_restore_auto",
                    paused_task_id=paused_task_id,
                )
                # Auto spends without asking, so the operator must LEARN of it as
                # it happens — same two channels, framed as a fait accompli.
                try:
                    if self.notifier is not None:
                        await self.notifier.notify_case_resume_proposal(
                            case_id=case_id, mode=mode, estimate_usd=usd,
                            estimate_known=bool(estimate.get("known")),
                            reset_at=pause.get("reset_at"),
                            objective=str((db.get_case_brief(case_id) or {}).get("objective") or ""),
                            auto=True,
                        )
                except Exception as e:
                    logger.warning("event=case_resume_notify_failed case=%s err=%s", case_id, e)
                return True

            brief = db.get_case_brief(case_id) or {}
            result = self.approval_service.request(
                action=CASE_RESUME_APPROVAL_ACTION, risk="medium", reversible=True,
                requested_by="system", case_id=case_id,
                payload={
                    "case_id": case_id,
                    "paused_task_id": paused_task_id,
                    "session_id": pause.get("session_id"),
                    "mode": mode,
                    "cause": "quota_restored",
                    "paused_at": pause.get("paused_at"),
                    "reset_at": pause.get("reset_at"),
                    "quota_evidence": quota.get("evidence"),
                    "estimate_usd": usd,
                    "estimate_known": bool(estimate.get("known")),
                    "objective_excerpt": str(brief.get("objective") or "")[:400],
                },
            )
            if not getattr(result, "ok", False):
                raise RuntimeError(f"approval_request_failed:{getattr(result, 'reason', '')}")
            self._emit_event(
                "case_resume_proposed", None,
                {"case_id": case_id, "mode": mode, "estimate_usd": usd,
                 "paused_task_id": paused_task_id},
            )
            # The proposal is worthless if nobody sees it: quota returns hours
            # later, typically when the operator is not on the dashboard. Push +
            # Telegram, best-effort and isolated — a notification failure must
            # never undo a proposal that is already on the ledger.
            try:
                if self.notifier is not None:
                    await self.notifier.notify_case_resume_proposal(
                        case_id=case_id, mode=mode, estimate_usd=usd,
                        estimate_known=bool(estimate.get("known")),
                        reset_at=pause.get("reset_at"),
                        objective=str(brief.get("objective") or ""),
                    )
            except Exception as e:
                logger.warning("event=case_resume_notify_failed case=%s err=%s", case_id, e)
            logger.info(
                "event=case_resume_proposed case=%s mode=%s estimate_usd=%s",
                case_id, mode, usd,
            )
            return True
        except Exception as e:
            # A failure in the DECISION layer must not strand the Case: fall
            # through to the ordinary tick (which is what would have happened
            # before this seam existed).
            logger.warning("event=quota_resume_gate_failed case=%s err=%s", case_id, e)
            return False

    def _restore_reading_is_usable(self, quota: Dict[str, Any], pause: Dict[str, Any]) -> bool:
        """[quota-resume] May THIS telemetry reading release a pause that carries no
        recorded reset instant?

        Only when the reading is not meaningfully older than the refusal it is
        supposed to overrule. The live shape (2026-08-19): the observer's last
        poll was 35 minutes stale and said "68% used, below_limit", so the pause
        was proposed for resume on the next 30s tick — an hour before quota came
        back. A reading from before the pause has no information about it.

        A small grace absorbs the ordinary race where the observer polls in the
        same moment the turn is refused. Past ``_QUOTA_BLIND_WAIT_MAX`` — longer
        than any window this provider runs — the answer is True regardless: a
        silent instrument must never park a Case forever (the same fail-open
        posture ``quota_window_state`` takes for a missing instrument).
        """
        paused_at = _parse_iso_utc(pause.get("paused_at"))
        if paused_at is None:
            return True
        if datetime.now(timezone.utc) - paused_at >= _QUOTA_BLIND_WAIT_MAX:
            return True
        observed_at = _parse_iso_utc(quota.get("observed_at"))
        if observed_at is None:
            return False
        return observed_at >= paused_at - _QUOTA_RESTORE_READING_GRACE

    async def _handle_transient_paused_case(self, db, case_id: str) -> bool:
        """[transient-resume] One Wake-Dispatcher pass over a Case's transient-5xx
        pause. Returns True iff the pause OWNS this Case this tick (caller then
        skips the normal wake path).

        Branches, in order:
          * flag OFF / no open pause         → False (pre-feature behaviour)
          * backoff not yet elapsed          → True, silent (still overloaded)
          * bound session dead/closed        → close the pause, False (let the
            dead-manager/respawn path handle it, don't retry into a corpse)
          * bound session BUSY / not waiting → True (a turn is already running —
            an operator poke or the prior retry; not stuck, don't double-drive)
          * elapsed + session AWAITING_INPUT → deliver ONE retry of the exact
            failed turn under a single-flight lease, close the pause, return True

        The retry is automatic and free — a 529 does not re-write the prompt cache
        the way a multi-hour quota pause does — so there is no cost estimate and no
        approval, unlike ``_handle_quota_paused_case``.
        """
        from src.control.db import (
            CONTINUATION_MACHINE_SENTINEL, TRANSIENT_RESUME_ACTION,
            transient_provider_resume_enabled, transient_resume_task_id,
        )
        if not transient_provider_resume_enabled():
            return False
        pause = db.transient_pause(case_id)
        if pause is None:
            return False
        retry_at = _parse_iso_utc(pause.get("retry_at"))
        if retry_at is not None and retry_at > datetime.now(timezone.utc):
            return True  # backoff still running — holding, nothing to decide yet

        paused_task_id = str(pause.get("paused_task_id") or "")
        session_id = str(pause.get("session_id") or "") or db.case_manager_session_id(case_id)
        session = self.session_store.get(session_id) if session_id else None
        if session is None or session.status in (
            SessionStatus.CLOSED, SessionStatus.CANCELLED,
        ):
            # The session that hit the 529 is gone. Close the pause so it stops
            # owning the Case, and hand back to the ordinary tick — the dead-manager
            # branch (crash-respawn) is the right owner of a dead session, not a
            # blind retry into it.
            db.append_flow_event(
                case_id, "flow.transient_resumed", "system",
                entity_type="session", entity_id=session_id or None,
                payload={"paused_task_id": paused_task_id,
                         "attempt": pause.get("attempt"),
                         "outcome": "session_unavailable"},
            )
            return False
        if session.status != SessionStatus.AWAITING_INPUT:
            # BUSY (a turn — operator poke or the prior retry — is in flight) or a
            # transient IDLE/just-restored state: not stuck, resolves on its own.
            return True

        # SINGLE-FLIGHT: the same reserved sentinel + atomic claim the continuation/
        # respawn/quota-resume leases use (no new lock model). Keyed on attempt so a
        # later retry never collides with this one's completed row.
        attempt = int(pause.get("attempt") or 1)
        retry_id = transient_resume_task_id(case_id, paused_task_id, attempt)
        db.enqueue_task(
            retry_id,
            session_id=None,
            machine_id=CONTINUATION_MACHINE_SENTINEL,
            backend=(getattr(session, "backend", None) or "claude"),
            action=TRANSIENT_RESUME_ACTION,
            payload={"case_id": case_id, "paused_task_id": paused_task_id,
                     "attempt": attempt, "session_id": session_id},
        )
        if not db.claim_task(retry_id, socket.gethostname()):
            return True  # a concurrent tick owns this retry — done for this Case

        description = self._render_transient_retry_turn(db, case_id, pause)
        try:
            await self.submit_instruction(
                description=description,
                session_id=session_id,
                cwd=getattr(session, "repo_path", None),
                source="manager_transient_resume",
            )
        except Exception as e:
            logger.warning("event=transient_resume_deliver_failed case=%s err=%s", case_id, e)
            db.release_task(retry_id, socket.gethostname())
            return True
        db.append_flow_event(
            case_id, "flow.transient_resumed", "system",
            entity_type="session", entity_id=session_id,
            payload={"paused_task_id": paused_task_id, "attempt": attempt,
                     "outcome": "retried"},
        )
        db.complete_task(retry_id, result={"attempt": attempt, "session_id": session_id})
        self._emit_event(
            "case_transient_resumed", None,
            {"case_id": case_id, "session_id": session_id, "attempt": attempt},
        )
        logger.info(
            "event=case_transient_resumed case=%s session=%s attempt=%s",
            case_id, session_id, attempt,
        )
        return True

    def _render_transient_retry_turn(
        self, db, case_id: str, pause: Dict[str, Any],
    ) -> str:
        """The auto-retry turn for a transient-paused Case.

        Prefers re-running the EXACT instruction that was refused (captured on the
        pause), so a fresh operator turn that 529'd is retried faithfully rather
        than silently replaced. Falls back to a generic re-derive-from-ledger turn
        only when the original was too large to store — mirroring the quota
        in-place resume's 'trust the ledger' framing.
        """
        attempt = int(pause.get("attempt") or 1)
        failed_prompt = str(pause.get("failed_prompt") or "")
        truncated = bool(pause.get("failed_prompt_truncated"))
        if failed_prompt and not truncated:
            return (
                "[transient-retry] The previous attempt at this turn was refused by a "
                "transient provider error (5xx / 529 Overloaded) and is being retried "
                f"automatically (attempt {attempt}). No state changed. The original "
                "instruction follows verbatim — carry it out now:\n\n"
                f"{failed_prompt}"
            )
        objective = str((db.get_case_brief(case_id) or {}).get("objective") or "").strip()
        return (
            "[transient-retry] The previous turn on this Case was refused by a transient "
            f"provider error (5xx / 529 Overloaded) and is being retried automatically "
            f"(attempt {attempt}). Case: {case_id}. Objective (unchanged — do NOT re-open "
            f"or re-scope it): {objective}\n"
            "FIRST call get_case_brief / get_case to re-derive the CURRENT state from the "
            "ledger (dispatched workers, verdicts recorded, open waits), THEN do the single "
            "next thing that advances the objective. This turn was delivered autonomously "
            "by the harness — treat it exactly like an operator poke to continue the Case."
        )

    async def resume_case(
        self,
        case_id: str,
        *,
        mode: Optional[str] = None,
        actor: str = "operator",
        paused_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """[quota-resume] THE resume path. One function, one lease, three callers
        (operator button, approved proposal, auto-restore).

        This is a genuine CONTINUATION, never a fork: both modes stay on the same
        ``flow_run_id``, keep the objective locked, and re-arm the existing waits.

        ``mode`` defaults to :meth:`_recommended_resume_mode`. ``in_place`` is
        refused (and downgraded to ``fresh_manager``) when the bound session
        cannot take a turn, because silently doing nothing is the failure this
        whole seam exists to remove.

        Returns ``{ok, reason, mode, case_id, session_id}``. ``reason`` codes:
        ``case_not_found`` / ``case_terminal`` / ``continuation_disabled`` /
        ``manager_busy`` / ``resume_in_flight`` / ``no_manager_link`` /
        ``respawn_failed`` / ``deliver_failed`` / ``db_unavailable``.
        """
        from src.control.db import (
            CONTINUATION_MACHINE_SENTINEL, QUOTA_RESUME_ACTION,
            case_continuation_enabled, get_db, quota_resume_task_id,
        )
        out: Dict[str, Any] = {
            "ok": False, "reason": "", "case_id": case_id,
            "mode": mode or "", "session_id": None,
        }
        db = get_db()
        if db is None:
            out["reason"] = "db_unavailable"
            return out
        row = db.get_flow_run(case_id)
        if row is None:
            out["reason"] = "case_not_found"
            return out
        if str(row.get("status") or "").strip().lower() in ("closed", "cancelled"):
            out["reason"] = "case_terminal"
            return out
        if not case_continuation_enabled():
            # Without the continuation substrate a resumed Manager has no wake
            # path at all — resuming would produce one orphan turn. Refuse
            # visibly instead.
            out["reason"] = "continuation_disabled"
            return out

        # LEASE KEY. A resume of a PAUSE is keyed on the paused task — one pause,
        # one resume, whichever entry point gets there first. A manual resume with
        # no open pause is an ordinary operator poke and must stay repeatable
        # later, so it is keyed on how many resumes this Case has already
        # recorded: two rapid clicks compute the same key (one wins), while a
        # deliberate poke after the previous resume landed gets a fresh one.
        pause = db.case_quota_pause(case_id)
        if not paused_task_id:
            paused_task_id = str((pause or {}).get("paused_task_id") or "")
        if not paused_task_id:
            resumes = sum(
                1 for e in db.list_flow_events(case_id, limit=1000)
                if e.get("event_type") == "flow.quota_resumed"
            )
            paused_task_id = f"manual:{resumes}"
        session_id = db.case_manager_session_id(case_id)
        if not session_id:
            out["reason"] = "no_manager_link"
            return out
        session = self.session_store.get(session_id)
        if session is not None and session.status == SessionStatus.BUSY:
            # A turn is already running on this Manager — the Case is not stuck.
            out["reason"] = "manager_busy"
            return out

        paused_at = (pause or {}).get("paused_at")
        chosen = (mode or "").strip().lower() or self._recommended_resume_mode(
            session_id, paused_at=paused_at,
        )
        if chosen not in CASE_RESUME_MODES:
            chosen = self._recommended_resume_mode(session_id, paused_at=paused_at)
        if chosen == "in_place" and (
            session is None
            or session.status in (SessionStatus.CLOSED, SessionStatus.CANCELLED)
        ):
            chosen = "fresh_manager"
        out["mode"] = chosen

        # SINGLE-FLIGHT: the same atomic claim the continuation/respawn leases
        # use. This is what makes "operator pressed Resume" and "quota came back"
        # unable to start two Managers on one Case — whoever claims first owns it.
        resume_id = quota_resume_task_id(case_id, paused_task_id)
        db.enqueue_task(
            resume_id,
            session_id=None,
            machine_id=CONTINUATION_MACHINE_SENTINEL,
            backend=(getattr(session, "backend", None) or "claude"),
            action=QUOTA_RESUME_ACTION,
            payload={"case_id": case_id, "paused_task_id": paused_task_id,
                     "mode": chosen, "actor": actor},
        )
        if not db.claim_task(resume_id, socket.gethostname()):
            out["reason"] = "resume_in_flight"
            return out

        try:
            if chosen == "fresh_manager":
                generation = int(
                    db.compute_continuation_tick(case_id).get("generation_next") or 1
                )
                spawned = await self._do_respawn_manager_for_case(
                    db, case_id, generation, session_id,
                )
                if not spawned:
                    db.release_task(resume_id, socket.gethostname())
                    out["reason"] = "respawn_failed"
                    return out
                out["session_id"] = db.case_manager_session_id(case_id)
            else:
                # In place: the session is alive and idle. A resume turn is an
                # ordinary instruction — the SAME conversation, not a fork.
                if session is not None and session.status == SessionStatus.ERROR:
                    session.status = SessionStatus.AWAITING_INPUT
                    self.session_store.save(session)
                try:
                    await self.submit_instruction(
                        description=self._render_quota_resume_turn(case_id, row),
                        session_id=session_id,
                        cwd=getattr(session, "repo_path", None),
                        source="manager_quota_resume",
                    )
                except Exception as e:
                    logger.warning("event=quota_resume_deliver_failed case=%s err=%s", case_id, e)
                    db.release_task(resume_id, socket.gethostname())
                    out["reason"] = "deliver_failed"
                    return out
                out["session_id"] = session_id

            db.append_flow_event(
                case_id, "flow.quota_resumed", actor,
                entity_type="session", entity_id=out["session_id"],
                payload={"mode": chosen, "paused_task_id": paused_task_id,
                         "actor": actor, "resumed_session_id": out["session_id"]},
            )
            db.complete_task(resume_id, result={"mode": chosen, "session_id": out["session_id"]})
            self._emit_event(
                "case_resumed", None,
                {"case_id": case_id, "mode": chosen, "actor": actor,
                 "session_id": out["session_id"]},
            )
            logger.info(
                "event=case_resumed case=%s mode=%s actor=%s session=%s",
                case_id, chosen, actor, out["session_id"],
            )
            out["ok"] = True
            return out
        except Exception as e:
            logger.warning("event=case_resume_failed case=%s err=%s", case_id, e)
            try:
                db.release_task(resume_id, socket.gethostname())
            except Exception:
                pass
            out["reason"] = "resume_failed"
            return out

    def _render_quota_resume_turn(self, case_id: str, case_row: Dict[str, Any]) -> str:
        """The in-place resume turn. Deliberately points the Manager at the LEDGER
        first: after a multi-hour pause its in-context picture of the Case is the
        stalest thing in the room, and re-deriving state from get_case_brief is
        cheaper than re-reasoning from the transcript."""
        objective = str(case_row.get("objective") or "").strip()
        return (
            "[quota-resume] The quota window that interrupted this Case has reopened, "
            f"and the operator authorised continuing it. Case: {case_id}. Objective "
            f"(unchanged — do NOT re-open or re-scope it): {objective}\n"
            "FIRST call get_case_brief / get_case to re-derive the CURRENT state from the "
            "ledger (dispatched workers, verdicts already recorded, open waits, rounds "
            "used) — hours may have passed since your last turn, so trust the ledger over "
            "your recollection. THEN do the single next thing that advances the objective: "
            "review a finished worker's committed diff, dispatch the next task, wait on "
            "outstanding workers, or close the Case if its completion_criteria are met. "
            "Do NOT restate the plan or summarise history back to the operator — this turn "
            "is expensive; spend it on work, not narration."
        )

    async def _handle_dead_manager_session(
        self, db, case_id: str, generation: int, dead_session_id: Optional[str],
    ) -> bool:
        """[Case-respawn approval gate] Decide whether to respawn now, wait, or
        ask the operator first. Same return contract as the respawn it wraps:
        True = a respawn is owned/being handled this tick (don't escalate a
        strand); False = fall through to the pre-gate escalation.

        CASE_RESPAWN_REQUIRES_APPROVAL OFF ⇒ byte-identical to pre-gate M3.4 Job 3
        (respawns immediately). ON: a quota-caused death (error_class rate_limit/
        usage_limit on the dead session's last task) waits — silently, no
        approval request yet — until telemetry confirms the quota window is no
        longer exhausted, THEN asks. A non-quota death (or no task to classify)
        asks immediately. Any failure in this gate FAILS OPEN to the unconditional
        respawn — a bug here must never regress below pre-gate behavior."""
        from src.control.db import case_continuation_enabled, case_respawn_requires_approval
        if not case_respawn_requires_approval():
            return await self._do_respawn_manager_for_case(db, case_id, generation, dead_session_id)
        # Mirror _do_respawn_manager_for_case's own defensive re-gate BEFORE ever
        # asking the operator: an approval the eventual respawn would refuse
        # anyway (continuation off, or a naked tool-less Manager with role off)
        # is worse than no approval — it would sit "approved" with nothing
        # happening. Let these fall straight through to strand-escalation, same
        # as pre-gate behavior.
        if not case_continuation_enabled() or not self._manager_role_enabled():
            return False

        action = "case_manager_respawn"
        try:
            self._ensure_approval_service(db)
            existing = self._find_case_generation_approval(action, case_id, generation)
            if existing is not None:
                status = str(existing.get("status") or "")
                if status == "approved":
                    # Safety net: approved but the on_approve callback never
                    # fired/completed (e.g. a crash between commit and dispatch).
                    return await self._do_respawn_manager_for_case(
                        db, case_id, generation, dead_session_id,
                    )
                # 'pending' → already asked, don't spam. 'rejected' → operator
                # declined, stand down for this generation (no re-ask, no
                # escalation — the case just stays open until manual action).
                return True

            error_class = ""
            if dead_session_id:
                last = db.list_tasks(session_id=dead_session_id, limit=1)
                if last:
                    error_class = str(last[0].get("error_class") or "")
            cause = "crash"
            if error_class in ("rate_limit", "usage_limit"):
                if self._quota_still_exhausted_for_claude():
                    # Confirmed still down — wait for a future tick, don't nag
                    # with an approval the operator can't usefully act on yet.
                    return True
                cause = "quota_restored"

            objective_excerpt = ""
            try:
                brief = db.get_case_brief(case_id) or {}
                objective_excerpt = str(brief.get("objective") or "")[:400]
            except Exception:
                pass
            result = self.approval_service.request(
                action=action, risk="medium", reversible=True, requested_by="system",
                case_id=case_id,
                payload={
                    "case_id": case_id, "generation": generation,
                    "dead_session_id": dead_session_id, "cause": cause,
                    "error_class": error_class, "objective_excerpt": objective_excerpt,
                },
            )
            if not getattr(result, "ok", False):
                raise RuntimeError(f"approval_request_failed:{getattr(result, 'reason', '')}")
            self._emit_event(
                "case_respawn_approval_requested", None,
                {"case_id": case_id, "generation": generation, "cause": cause},
            )
            return True
        except Exception as e:
            logger.warning("event=respawn_approval_gate_failed case=%s err=%s", case_id, e)
            return await self._do_respawn_manager_for_case(db, case_id, generation, dead_session_id)

    async def _do_respawn_manager_for_case(
        self, db, case_id: str, generation: int, dead_session_id: Optional[str],
    ) -> bool:
        """[A55 / M3.4 Job 3] Bring a role-full Manager back on a SATISFIED Case whose
        bound Manager session is dead — on the SAME Case, exactly once, never new work.

        Unconditional — actually performs the respawn. Callers that want the
        approval gate (CASE_RESPAWN_REQUIRES_APPROVAL) go through
        ``_handle_dead_manager_session`` instead, which calls this only once an
        operator has approved (or the flag is OFF). This method is also the
        ``on_approve`` target invoked from the approval-resolution path.

        Returns True iff a respawn is owned for this Case this tick (this tick won the
        single-flight and (re)spawned, OR a concurrent tick won and this one lost the
        atomic claim — either way a Manager is being brought back, so the caller must
        NOT escalate a strand). Returns False only when a respawn is genuinely not
        viable (continuation off, no placement node, reconstruction/spawn failed) — the
        caller then falls through to the visible-strand escalation as before A55.

        SINGLE-FLIGHT: reuses the continuation lease mechanism verbatim — a deterministic
        ``respawn:{case}:{gen}`` row pinned to ``CONTINUATION_MACHINE_SENTINEL`` (invisible
        to every worker claim scan) claimed via the atomic ``claim_task``. Two racing ticks
        enqueue the SAME id (UNIQUE collapses to one row); ``claim_task`` (an
        ``UPDATE … WHERE status='pending'`` + ``changes()>0``) elects a single winner. NO
        second lock model. A crash between claim and spawn leaves the row ``claimed`` by a
        dead incarnation → reaped to ``pending`` by the SAME reaper → re-claimed and retried
        by a later tick (at-least-once, no permanent stall).

        ANTI-GOAL: this NEVER calls ``open_case`` and NEVER mints a new ``flow_run_id`` — it
        binds the fresh session to the passed ``case_id`` via a manager ``flow_link`` and
        reconstructs the objective/waits from the DB (``get_case_brief`` /
        ``boot_reconcile_case``). The objective-lock is preserved end to end.
        """
        from src.control.db import (
            case_continuation_enabled, CONTINUATION_MACHINE_SENTINEL,
            RESPAWN_ACTION, respawn_task_id,
        )
        # Defensive re-gate (this path is only reached with continuation ON, but keep
        # the respawn itself explicitly flag-guarded so it can never fire otherwise).
        if not case_continuation_enabled():
            return False
        # Manager respawn boots a role-full Manager session; the driver's _role_boot
        # only applies the role prompt + scoped manager tools when MANAGER_ROLE is ON.
        # Without it a "respawn" would be a naked session with no dispatch tools — worse
        # than the visible strand. Surface via the escalation path instead.
        if not self._manager_role_enabled():
            return False

        # Reconstruct the Case from the DB ALONE (A54). A None brief means the Case is
        # unknown/unreadable — not respawnable; let the caller escalate.
        brief = db.get_case_brief(case_id)
        if brief is None:
            return False
        objective = str(brief.get("objective") or "").strip()
        if not objective:
            return False

        # SINGLE-FLIGHT CLAIM (atomic, one winner) — BEFORE any spawn side-effect.
        respawn_id = respawn_task_id(case_id, generation)
        db.enqueue_task(
            respawn_id,
            session_id=None,
            machine_id=CONTINUATION_MACHINE_SENTINEL,
            backend="claude",
            action=RESPAWN_ACTION,
            payload={"case_id": case_id, "generation": generation,
                     "dead_session_id": dead_session_id},
        )
        if not db.claim_task(respawn_id, socket.gethostname()):
            # Lost the race — a concurrent tick owns the respawn. Report "owned" so the
            # caller does NOT double-respawn or escalate a strand that is being healed.
            return True

        # --- We are the single respawn owner. Everything below runs at most once. ---
        try:
            # NODE PLACEMENT: reuse the dead Manager's recorded node if we can read it,
            # else the gateway host (__local__). Remote-node MCP reachability is a known
            # deferred item — if the recorded node is remote we still pin it (the mesh
            # dispatch path owns reachability); a __local__/absent pin lands in-gateway.
            node_id = "__local__"
            repo_path = os.getcwd()
            backend = "claude"
            if dead_session_id:
                dead_row = db.get_session(dead_session_id)
                if dead_row is not None:
                    node_id = str(dead_row.get("machine_id") or "") or "__local__"
                    repo_path = str(dead_row.get("repo_path") or "") or repo_path
                    backend = str(dead_row.get("backend") or "") or backend

            from src.core.interfaces import SessionOrigin
            result = self.session_service.create_session(
                backend=backend, repo_path=repo_path, node_id=node_id,
                origin=SessionOrigin(channel="web", kind="user"), bind_chat=False,
            )
            if not getattr(result, "ok", False) or getattr(result, "session", None) is None:
                # Spawn failed AFTER the claim — release the lease so a later tick can
                # retry cleanly rather than stranding the row 'claimed' (recovery).
                db.release_task(respawn_id, socket.gethostname())
                return False
            new_session = result.session
            new_sid = new_session.session_id

            # Bind the fresh session to the SAME Case as its Manager — the anti-goal
            # boundary. NO open_case, NO new flow_run_id: just a manager flow_link on the
            # existing Case + the durable session affiliation the wake target is read from.
            db.create_flow_link(
                case_id, "session", new_sid, "manager", created_by="system",
            )
            self._set_session_case_affiliation(new_sid, case_id, role="manager")
            db.append_flow_event(
                case_id, "case.manager_respawned", "system",
                entity_type="session", entity_id=new_sid,
                payload={"reason": "manager_session_dead",
                         "dead_session_id": dead_session_id,
                         "generation": generation, "node_id": node_id},
            )

            # Re-arm the Case's outstanding waits/groups from the ledger (A54, idempotent).
            try:
                db.boot_reconcile_case(case_id, actor="manager")
            except Exception as e:
                logger.debug("event=respawn_reconcile_failed case=%s err=%s", case_id, e)

            # Resume turn — a role-full Manager first assignment that RESUMES this Case
            # (get_case_brief + reconcile_waits), NOT a new objective. Delivering the turn
            # flips the new session BUSY→AWAITING_INPUT so the next tick wakes it normally.
            resume = self._render_respawn_turn(case_id, objective)
            try:
                await self.submit_instruction(
                    description=resume,
                    session_id=new_sid,
                    cwd=new_session.repo_path,
                    source="manager_respawn",
                )
            except Exception as e:
                # The session + binding are already durable; a failed first-turn deliver
                # leaves a bound Manager the next tick can still drive. Keep the claim
                # COMPLETED (we did respawn) and surface the deliver failure.
                logger.warning("event=respawn_deliver_failed case=%s err=%s", case_id, e)

            db.complete_task(respawn_id, result={"session_id": new_sid, "node_id": node_id})
            self._emit_event(
                "case_manager_respawned", None,
                {"case_id": case_id, "new_session_id": new_sid,
                 "dead_session_id": dead_session_id, "generation": generation},
            )
            logger.info(
                "event=case_manager_respawned case=%s new_session=%s dead_session=%s node=%s",
                case_id, new_sid, dead_session_id, node_id,
            )
            return True
        except Exception as e:
            # Any failure after the claim releases the lease for a clean retry — the
            # respawn must never permanently stall a Case on a transient error.
            logger.warning("event=respawn_failed case=%s err=%s", case_id, e)
            try:
                db.release_task(respawn_id, socket.gethostname())
            except Exception:
                pass
            return False

    def _render_respawn_turn(self, case_id: str, objective: str) -> str:
        """[A55] The role-full resume turn for a respawned Manager. Points it at the
        SAME Case to reconstruct (get_case_brief) and reconcile (reconcile_waits) — it
        RESUMES a bounded Case, it does NOT open a new one."""
        return (
            "[respawn] You are resuming an EXISTING Case whose prior Manager session "
            f"crashed/was lost. Case: {case_id}. Objective (unchanged, do NOT re-open "
            f"or re-scope it): {objective}\n"
            "FIRST call get_case(case_id) / read your Case brief to reconstruct the full "
            "working state (dispatched workers, latest verdicts, open/ready waits, rounds "
            "used) from the durable record — your in-process memory is empty. Then call "
            "reconcile_waits to re-establish your outstanding obligations. Review any "
            "finished-but-unreviewed worker deliveries IN ORDER (relevance gate before "
            "rigor gate), then dispatch the next task / wait on remaining workers / "
            "close the Case if its completion_criteria are met. Do NOT open_case a new "
            "objective — this is a continuation of the SAME bounded Case. This turn was "
            "delivered autonomously by the harness after a crash-respawn."
        )

    async def _finalize_continuation(
        self,
        case_id: str,
        continuation_id: str,
        generation: int,
        presented: List[str],
        retired_group_ids: List[str],
        wake_task_id: Optional[str],
        session_id: str,
    ) -> None:
        """Wait for the proactive wake turn to return, then HARNESS-record the
        consumed watermark (the transport ACK). Because consumption is written
        ONLY here (never by the LLM), a crash before this point leaves the row
        'claimed' → reaped → redelivered (at-least-once)."""
        from src.control.db import get_db
        try:
            db = get_db()
        except Exception:
            db = None
        if db is None:
            return
        # Bound the wait so a wedged turn never leaks this task forever.
        deadline = time.time() + float(config.system.task_timeout or 1800)
        # The IDLE fallback (below) must not fire in the window BEFORE the wake turn
        # flips the session BUSY, or we would record consumption prematurely. Only
        # trust "session is IDLE ⇒ turn returned" once we have observed it go BUSY.
        seen_busy = False
        while self.running and time.time() < deadline:
            await asyncio.sleep(2)
            row = db.get_task(wake_task_id) if wake_task_id else None
            if row is not None:
                if row.get("status") in self._CONTINUATION_TERMINAL_STATUSES:
                    break
                continue
            # In-process (non-mesh) turn with no mesh_tasks row: consider it returned
            # only after we saw the session go BUSY and then settle back to a waiting
            # state (IDLE or AWAITING_INPUT — a Manager that finished a turn lands in
            # AWAITING_INPUT, never IDLE) with the wake task no longer active.
            sess = self.session_store.get(session_id)
            if sess is None:
                continue
            if sess.status == SessionStatus.BUSY:
                seen_busy = True
                continue
            if seen_busy and sess.status in (
                SessionStatus.IDLE, SessionStatus.AWAITING_INPUT,
            ) and (not wake_task_id or wake_task_id not in self.active_tasks):
                break
        db.record_continuation_consumed(
            case_id, continuation_id, generation, presented, retired_group_ids,
        )
        self._emit_event(
            "case_continuation_consumed", None,
            {"case_id": case_id, "generation": generation,
             "consumed_task_ids": presented, "continuation_id": continuation_id},
        )

    async def _escalate_case_continuation_cap(
        self, case_id: str, cap: int, generation: int,
    ) -> None:
        """Best-effort operator escalation when a Case exhausts its round cap.
        Isolated: a notify failure must never crash the tick."""
        try:
            await self.notifier.notify_error(
                f"[continuation] Case {case_id} hit its continuation round cap "
                f"({cap}); a further satisfied wait (round {generation}) was NOT "
                "scheduled. Re-enter the Case manually to continue or close it.",
            )
        except Exception as e:
            logger.debug("event=case_continuation_escalate_failed case=%s err=%s", case_id, e)

    async def _escalate_headless_case(
        self, db, case_id: str, session_id: Optional[str],
    ) -> None:
        """[M3.4] A satisfied Case whose Manager session is missing/closed can never
        self-continue — its finished workers are stranded. Record an idempotent
        ``case.manager_unavailable`` marker and notify the operator ONCE (keyed on
        case_id, independent of which dead session), so the strand is VISIBLE
        instead of a silent no-op. Isolated: a notify/db failure must never crash
        the tick."""
        try:
            already = any(
                e.get("event_type") == "case.manager_unavailable"
                for e in db.list_flow_events(case_id)
            )
            if already:
                return
            db.append_flow_event(
                case_id, "case.manager_unavailable", "system",
                payload={"reason": "manager_session_unavailable",
                         "manager_session_id": session_id},
            )
            self._emit_event(
                "case_manager_unavailable", None,
                {"case_id": case_id, "manager_session_id": session_id},
            )
            await self.notifier.notify_error(
                f"[continuation] Case {case_id} has finished worker(s) awaiting "
                f"review but its Manager session ({session_id or 'none'}) is "
                "closed/unavailable — the wake cannot be delivered and the work is "
                "stranded. Re-enter the Case with a live Manager (or close it).",
            )
        except Exception as e:
            logger.debug("event=headless_case_escalate_failed case=%s err=%s", case_id, e)

    async def interrupt_case(
        self, case_id: str, *, actor: str = "operator", reason: str = "operator_kill",
    ) -> Dict[str, Any]:
        """[A53] Programmatic/operator KILL path for a Case.

        Cancels the Case's in-flight worker task(s) (reusing the cooperative
        ``cancel_task`` plumbing — no second cancel mechanism), records a
        ``flow.interrupted`` event, and marks the Case ``status='blocked'`` — a
        RESUMABLE state (per A37, 'blocked' is still OPEN), NEVER a force-close
        (closure stays the authoritative, criteria-gated ``close_case`` op).
        Escalates to the operator exactly once. Idempotent: a second call on an
        already-interrupted Case cancels nothing new, writes no duplicate event,
        and re-escalates nothing — it just reports the prior interruption.

        Returns ``{ok, reason?, cancelled_tasks, already, status}``.
        """
        from src.control.db import get_db, _event_payload
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        row = db.get_flow_run(case_id)
        if row is None:
            return {"ok": False, "reason": "case_not_found"}
        status = str(row.get("status") or "").strip().lower()
        if status in ("closed", "cancelled"):
            # A terminal Case cannot be interrupted — it is already done.
            return {"ok": False, "reason": "case_closed", "status": status}
        if reason == "manager_session_unavailable":
            manager_session_id = db.case_manager_session_id(case_id)
            if manager_session_id:
                manager = self.session_store.get(manager_session_id)
                if manager is not None:
                    raw_status = getattr(manager, "status", "")
                    manager_status = (
                        raw_status.value
                        if isinstance(raw_status, SessionStatus)
                        else str(raw_status)
                    )
                    terminal_manager_statuses = {
                        SessionStatus.CLOSED.value,
                        SessionStatus.CANCELLED.value,
                        SessionStatus.PINNED_NODE_OFFLINE.value,
                    }
                    if manager_status not in terminal_manager_statuses:
                        return {
                            "ok": False,
                            "reason": "manager_session_active",
                            "status": status or "open",
                            "manager_session_id": manager_session_id,
                            "manager_status": manager_status,
                        }

        # Idempotency: has this Case ALREADY been killed? Any prior operator kill
        # (a flow.interrupted whose reason is NOT the round-cap escalation) counts —
        # a second kill, even with a different reason label, must not double-write /
        # double-escalate. (round_cap_exhausted is the Wake-Dispatcher's own escalation
        # and is deliberately excluded so a kill after a cap-escalation still fires.)
        already = any(
            e.get("event_type") == "flow.interrupted"
            and (_event_payload(e) or {}).get("reason") != "round_cap_exhausted"
            for e in db.list_flow_events(case_id)
        )

        # Cancel the in-flight WORKER tasks joined to this Case. A dispatched worker
        # task is linked entity_type='task', role='task', created_by='manager' (the
        # Manager's own-turn attach is created_by='system'; the root task is
        # role='root_task') — so filter on created_by to target only workers.
        # Best-effort; an already-cancelled/absent task returns False ⇒ idempotent.
        cancelled: List[str] = []
        try:
            for link in db.list_flow_links(
                flow_run_id=case_id, entity_type="task", role="task",
            ):
                if str(link.get("created_by") or "") != "manager":
                    continue
                tid = str(link.get("entity_id") or "").strip()
                if tid and self.cancel_task(tid):
                    cancelled.append(tid)
        except Exception as e:
            logger.warning("event=interrupt_case_cancel_failed case=%s err=%s", case_id, e)

        # Mark blocked (resumable) — a follow-up turn can re-enter the Case.
        if status != "blocked":
            try:
                db.update_flow_run(case_id, status="blocked")
            except Exception as e:
                logger.warning("event=interrupt_case_block_failed case=%s err=%s", case_id, e)

        if not already:
            db.append_flow_event(
                case_id, "flow.interrupted", actor,
                payload={"reason": reason, "cancelled_tasks": cancelled},
            )
            self._emit_event(
                "case_interrupted", None,
                {"case_id": case_id, "reason": reason, "cancelled": len(cancelled)},
            )
            try:
                await self.notifier.notify_error(
                    f"[kill] Case {case_id} was interrupted ({reason}); "
                    f"{len(cancelled)} in-flight worker task(s) cancelled. The Case is "
                    "BLOCKED (resumable) — re-enter it to continue or close it."
                )
            except Exception as e:
                logger.debug("event=interrupt_case_escalate_failed case=%s err=%s", case_id, e)

        return {"ok": True, "cancelled_tasks": cancelled, "already": already, "status": "blocked"}

    async def set_case_state(
        self,
        case_id: str,
        *,
        state: str,
        actor: str = "operator",
        reason: str = "operator_state_change",
    ) -> Dict[str, Any]:
        """Operator state control for a Case.

        This is deliberately narrower than ``update_flow_run``: operators can
        move a non-terminal Case between ``open`` and ``blocked``. ``closed`` and
        ``cancelled`` remain governed by the existing close/interrupt semantics.
        """
        from src.control.db import get_db

        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        row = db.get_flow_run(case_id)
        if row is None:
            return {"ok": False, "reason": "case_not_found"}

        current = str(row.get("status") or "").strip().lower()
        target = (state or "").strip().lower()
        note = (reason or "operator_state_change").strip()[:256] or "operator_state_change"

        if current in ("closed", "cancelled"):
            return {"ok": False, "reason": "case_terminal", "status": current}
        if target in ("open", "active", ""):
            if current == "":
                return {"ok": True, "changed": False, "status": "open"}
            db.update_flow_run(case_id, status=None)
            db.append_flow_event(
                case_id,
                "flow.unblocked" if current == "blocked" else "flow.status_changed",
                actor,
                from_state=current or "open",
                to_state="open",
                payload={"reason": note},
            )
            self._emit_event(
                "case_state_changed", None,
                {"case_id": case_id, "from": current or "open", "to": "open"},
            )
            return {"ok": True, "changed": True, "status": "open"}
        if target == "blocked":
            return await self.interrupt_case(case_id, actor=actor, reason=note[:64])
        return {"ok": False, "reason": "invalid_state", "status": current or "open"}

    async def sweep_orphaned_cases(
        self,
        *,
        limit: int = 200,
        dry_run: bool = False,
        reason: str = "manager_session_unavailable",
    ) -> Dict[str, Any]:
        """Operator cleanup for open Cases that have no active Manager session.

        ``close_case`` means "done" and is criteria-gated, so an orphaned Case
        must not be force-closed. The honest cleanup is the existing operator
        interrupt path: mark it ``blocked`` (resumable), cancel any in-flight
        worker tasks, and leave an audit event explaining why it left the active
        set. Read/scan is bounded by ``limit``; writes only happen when
        ``dry_run`` is false.
        """
        from src.control.db import get_db

        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable", "dry_run": dry_run}

        scan_limit: int = max(1, min(int(limit or 200), 500))
        cleanup_reason: str = (reason or "manager_session_unavailable").strip()[:64]
        if not cleanup_reason:
            cleanup_reason = "manager_session_unavailable"

        candidates: List[Dict[str, Any]] = []
        cleaned: List[Dict[str, Any]] = []
        skipped: int = 0
        inactive_statuses = {
            SessionStatus.CLOSED.value,
            SessionStatus.CANCELLED.value,
            SessionStatus.PINNED_NODE_OFFLINE.value,
        }

        for row in db.list_open_cases(limit=scan_limit):
            case_id = str(row.get("flow_run_id") or "").strip()
            if not case_id:
                skipped += 1
                continue
            status = str(row.get("status") or "").strip().lower()
            if status == "blocked":
                skipped += 1
                continue

            manager_session_id = db.case_manager_session_id(case_id)
            orphan_reason: Optional[str] = None
            manager_status: Optional[str] = None
            if not manager_session_id:
                orphan_reason = "missing_manager_link"
            else:
                session = self.session_store.get(manager_session_id)
                if session is None:
                    orphan_reason = "manager_session_missing"
                else:
                    raw_status = getattr(session, "status", "")
                    manager_status = (
                        raw_status.value
                        if isinstance(raw_status, SessionStatus)
                        else str(raw_status)
                    )
                    if manager_status in inactive_statuses:
                        orphan_reason = f"manager_session_{manager_status}"

            if orphan_reason is None:
                skipped += 1
                continue

            candidate = {
                "case_id": case_id,
                "manager_session_id": manager_session_id,
                "manager_status": manager_status,
                "reason": orphan_reason,
            }
            candidates.append(candidate)
            if dry_run:
                continue

            result = await self.interrupt_case(
                case_id,
                actor="operator",
                reason=cleanup_reason,
            )
            cleaned.append({**candidate, "result": result})

        return {
            "ok": True,
            "dry_run": dry_run,
            "scanned": scan_limit,
            "candidates": candidates,
            "cleaned": cleaned,
            "skipped": skipped,
        }

    # ===========================================================================
    # STALE-BUSY RECONCILER & REMOTE TASK REATTACH  (M3 mesh)
    # Flag: MESH_ENABLED (without it the loop never starts).
    #
    # Periodic scan for sessions stuck in BUSY state whose remote mesh_tasks row
    # is actually terminal.  On hit: _recover_completed_session() delivers the
    # result and transitions the session.  _reattach_remote_task() handles the
    # case where the result row exists but was never surfaced to the operator.
    #
    # FUTURE EXTRACTION → MeshReconciler (low priority, small surface ~3 methods).
    #   Only worth it if the reconciler grows (e.g. per-backend policies, metrics).
    #   Defer until post-M4 when the mesh protocol is stable.
    # ===========================================================================

    def _start_stale_busy_reconciler(self) -> None:
        """Start the periodic M3 reconciliation loop when mesh routing is active."""
        interval = int(getattr(config.mesh, "session_reconcile_interval_sec", 60) or 0)
        if not config.mesh.enabled or interval <= 0:
            return
        if self._stale_busy_reconcile_task and not self._stale_busy_reconcile_task.done():
            return
        self._stale_busy_reconcile_task = asyncio.create_task(
            self._stale_busy_reconciliation_loop(interval)
        )

    async def _stale_busy_reconciliation_loop(self, interval_sec: int) -> None:
        logger.info("event=stale_busy_reconciler_started interval=%ds", interval_sec)
        try:
            while self.running:
                try:
                    await self._reconcile_stale_busy_sessions_once()
                except Exception as e:
                    logger.debug("event=stale_busy_reconcile_failed err=%s", e)
                try:
                    await self._reap_idle_warm_workers_once()
                except Exception as e:
                    logger.debug("event=idle_warm_worker_reap_failed err=%s", e)
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            logger.info("event=stale_busy_reconciler_stopped")
            raise

    async def _reconcile_stale_busy_sessions_once(self) -> int:
        """Mark BUSY sessions with no active task row as ERROR."""
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            db = None
        if db is None:
            return 0

        rows = await asyncio.to_thread(db.list_stale_busy_sessions)
        reconciled = 0
        active_task_ids = set(self.active_tasks.keys())
        for row in rows:
            session_id = row.get("session_id", "")
            task_id = row.get("last_task_id", "") or ""
            if task_id and task_id in active_task_ids:
                continue

            session = self.session_store.get(session_id)
            if session is None or session.status != SessionStatus.BUSY:
                continue

            if task_id:
                task_row = db.get_task(task_id)
                status = task_row.get("status") if task_row else None
                if status == "completed":
                    await self._recover_completed_session(session, task_row)
                    reconciled += 1
                    continue
                if status in ("pending", "claimed"):
                    continue
                if status in ("failed", "failed_node_offline"):
                    error_msg = (task_row.get("error") if task_row else "") or f"Task {status}"
                    session.last_result_summary = error_msg[-400:]

            session.status = SessionStatus.ERROR
            if not session.last_result_summary:
                session.last_result_summary = (
                    "Marked error by mesh reconciliation: session was busy with no active task."
                )
            self.session_store.save(session)

            result = TaskResult(
                task_id=task_id or f"session_{session_id}",
                success=False,
                output="",
                errors=[session.last_result_summary or "stale busy session: no pending or claimed mesh task"],
                files_modified=[],
                execution_time=0.0,
                timestamp=now_iso(),
            )
            setattr(result, "backend_name", session.backend or "claude")
            self._append_session_event(session_id, task_id, result)
            self._emit_event(
                "stale_busy_session_reconciled",
                None,
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "machine_id": session.machine_id,
                    "backend": session.backend,
                },
            )
            logger.warning(
                "event=stale_busy_session_reconciled session_id=%s task_id=%s node=%s",
                session_id,
                task_id,
                session.machine_id,
            )
            reconciled += 1

        return reconciled

    async def _reap_idle_warm_workers_once(self) -> int:
        """[A60] Close warm WORKER sessions idle beyond the configured TTL.

        A48/PR#26 deliberately keeps a joined worker session warm after its Case
        closes (held backend slot, for cheap re-dialogue) — closed only by an
        explicit Manager ``release_worker``, with no idle bound. That is a written
        §7 resource leak once a worker session sits idle indefinitely (observed
        directly: SDK ``claude`` child processes pooled for days with no owning
        Case and no activity). This sweep reclaims them the same way Case-close
        already does — ``session_service.close_session`` — after TTL, never before.

        ``warm_worker_idle_ttl_sec <= 0`` disables the reaper entirely (default
        3600s / 1h, sized so it never fights normal re-dialogue latency). Reuses
        ``_stale_busy_reconciliation_loop``'s cadence — no second scheduler.
        """
        ttl = int(getattr(config.mesh, "warm_worker_idle_ttl_sec", 0) or 0)
        if ttl <= 0:
            return 0
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            db = None
        if db is None:
            return 0

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl)).isoformat()
        rows = await asyncio.to_thread(db.list_idle_warm_workers, cutoff)
        reaped = 0
        for row in rows:
            session_id = row.get("session_id", "")
            # Re-check the live in-memory session, not just the DB row snapshot —
            # mirrors _close_worker_session_on_case_close's guard so a turn that
            # started between the query and this loop iteration is never reaped.
            session = self.session_store.get(session_id)
            if session is None:
                continue
            if session.status not in (SessionStatus.IDLE, SessionStatus.AWAITING_INPUT):
                continue
            if getattr(session, "case_role", None) != "worker":
                continue
            case_id = getattr(session, "current_case_id", None)
            if case_id:
                flow_run = db.get_flow_run(case_id) if hasattr(db, "get_flow_run") else None
                if flow_run and (flow_run.get("status") or "") not in db._CLOSED_STATUSES:
                    continue  # joined to an OPEN Case — never reap
            try:
                result = self.session_service.close_session(
                    session_id, backends=getattr(self, "_backends", {}),
                )
            except Exception as e:
                logger.warning(
                    "event=idle_warm_worker_reap_close_failed session_id=%s err=%s",
                    session_id, e,
                )
                continue
            if case_id:
                self._clear_session_case_affiliation(session_id, case_id)
            self._emit_event(
                "idle_warm_worker_reaped",
                None,
                {
                    "session_id": session_id,
                    "machine_id": row.get("machine_id", ""),
                    "backend": row.get("backend", ""),
                    "idle_ttl_sec": ttl,
                },
            )
            logger.warning(
                "event=idle_warm_worker_reaped session_id=%s node=%s ttl=%ds ok=%s",
                session_id, row.get("machine_id", ""), ttl,
                getattr(result, "success", None),
            )
            reaped += 1

        return reaped

    async def _reattach_remote_task(self, session: Any, task_row: Dict[str, Any]) -> None:
        """Reattach to a remote task still in-flight after a gateway restart.

        The worker on `session.machine_id` kept running across our restart and
        owns the task's terminal state in the DB. We poll the row until it
        reaches a terminal status, then report the worker's *real* result to
        Telegram — never a fabricated one. This is the startup half of the
        detach/reattach handoff (the shutdown half lives in _dispatch_to_node).

        Pending pickup is bounded by the same oneoff_queue_timeout. Once the
        worker has claimed the row, that queue timeout no longer applies.
        """
        import asyncio as _aio
        from src.control.db import get_db

        task_id = task_row.get("id") or ""
        db = get_db()
        if db is None or not task_id:
            return

        pickup_timeout_sec = getattr(config.mesh, "oneoff_queue_timeout_sec", 600)
        pickup_deadline = time.time() + pickup_timeout_sec
        poll_interval = 3.0

        while True:
            if not self.running:
                # Gateway is shutting down again; detach quietly. The next
                # startup will reattach from the still-claimed DB row.
                return
            row = db.get_task(task_id)
            status = row.get("status") if row else None
            if status == "completed":
                await self._recover_completed_session(session, row)
                return
            if status in ("failed", "failed_node_offline"):
                result_raw = row.get("result") if row else None
                try:
                    result_dict = json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                except Exception:
                    result_dict = {}
                error_msg = (row.get("error") if row else "") or f"Task {status}"
                session.status = SessionStatus.ERROR
                session.last_result_summary = error_msg[-400:]
                self.session_store.save(session)
                result = TaskResult(
                    task_id=task_id,
                    success=False,
                    output=result_dict.get("output", "") if result_dict else "",
                    errors=result_dict.get("errors") or [error_msg],
                    files_modified=result_dict.get("files_modified") or [],
                    execution_time=result_dict.get("execution_time", 0.0),
                    timestamp=result_dict.get("timestamp", now_iso()) if result_dict else now_iso(),
                    return_code=result_dict.get("return_code", 1) if result_dict else 1,
                    raw_stdout=result_dict.get("output", "") if result_dict else "",
                    raw_stderr=(result_dict.get("error_detail", "") if result_dict else ""),
                )
                setattr(result, "error_detail", result_dict.get("error_detail", "") if result_dict else "")
                setattr(result, "usage", result_dict.get("usage") if result_dict else None)
                setattr(result, "backend_name", session.backend or "claude")
                self._write_session_summary(session, result)
                self._append_session_event(session.session_id, task_id, result)
                self._emit_event(
                    "session_recovered_failed",
                    None,
                    {"session_id": session.session_id, "task_id": task_id, "backend": session.backend},
                )
                await self.notifier.notify_error(
                    f"Task failed on remote node while gateway was restarting: {error_msg}",
                    task_id=task_id,
                    chat_id=session.telegram_chat_id,
                )
                return
            if status != "claimed" and time.time() >= pickup_deadline:
                break
            # still pending/claimed before pickup timeout, or claimed execution
            # after pickup — keep waiting for the worker's terminal state.
            await _aio.sleep(poll_interval)

        # Timed out waiting for a terminal state. Don't fabricate a result —
        # surface the uncertainty and unblock the session.
        logger.warning(
            "event=reattach_timeout session_id=%s task_id=%s node=%s",
            session.session_id, task_id, session.machine_id,
        )
        session.status = SessionStatus.ERROR
        session.last_result_summary = (
            "Lost contact with the remote node after a gateway restart; "
            "the task's outcome is unknown."
        )
        self.session_store.save(session)
        await self.notifier.notify_error(
            "Lost contact with the remote node after a restart; task outcome unknown.",
            task_id=task_id,
            chat_id=session.telegram_chat_id,
        )

    # ===========================================================================
    # JOB COMPLETION POLLER  (T3 watched-jobs)
    # Flag: MESH_ENABLED (poller only runs when mesh is on).
    #
    # Background coroutine that polls for watched jobs reaching terminal state
    # and fans out Telegram notifications.  Deduplicates via
    # _processed_terminal_jobs so each job notifies exactly once per run.
    #
    # FUTURE EXTRACTION → JobCompletionPoller (very low priority, ~11 methods).
    #   Self-contained enough but too small to justify now.  Revisit if T3 grows
    #   (e.g. Web Push per-job, per-node polling policies, or a jobs feed API).
    # ===========================================================================

    async def _job_completion_poller(self) -> None:
        """Poll for terminal watched jobs and push Telegram notifications.

        Runs as a background task during the gateway's lifetime. Checks every
        30s for jobs that reached terminal state since the last poll.
        """
        while self.running:
            try:
                from src.control.db import get_db
                db = get_db()
                if db is None:
                    await asyncio.sleep(30)
                    continue

                # The task server owns terminal job state; the gateway owns
                # routing those terminal jobs to session-visible notifications.
                terminal = db.get_terminal_jobs_since(self._last_job_poll)
                if terminal:
                    self._last_job_poll = now_iso()

                for job in terminal:
                    await self._process_terminal_job(job)

                remote_terminal = self._remote_terminal_jobs_since(self._last_remote_job_poll)
                if remote_terminal:
                    self._last_remote_job_poll = now_iso()

                for job in remote_terminal:
                    await self._process_terminal_job(job)
            except Exception as e:
                logger.debug("event=job_poller_error err=%s", e)

            try:
                await asyncio.wait_for(asyncio.sleep(30), timeout=30)
            except asyncio.TimeoutError:
                pass

    def _remote_jobs_client(self):
        """Return a task-server client for CONTROLLER_URL, if this gateway has one."""
        controller_url = os.environ.get("CONTROLLER_URL", "").strip().rstrip("/")
        token = config.mesh.worker_token
        if not controller_url or not token:
            return None
        try:
            from src.control.task_server_client import TaskServerClient
            return TaskServerClient(controller_url, token, timeout=2)
        except Exception as e:
            logger.debug("event=remote_jobs_client_unavailable err=%s", e)
            return None

    def list_watched_jobs(
        self,
        limit: int = 20,
        session_id: Optional[str] = None,
        ownership: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return watched jobs visible to this gateway.

        The local Web UI can run on a machine whose MCP/worker points at a
        remote task server via CONTROLLER_URL. In that topology, local SQLite is
        empty but the real watched jobs live on the controller. Merge both views
        by job id so System > Jobs reflects the actual registration target.
        When session_id is supplied, return only jobs owned by that session so
        Session/Project surfaces do not have to filter a global operator list.
        """
        running: List[Dict[str, Any]] = []
        recent: List[Dict[str, Any]] = []
        local_ownership = ownership
        remote_ownership = None if ownership == "unowned" else ownership
        remote_limit = max(limit, 50) if ownership == "unowned" else limit
        try:
            from src.control.db import get_db
            db = get_db()
            if db is not None:
                running.extend(
                    db.list_jobs(
                        status="running",
                        session_id=session_id,
                        ownership=local_ownership,
                        limit=limit,
                    )
                )
                recent.extend(
                    db.list_jobs(
                        session_id=session_id,
                        ownership=local_ownership,
                        limit=limit,
                    )
                )
        except Exception as e:
            logger.debug("event=local_jobs_list_failed err=%s", e)

        remote = self._cached_remote_watched_jobs(
            limit=remote_limit,
            session_id=session_id,
            ownership=remote_ownership,
        )
        running.extend(remote["running"])
        recent.extend(remote["recent"])
        running = self._normalize_job_ownership(running)
        recent = self._normalize_job_ownership(recent)
        if ownership == "unowned":
            running = [j for j in running if not j.get("session_id") or bool(j.get("orphaned"))]
            recent = [j for j in recent if not j.get("session_id") or bool(j.get("orphaned"))]

        return {
            "running": self._dedupe_jobs(running, limit),
            "recent": self._dedupe_jobs(recent, limit),
        }

    def _normalize_job_ownership(self, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Recompute UI reachability for watched-job session links.

        A remote task server can only compare a job's ``session_id`` against its
        own SQLite ``sessions`` table. The web gateway is the place that knows
        whether that session is reachable in this UI, so remote ``orphaned``
        flags are advisory until reconciled here.
        """
        out: List[Dict[str, Any]] = []
        for job in jobs:
            item = dict(job)
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                item["orphaned"] = 0
            elif self._job_session_reachable(session_id):
                item["orphaned"] = 0
            else:
                item["orphaned"] = 1
            out.append(item)
        return out

    def _job_session_reachable(self, session_id: str) -> bool:
        store = getattr(self, "session_store", None)
        if store is not None:
            try:
                if store.get(session_id) is not None:
                    return True
            except Exception:
                pass
        try:
            from src.control.db import get_db
            db = get_db()
            if db is not None and db.get_session(session_id):
                return True
        except Exception:
            pass
        return False

    def _cached_remote_watched_jobs(
        self,
        *,
        limit: int,
        session_id: Optional[str],
        ownership: Optional[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        if not hasattr(self, "_watched_jobs_cache_lock"):
            self._watched_jobs_cache_lock = threading.Lock()
        if not hasattr(self, "_watched_jobs_remote_cache"):
            self._watched_jobs_remote_cache = {}
        if not hasattr(self, "_watched_jobs_remote_cache_ttl_sec"):
            self._watched_jobs_remote_cache_ttl_sec = 2.0

        cache_key = (session_id, ownership, limit)
        now = time.monotonic()
        cached = self._watched_jobs_remote_cache.get(cache_key)
        if cached and now - cached[0] <= self._watched_jobs_remote_cache_ttl_sec:
            return cached[1]

        if not self._watched_jobs_cache_lock.acquire(blocking=False):
            return cached[1] if cached else {"running": [], "recent": []}

        try:
            cached = self._watched_jobs_remote_cache.get(cache_key)
            now = time.monotonic()
            if cached and now - cached[0] <= self._watched_jobs_remote_cache_ttl_sec:
                return cached[1]

            client = self._remote_jobs_client()
            if client is None:
                return {"running": [], "recent": []}

            result = {
                "running": client.list_jobs(
                    status="running",
                    session_id=session_id,
                    ownership=ownership,
                    limit=limit,
                ),
                "recent": client.list_jobs(
                    session_id=session_id,
                    ownership=ownership,
                    limit=limit,
                ),
            }
            self._watched_jobs_remote_cache[cache_key] = (now, result)
            return result
        finally:
            self._watched_jobs_cache_lock.release()

    def _dedupe_jobs(self, jobs: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for job in jobs:
            job_id = str(job.get("id") or "")
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            out.append(job)
            if len(out) >= limit:
                break
        return out

    def _remote_terminal_jobs_since(self, since: str) -> List[Dict[str, Any]]:
        client = self._remote_jobs_client()
        if client is None:
            return []
        started_after = float(getattr(self, "_remote_job_poll_started_epoch", 0.0) or 0.0)
        terminal: List[Dict[str, Any]] = []
        for job in client.list_jobs(limit=50):
            job_id = str(job.get("id") or "")
            if not job_id or job_id in self._processed_terminal_jobs:
                continue
            if str(job.get("status") or "") not in {"done", "failed", "lost"}:
                continue
            started_epoch = job.get("started_epoch")
            if isinstance(started_epoch, (int, float)) and started_epoch < started_after:
                continue
            if started_epoch is None and str(job.get("updated_at") or "") <= since:
                continue
            terminal.append(job)
        return terminal

    def _job_notification_payload(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(job.get("id") or "")
        label = str(job.get("label") or job_id or "unknown")
        status = str(job.get("status") or "done")
        exit_code = job.get("exit_code")
        tail = str(job.get("tail") or "")
        success = status == "done"

        prompt = f"Watched job finished: {label}"
        lines = [f"Watched job `{label}` {status}."]
        if exit_code is not None:
            lines.append(f"Exit code: `{exit_code}`")
        if job.get("notify_agent"):
            lines.append("Agent continuation requested.")
        if tail:
            lines.append(f"\nLast log lines:\n```\n{tail[-1500:]}\n```")

        return {
            "job_id": job_id,
            "label": label,
            "status": status,
            "success": success,
            "prompt": prompt,
            "reply": "\n".join(lines),
        }

    def _record_job_session_turn(self, job: Dict[str, Any], session: Any, payload: Dict[str, Any]) -> None:
        job_id = str(payload["job_id"])
        reply = str(payload["reply"])
        prompt = str(payload["prompt"])
        success = bool(payload["success"])
        now = now_iso()

        try:
            from src.control.db import get_db
            db = get_db()
            if db is not None:
                db.enqueue_task(
                    task_id=job_id,
                    session_id=session.session_id,
                    machine_id=getattr(session, "machine_id", None),
                    backend=getattr(session, "backend", None) or "unknown",
                    action="watched_job",
                    payload={
                        "task": {"id": job_id, "title": prompt, "prompt": prompt},
                        "job": {
                            "id": job_id,
                            "label": payload["label"],
                            "status": payload["status"],
                        },
                    },
                )
                result_dict = {
                    "success": success,
                    "output": reply,
                    "errors": [] if success else [reply],
                    "files_modified": [],
                    "execution_time": 0.0,
                    "timestamp": now,
                    "return_code": job.get("exit_code"),
                }
                if success:
                    db.complete_task(job_id, result_dict, None)
                else:
                    db.fail_task(job_id, reply, result=result_dict)
                try:
                    with db._write() as conn:
                        conn.execute(
                            """
                            UPDATE mesh_tasks
                            SET created_at = ?, completed_at = ?, updated_at = ?
                            WHERE id = ? AND action = 'watched_job'
                            """,
                            (now, now, now, job_id),
                        )
                except Exception as e:
                    logger.debug("event=job_session_turn_time_update_failed job_id=%s err=%s", job_id, e)
                db.enrich_task(
                    job_id,
                    prompt=prompt,
                    reply_text=reply,
                    parsed_output={"type": "watched_job", "job": job},
                    files_modified=[],
                    return_code=job.get("exit_code"),
                )
                db.append_event(
                    session_id=session.session_id,
                    task_id=job_id,
                    success=success,
                    execution_time=0.0,
                    error="" if success else reply,
                )
        except Exception as e:
            logger.warning("event=job_session_turn_db_failed job_id=%s err=%s", job_id, e)

        try:
            history = list(getattr(session, "task_history", None) or [])
            exists = any(
                item.get("task_id") == job_id
                for item in history
                if isinstance(item, dict)
            )
            if not exists:
                history.append({
                    "task_id": job_id,
                    "timestamp": now,
                    "success": success,
                    "execution_time": 0.0,
                    "user_message": prompt,
                    "result_summary": reply,
                    "files_modified": [],
                })
                session.task_history = history[-20:]
            session.last_task_id = job_id
            session.last_result_summary = reply[-400:] if len(reply) > 400 else reply
            session.last_summary = session.last_result_summary
            session.last_files_modified = []
            self.session_store.save(session)
        except Exception as e:
            logger.warning("event=job_session_turn_save_failed job_id=%s err=%s", job_id, e)

    async def _process_terminal_job(self, job: Dict[str, Any]) -> None:
        job_id_key = str(job.get("id") or "")
        processed = getattr(self, "_processed_terminal_jobs", None)
        if processed is None:
            processed = set()
            self._processed_terminal_jobs = processed
        if job_id_key in processed:
            return
        processed.add(job_id_key)

        if not job.get("notify") and not job.get("notify_agent"):
            return

        payload = self._job_notification_payload(job)
        job_id = str(payload["job_id"])
        session_id = str(job.get("session_id") or "")
        session = self.session_store.get(session_id) if session_id else None
        if session is None:
            logger.info("event=job_notify_skipped job_id=%s reason=no_session", job_id)
            return

        self._record_job_session_turn(job, session, payload)

        if job.get("notify"):
            result = TaskResult(
                task_id=job_id,
                success=bool(payload["success"]),
                output=str(payload["reply"]),
                errors=[] if payload["success"] else [str(payload["reply"])],
                files_modified=[],
                execution_time=0.0,
                timestamp=now_iso(),
                return_code=job.get("exit_code") or 0,
            )
            try:
                await self.notifier.notify_task_outcome(
                    job_id,
                    result,
                    session=session,
                    chat_id=getattr(session, "telegram_chat_id", None),
                )
            except Exception as e:
                logger.warning("event=job_notify_failed job_id=%s err=%s", job_id, e)

        if job.get("notify_agent"):
            description = (
                f"The watched job `{payload['label']}` finished with status "
                f"`{payload['status']}`.\n\n{payload['reply']}"
            )
            try:
                await self.submit_instruction(
                    description,
                    session_id=session.session_id,
                    cwd=getattr(session, "repo_path", None),
                    source="watched_job",
                    extra_metadata={"job_id": job_id, "source": "watched_job"},
                    # [M2] This follow-up task is dispatched BY the watched-job
                    # subsystem — record that provenance on its flow_runs row
                    # (flag-guarded; no-op when HARNESS_FLOW_DRIVE is OFF).
                    dispatched_by=f"watched_job:{job_id}",
                )
            except Exception as e:
                logger.warning("event=job_notify_agent_failed job_id=%s err=%s", job_id, e)

    @staticmethod
    def _format_file_change_lines(result: TaskResult, limit: int = 20) -> List[str]:
        changes = list(getattr(result, "file_changes", None) or [])
        if changes:
            lines: List[str] = []
            for item in changes[:limit]:
                path = item.get("path", "")
                change_type = str(item.get("change_type", "modified")).capitalize()
                added = item.get("added_lines")
                deleted = item.get("deleted_lines")
                stats = ""
                if added is not None or deleted is not None:
                    stats = f" (+{added if added is not None else '?'}/-{deleted if deleted is not None else '?'})"
                lines.append(f"  `{path}` [{change_type}{stats}]")
            if len(changes) > limit:
                lines.append(f"  _...and {len(changes) - limit} more_")
            return lines

        files = result.files_modified or []
        lines = [f"  `{f}`" for f in files[:limit]]
        if len(files) > limit:
            lines.append(f"  _...and {len(files) - limit} more_")
        return lines

    @staticmethod
    def _is_missing_backend_conversation(result: TaskResult) -> bool:
        texts = list(result.errors or [])
        po = getattr(result, "parsed_output", None)
        if isinstance(po, dict):
            maybe_errors = po.get("errors")
            if isinstance(maybe_errors, list):
                texts.extend(str(item) for item in maybe_errors)
        haystack = "\n".join(str(item) for item in texts).lower()
        return "no conversation found with session id" in haystack
    
    # ===========================================================================
    # LIFECYCLE — START / STOP
    # start() boots the worker pool, embedded servers, file watcher, and all
    # background loops (stale-busy reconciler, job poller, wake dispatcher).
    # stop() drains the queue, cancels workers, and shuts down servers cleanly.
    # reload_worker_pool() adjusts the pool size at runtime without a restart.
    # ===========================================================================

    async def start(self):
        """Start all system components.

        Actions:
        - Check component availability (Claude CLI, LLAMA)
        - Spawn worker coroutines up to `config.system.max_concurrent_tasks`
        - Resume any pending files captured in persisted state
        - Start the file watcher to ingest newly created task files
        """
        if self.running:
            logger.warning("Orchestrator is already running")
            return
        
        logger.info("Starting Telegram Coding Gateway...")

        # Mark running BEFORE starting workers so they don't immediately exit
        self.running = True

        # Start task processing workers
        for i in range(config.system.max_concurrent_tasks):
            worker = asyncio.create_task(self._task_worker(f"worker-{i}"))
            self.worker_tasks.append(worker)

        # Start the embedded mesh task server (no-op unless MESH_ENABLED)
        await self._start_embedded_task_server()
        # Quota observation is disabled by default and observe-only when enabled.
        if self.quota_coordinator is not None:
            await self.quota_coordinator.start()
        # The prewarmer is the one path that may SPEND without a user asking —
        # separately flagged, off by default, and never built without the
        # coordinator it verifies against.
        if getattr(self, "quota_prewarmer", None) is not None:
            await self.quota_prewarmer.start()

        # Start the embedded control API (read surface for the Web UI)
        await self._start_embedded_control_api()

        # Resume pending before starting watcher to avoid duplicate/racy processing
        try:
            for file_path in list(self._pending_files):
                p = Path(file_path)
                processed_dir = Path(config.system.tasks_dir) / "processed"
                if p.exists() and p.parent != processed_dir:
                    await self._handle_new_task_file(file_path)
                else:
                    self._pending_files.discard(file_path)
            self._save_state()
        except Exception as e:
            logger.warning(f"event=state_resume_failed error={e}")

        # Start file watcher after resuming pending
        await self.file_watcher.start_async(self._handle_new_task_file)
        self.component_status["file_watcher_running"] = True

        # Check component availability now that all components are up
        await self._check_component_status()
        self.reconcile_spooled_mesh_completions(limit=100)
        asyncio.create_task(self._warm_llama_helpers())
        
        # Start Telegram interface if available
        if self.telegram_interface:
            try:
                await self.telegram_interface.start()
                logger.info("Telegram interface started")
            except Exception as e:
                logger.error(f"Failed to start Telegram interface: {e}")
                await self.stop()
                raise
        await self._recover_stale_busy_sessions()
        self._start_stale_busy_reconciler()
        self._start_wake_dispatcher()

        # Start the job completion poller (T3 — Watched Jobs)
        asyncio.create_task(self._job_completion_poller())

        # Log startup status
        self._log_startup_status()
        
        logger.info("Telegram Coding Gateway started successfully!")
    
    async def stop(self):
        """Stop orchestrator and all workers.

        Ensures graceful cancellation of workers and stops the file watcher.
        """
        if not self.running:
            return
        
        logger.info("Stopping Telegram Coding Gateway...")
        
        self.running = False

        interrupted_ids = list(self.active_tasks.keys())
        for task_id in interrupted_ids:
            self._shutdown_interrupted_tasks.add(task_id)
            ev = self._task_cancel_events.get(task_id)
            if ev is None:
                ev = asyncio.Event()
                self._task_cancel_events[task_id] = ev
            ev.set()
            exec_task = self._running_exec_tasks.get(task_id)
            if exec_task is not None and not exec_task.done():
                exec_task.cancel()

        if interrupted_ids:
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if not any(task_id in self.active_tasks for task_id in interrupted_ids):
                    break
                await asyncio.sleep(0.1)

        # Terminate any live backend child processes before worker cancellation.
        for backend in self._backends.values():
            terminate = getattr(backend, "terminate_active_processes", None)
            if callable(terminate):
                try:
                    terminate()
                except Exception as e:
                    logger.warning(f"Failed to terminate backend processes: {e}")
        
        # Stop Telegram interface if available
        if self.telegram_interface:
            try:
                await self.telegram_interface.stop()
                logger.info("Telegram interface stopped")
            except Exception as e:
                logger.error(f"Failed to stop Telegram interface: {e}")
        
        if getattr(self, "quota_prewarmer", None) is not None:
            await self.quota_prewarmer.stop()
        if self.quota_coordinator is not None:
            await self.quota_coordinator.stop()
        # Stop file watcher
        await self.file_watcher.stop_async()
        self.component_status["file_watcher_running"] = False

        # Stop the embedded mesh task server (no-op if it was never started)
        await self._stop_embedded_task_server()

        # Stop the embedded control API (no-op if it was never started)
        await self._stop_embedded_control_api()

        if self._stale_busy_reconcile_task and not self._stale_busy_reconcile_task.done():
            self._stale_busy_reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stale_busy_reconcile_task
        self._stale_busy_reconcile_task = None

        if self._wake_dispatcher_task and not self._wake_dispatcher_task.done():
            self._wake_dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_dispatcher_task
        self._wake_dispatcher_task = None

        # Cancel worker tasks
        for worker in self.worker_tasks:
            worker.cancel()
        
        # Wait for workers to finish
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        self.worker_tasks.clear()
        
        logger.info("Telegram Coding Gateway stopped")

    async def _start_embedded_task_server(self) -> None:
        """Start the in-process mesh task server in single-process / fallback mode.

        State Separation Phase 2: by default the task server now runs as its own
        process (server_main.py / ai-team-server) and this is a no-op. It only
        starts embedded when `MESH_EMBEDDED_SERVER=true` — the single-process or
        mesh-broken fallback mode. Running it on the gateway's event loop makes
        the HTTP handlers and the orchestrator share one get_registry() singleton.

        When embedded is off, node discovery in the remote path falls through to
        the shared DB (see _process_task_remote), so dispatch still works.
        """
        if not config.mesh.enabled:
            return
        if not config.mesh.embedded_server:
            logger.info(
                "event=embedded_task_server_skipped reason=standalone_mode "
                "(set MESH_EMBEDDED_SERVER=true to embed; otherwise run ai-team-server)"
            )
            return
        if self._embedded_task_server is not None:
            return
        host = config.mesh.tailscale_ip or "127.0.0.1"
        port = config.mesh.task_server_port
        try:
            from src.control.embedded_server import EmbeddedTaskServer
            server = EmbeddedTaskServer(host=host, port=port)
            await server.start()
            self._embedded_task_server = server
            # Bind the proactive-turn hook so autonomous (background-job) turns a
            # worker reports get delivered through the gateway's notification
            # fan-out. Capture the running loop here (we ARE on it) so the hook,
            # invoked from the server's threadpool, can marshal the async notify
            # back onto it.
            try:
                self._loop = asyncio.get_running_loop()
                from src.control import task_server as _task_server
                _task_server.bind_proactive_hook(self._handle_proactive_turn)
            except Exception as e:
                logger.warning(f"event=proactive_hook_bind_failed err={e}")
            logger.info(
                f"event=embedded_task_server_up host={host} port={port}"
            )
        except Exception as e:
            # Don't take the whole gateway down if the task server fails to bind;
            # log loudly so the operator notices mesh routing is degraded.
            logger.error(f"event=embedded_task_server_start_failed err={e}")
            self._embedded_task_server = None

    async def _stop_embedded_task_server(self) -> None:
        if self._embedded_task_server is None:
            return
        try:
            await self._embedded_task_server.stop()
        except Exception as e:
            logger.warning(f"event=embedded_task_server_stop_failed err={e}")
        finally:
            self._embedded_task_server = None

    async def _start_embedded_control_api(self) -> None:
        """Start the in-process Control API (read surface for the Web UI). U1.

        Serves /api/sessions|tasks|nodes|events on dashboard_port from inside the
        gateway, sharing this process's SessionService and NodeRegistry — the
        replacement for the standalone dashboard_main.py process. Disabled by
        CONTROL_API_ENABLED=false. A bind failure logs loudly but never takes the
        gateway down (same posture as the embedded task server).
        """
        if not config.mesh.control_api_enabled:
            logger.info("event=control_api_skipped reason=disabled (CONTROL_API_ENABLED=false)")
            return
        if self._embedded_control_apis:
            return
        hosts: list[str] = resolve_control_api_hosts(
            config.mesh.control_api_host, config.mesh.tailscale_ip
        )
        port = config.mesh.dashboard_port
        from src.control.embedded_server import EmbeddedControlServer
        started: list = []
        for host in hosts:
            try:
                server = EmbeddedControlServer(orchestrator=self, host=host, port=port)
                await server.start()
                started.append(server)
                logger.info(f"event=control_api_up host={host} port={port}")
            except Exception as e:
                # One interface failing (e.g. Tailscale not up yet) must not deny
                # the others — bind what we can, log the rest.
                logger.error(f"event=control_api_start_failed host={host} err={e}")
        self._embedded_control_apis = started

    async def _stop_embedded_control_api(self) -> None:
        if not self._embedded_control_apis:
            return
        for server in self._embedded_control_apis:
            try:
                await server.stop()
            except Exception as e:
                logger.warning(f"event=control_api_stop_failed host={server.host} err={e}")
        self._embedded_control_apis = []

    async def reload_worker_pool(self):
        """Reload worker pool size from environment configuration at runtime"""
        try:
            # Reload config from environment
            config.reload_from_env()
            target_workers = config.system.max_concurrent_tasks
            current_workers = len(self.worker_tasks)
            
            if target_workers == current_workers:
                logger.info(f"Worker pool unchanged: {current_workers} workers")
                return
            
            logger.info(f"Adjusting worker pool: {current_workers} -> {target_workers}")
            
            if target_workers > current_workers:
                # Add more workers
                for i in range(current_workers, target_workers):
                    worker = asyncio.create_task(self._task_worker(f"worker-{i}"))
                    self.worker_tasks.append(worker)
                logger.info(f"Added {target_workers - current_workers} workers")
                self._emit_event("worker_pool_scaled", None, {"from": current_workers, "to": target_workers})
                
            elif target_workers < current_workers:
                # Remove excess workers
                workers_to_remove = current_workers - target_workers
                for i in range(workers_to_remove):
                    worker = self.worker_tasks.pop()
                    worker.cancel()
                logger.info(f"Removed {workers_to_remove} workers")
                self._emit_event("worker_pool_scaled", None, {"from": current_workers, "to": target_workers})
                
        except Exception as e:
            logger.error(f"Failed to reload worker pool: {e}")
            self._emit_event("worker_pool_reload_failed", None, {"error": str(e)})
    
    async def _check_component_status(self):
        """Check availability of core components and cache status.

        Populates `self.component_status` with:
        - claude_available: Claude CLI detected and responsive
        - llama_available: optional Ollama helper path is fully usable
        - file_watcher_running: based on watcher state
        """
        
        # Check Claude Code CLI
        self.component_status["claude_available"] = self._check_claude_cli_available()
        
        # Check LLAMA availability
        llama_status = self.llama_mediator.get_status(probe=False)
        self.component_status["llama_available"] = bool(llama_status.get("helpers_enabled"))
        
        logger.info(f"Component status: {self.component_status}")

    async def _warm_llama_helpers(self) -> None:
        """Initialize optional Ollama helpers off the startup hot path."""
        try:
            llama_status = await asyncio.to_thread(self.llama_mediator.get_status, True)
            self.component_status["llama_available"] = bool(llama_status.get("helpers_enabled"))
            logger.info(
                "LLAMA helper warm-up finished: "
                f"helpers_enabled={self.component_status['llama_available']} "
                f"probe_attempted={llama_status.get('probe_attempted')}"
            )
        except Exception as e:
            logger.warning(f"LLAMA helper warm-up failed: {e}")
    
    def _log_startup_status(self):
        """Log detailed startup status"""
        status_lines = [
            "=== Telegram Coding Gateway Status ===",
            f"Claude Code CLI: {'[OK] Available' if self.component_status['claude_available'] else '[--] Not found'}",
            f"Ollama helpers: {'[OK] Available' if self.component_status['llama_available'] else '[--] Optional helper disabled'}",
            f"External task watcher: {'[OK] Running' if self.component_status['file_watcher_running'] else '[--] Stopped'}",
            f"Task Workers: {len(self.worker_tasks)} active",
            f"Watch Directory: {Path(config.system.tasks_dir).resolve()}",
            "===================================="
        ]
        
        for line in status_lines:
            logger.info(line)

    def _load_state(self) -> None:
        """Load pending state from logs/state.json"""
        try:
            if self._state_path.exists():
                import json
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                pending = data.get("pending_files", [])
                if isinstance(pending, list):
                    self._pending_files = set(map(str, pending))
        except Exception as e:
            logger.warning(f"event=state_load_failed error={e}")

    def _save_state(self) -> None:
        """Persist minimal pending state to logs/state.json"""
        try:
            import json
            payload = {
                "pending_files": sorted(self._pending_files),
                "updated": now_iso(),
            }
            self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"event=state_save_failed error={e}")

    def _update_artifact_index(self, task_id: str, artifact_path: Path) -> None:
        """Persist minimal index mapping task_id to latest artifact path."""
        try:
            import json
            idx = {}
            if self._artifact_index_path.exists():
                try:
                    idx = json.loads(self._artifact_index_path.read_text(encoding="utf-8"))
                except Exception:
                    idx = {}
            idx[str(task_id)] = str(artifact_path)
            self._artifact_index_path.parent.mkdir(parents=True, exist_ok=True)
            self._artifact_index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"event=artifact_index_save_failed task_id={task_id} error={e}")

    def _check_claude_cli_available(self) -> bool:
        """Best-effort check that Claude CLI exists and is authenticated."""
        exe = shutil.which("claude") or "claude"
        try:
            result = subprocess.run(
                [exe, "auth", "status"],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
                creationflags=_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception:
            return False

    # ===========================================================================
    # TASK CREATION & ENQUEUE
    # _make_task()    — build an in-memory Task object (does NOT enqueue).
    # _enqueue_task() — admission gate + queue push; raises HarnessAdmissionBlocked
    #                   if the level-3 guard is ON and the task is unapproved.
    #                   Also records the flow_run row (M1) and stamps dispatch
    #                   lineage (M2) when HARNESS_FLOW_DRIVE is ON.
    # ===========================================================================

    def _make_task(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        source: str = "runtime",
        extra_metadata: Optional[Dict] = None,
    ) -> Task:
        """Create an in-memory task object for direct queueing."""
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        parsed = self._parse_description_simple(description)

        if task_type:
            parsed["type"] = task_type
        if target_files:
            parsed["target_files"] = target_files

        explicit_cwd = (cwd or "").strip()
        resolved_cwd = ""
        if explicit_cwd:
            resolved = PathResolver.from_config().resolve_execution_path(explicit_cwd)
            resolved_cwd = resolved or ""

        task_type_enum = TaskType.ANALYZE
        raw_type = str(parsed.get("type", "analyze")).strip().lower()
        for candidate in TaskType:
            if candidate.value == raw_type:
                task_type_enum = candidate
                break

        metadata: Dict = {
            "session_id": session_id or "",
            "cwd": resolved_cwd,
            "source": source,
            "task_origin": "runtime",
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        task = Task(
            id=task_id,
            type=task_type_enum,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PENDING,
            created=now_iso(),
            title=parsed.get("title", "Runtime task"),
            target_files=list(parsed.get("target_files", []) or []),
            prompt=parsed.get("prompt", description),
            success_criteria=["Task completed successfully", "Results validated"],
            context=f"Generated from {source}: {description}",
            metadata=metadata,
        )
        return task

    async def _enqueue_task(self, task: Task) -> str:
        """Queue a task object directly without writing a task file.

        This is the choke point every ingestion lane passes through
        (`submit_instruction` from Telegram/Web, the `.task.md` auto-pickup path,
        and internal runtime tasks). The task-harness Level-3 admission gate runs
        HERE — before any queue/telemetry side-effect — so an un-approved Level-3
        task is refused at admission on every lane, not just `.task.md`. The gate
        is flag-gated OFF by default (`HARNESS_LEVEL3_GUARD`), so default behavior
        is byte-identical: absent flag / absent field / level ≤ 2 ⇒ pass-through.
        """
        # [Harness] Admission control (spec docs/Task_harness_workflow.md §14).
        if not self._harness_level3_allows_autopickup(task):
            logger.warning(
                f"event=task_blocked reason=harness_level3_needs_approval "
                f"task_id={task.id} source={(task.metadata or {}).get('source', 'runtime')}"
            )
            self._emit_event(
                "task_blocked",
                task,
                {"task_id": task.id, "reason": "harness_level3_needs_approval"},
            )
            raise HarnessAdmissionBlocked(task.id)

        logger.info(f"event=task_created task_id={task.id} source={(task.metadata or {}).get('source', 'runtime')}")
        self._emit_event("task_created", task, {"source": (task.metadata or {}).get("source", "runtime")})
        self._emit_event("parsed", task)

        # [FlowRun A19] Best-effort dispatch-start record. This is a RECORD only —
        # nothing reads current_stage to drive behavior. Wrapped so a DB write
        # failure can NEVER fail or delay the real task (best-effort telemetry).
        flow_run_id = self._record_flow_run_start(task)
        self._emit_turn_telemetry(
            "turn.accepted",
            task,
            {
                "task_id": task.id,
                "source": (task.metadata or {}).get("source", "runtime"),
            },
        )

        try:
            self.task_queue.put_nowait(task)
            self.active_tasks[task.id] = task
            self._emit_turn_telemetry(
                "turn.queued",
                task,
                {"priority": getattr(task.priority, "value", str(task.priority))},
            )
            logger.info(f"Queued runtime task: {task.id} ({task.type.value}, {task.priority.value})")
            # [FlowRun A19/A22] Best-effort stage transition. When HARNESS_FLOW_DRIVE
            # is OFF this is A19's exact `queued` write (byte-identical). When ON the
            # task is now admitted/queued ⇒ the objective is locked in: write the
            # §11 `objective_lock` stage instead (SHADOW record; nothing reads it).
            if self._harness_flow_drive_enabled():
                self._record_flow_stage(flow_run_id, "objective_lock")
            else:
                self._record_flow_stage(flow_run_id, "queued")
        except asyncio.QueueFull:
            priority_val = getattr(task.priority, "value", str(task.priority))
            if priority_val == "low":
                logger.warning(f"event=dropped_low_priority task_id={task.id} reason=queue_full")
                self._emit_event("dropped_low_priority", task, {"reason": "queue_full"})
                raise RuntimeError("Task queue is full")
            logger.warning(f"event=throttled task_id={task.id} reason=queue_full priority={priority_val}")
            self._emit_event("throttled", task, {"reason": "queue_full", "priority": priority_val})
            try:
                await asyncio.wait_for(self.task_queue.put(task), timeout=5.0)
                self.active_tasks[task.id] = task
                self._emit_turn_telemetry(
                    "turn.queued",
                    task,
                    {"priority": priority_val},
                )
                logger.info(f"Queued throttled runtime task: {task.id} ({task.type.value}, {priority_val})")
            except asyncio.TimeoutError as exc:
                logger.error(f"event=dropped_after_throttle task_id={task.id}")
                self._emit_event("dropped_after_throttle", task, {"timeout": 5.0})
                raise RuntimeError("Task queue is full") from exc
        return task.id

    def _record_flow_run_start(self, task: Task) -> Optional[str]:
        """Best-effort FlowRun dispatch-start record (A19, v0.4 §13 item 1).

        Writes one flow_runs row at dispatch-start. This is a RECORD only — no
        code reads current_stage to drive behavior. Any failure is swallowed and
        logged: a telemetry write must never fail or delay a real task. Returns
        the flow_run_id on success, or None if the write was skipped/failed.

        The initial stage is `intent` (the first §11 stage) when HARNESS_FLOW_DRIVE
        is ON, and A19's legacy `dispatch_start` when it is OFF — so OFF behavior is
        byte-identical to A19. The generated flow_run_id is stashed on
        ``task.metadata[_FLOW_RUN_META_KEY]`` so later transition points on the
        worker loop can resolve it without new plumbing.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return None
            drive_on = self._harness_flow_drive_enabled()

            # OFF path — byte-identical to A19: exactly one dispatch-start RECORD
            # per task, no admission, no stash. Nothing reads current_stage.
            if not drive_on:
                return db.create_flow_run(task.id, "dispatch_start")

            # ---- Flag ON: A36 Case-admission policy -------------------------
            # The retired per-turn mint is replaced. A turn now: (A) BIRTHS a Case
            # iff it is a dispatched child (M2 lineage) or an explicit managed
            # root; (B) ATTACHES to the session's open Case; or (C) runs Case-less
            # (Pattern A: standalone session, many Tasks, no Case). Only (A)
            # creates a flow_run — so a reused session no longer shatters into one
            # fake Case per turn.
            # [M2] Dispatch lineage — RECORD only. A stamped child carries
            # parent_flow_run_id / dispatched_by / dispatch_file (see
            # _stamp_child_dispatch_lineage); persisted onto the child's row so
            # child→parent is recoverable via db.list_child_flow_runs.
            lineage = self._dispatch_lineage_fields(task)
            parent_fid = lineage.get("parent_flow_run_id")
            session_id = str((task.metadata or {}).get("session_id") or "").strip()
            managed = bool((task.metadata or {}).get(self._MANAGED_CASE_META_KEY))
            join_case_id = str((task.metadata or {}).get(self._JOIN_CASE_META_KEY) or "").strip()

            # (J) JOIN — [A38] explicit Manager→worker membership. The worker task
            # ATTACHES to the named Case (a `task` link + `task.attached` event),
            # NOT a child Case. Verified open first: a closed/absent target falls
            # through to normal admission (never silently attach to a dead Case).
            # Stashed under `_CASE_ID_META_KEY` (not `_FLOW_RUN_META_KEY`) so the
            # per-turn terminal/stage helpers never fire on the shared Case ⇒ worker
            # completion leaves the Manager's Case OPEN.
            if join_case_id:
                row = db.get_flow_run(join_case_id)
                if row is not None and (row.get("status") or "") not in db._CLOSED_STATUSES:
                    self._stash_task_meta(task, self._CASE_ID_META_KEY, join_case_id)
                    # [A47] Mark the worker's task link `created_by="manager"` so the
                    # Case graph tells a dispatched WORKER's task apart from the
                    # Manager's own-turn attach (branch B, created_by="system") — the
                    # two were previously indistinguishable role="task" links. Role
                    # stays "task" (consumers/queries unchanged); created_by carries
                    # the honest provenance the read-model already surfaces.
                    self._record_flow_link(
                        join_case_id, "task", task.id, "task", created_by="manager",
                    )
                    self._record_flow_event(
                        join_case_id, "task.attached", "system",
                        entity_type="task", entity_id=task.id,
                        payload={"membership": "worker"},
                    )
                    if session_id:
                        self._set_session_case_affiliation(
                            session_id, join_case_id, role="worker",
                        )
                        # [A47] Durable session→Case link so the worker SESSION is a
                        # first-class node in the Case graph (mirrors the manager
                        # session link `open_case` writes). Idempotent on the unique
                        # key (flow_run_id, entity_type, entity_id, role) ⇒ a repeat
                        # join of the same worker session does NOT duplicate the row.
                        # Best-effort (via _record_flow_link) so a link-write failure
                        # never aborts the join.
                        self._record_flow_link(
                            join_case_id, "session", session_id, "worker",
                            created_by="manager",
                        )
                    return None

            # (B) ATTACH — a non-birthing turn on a session that already owns an
            # open Case joins it as a Task: a per-turn `task` link + a
            # `task.attached` event, and NOTHING else (no new flow_run, no second
            # session link). The Case id is stashed under `_CASE_ID_META_KEY`
            # (NOT `_FLOW_RUN_META_KEY`) precisely so the per-turn stage/terminal
            # helpers do not fire on the shared Case and auto-close it.
            if not lineage and not managed and session_id:
                open_case_id = db.find_open_case_for_session(session_id)
                if open_case_id:
                    self._stash_task_meta(task, self._CASE_ID_META_KEY, open_case_id)
                    self._record_flow_link(
                        open_case_id, "task", task.id, "task", created_by="system",
                    )
                    self._record_flow_event(
                        open_case_id, "task.attached", "system",
                        entity_type="task", entity_id=task.id,
                    )
                    self._set_session_case_affiliation(session_id, open_case_id)
                    return None

            # (C) Standalone — no dispatch lineage, not managed, no open Case to
            # join ⇒ create nothing. Ordinary ad-hoc interaction needs no Case.
            if not lineage and not managed:
                return None

            # (A) BIRTH a Case (flow_run): a dispatched task (M2 lineage — a child
            # with parent_flow_run_id, or a watched-job dispatch carrying only
            # dispatched_by) or an explicit managed root. The A26/A29 authoritative
            # machinery below now runs ONLY on a genuine Case birth — never per
            # ordinary turn.
            objective = (task.metadata or {}).get(self._MANAGED_CASE_OBJECTIVE_KEY)
            criteria = (task.metadata or {}).get(self._MANAGED_CASE_CRITERIA_KEY)
            create_fields = dict(lineage)
            if criteria:
                create_fields["completion_criteria"] = criteria
            flow_run_id = db.create_flow_run(
                task.id, "intent", objective_lock=objective, **create_fields,
            )
            if flow_run_id:
                self._stash_task_meta(task, self._FLOW_RUN_META_KEY, flow_run_id)
                # [A26] flow.created event + root_task link at the moment of birth.
                self._record_flow_event(
                    flow_run_id, "flow.created", "system",
                    to_state="intent", entity_type="task", entity_id=task.id,
                )
                self._record_flow_link(
                    flow_run_id, "task", task.id, "root_task", created_by="system",
                )
                # [A29] The session running this Case is its WORKER session — an
                # AUTHORITATIVE relationship. Absent session_id ⇒ no link (oneoff).
                if session_id:
                    self._record_flow_link(
                        flow_run_id, "session", session_id, "worker",
                        created_by="system",
                    )
                    self._record_flow_event(
                        flow_run_id, "session.attached", "system",
                        entity_type="session", entity_id=session_id,
                        payload={"role": "worker"},
                    )
                    self._set_session_case_affiliation(
                        session_id, flow_run_id, role="worker",
                    )
                # Child-flow lineage: CONSUME the edge A26a stamped (do NOT add a
                # second stamping hook). flow_links(child_flow) on the PARENT is the
                # authoritative child→parent ledger; flow_runs.parent_flow_run_id
                # (already written above) stays a convenience index.
                if parent_fid:
                    self._record_flow_link(
                        parent_fid, "flow", flow_run_id, "child_flow",
                        created_by=lineage.get("dispatched_by"),
                    )
                    self._record_flow_event(
                        parent_fid, "task.dispatched", "system",
                        entity_type="flow", entity_id=flow_run_id,
                        payload={
                            "dispatched_by": lineage.get("dispatched_by"),
                            "dispatch_file": lineage.get("dispatch_file"),
                            "child_task_id": task.id,
                        },
                    )
            return flow_run_id
        except Exception as e:
            logger.warning("event=flow_run_start_failed task_id=%s err=%s", task.id, e)
            return None

    def _stash_task_meta(self, task: "Task", key: str, value: str) -> None:
        """Best-effort stash of a value on ``task.metadata[key]``. Never raises."""
        try:
            if getattr(task, "metadata", None) is None:
                task.metadata = {}
            task.metadata[key] = value
        except Exception:
            pass

    # ===========================================================================
    # CASE MANAGEMENT  (M3 / M3.4)
    # A Case is the durable unit of Manager-supervised work.  One Manager session
    # owns one Case; workers JOIN the Case as task or session links.
    #
    # open_case()              — create a Case row + affiliate the Manager session
    # close_case()             — guard-checked close (open children, unresolved
    #                            rework, completion_criteria all checked first)
    # record_review()          — emit review.accepted / review.rework_requested
    # record_worker_wait()     — append worker.wait_pending marker
    # arm_wait_group()         — M3.4 register a wait-group on the Case
    # reconcile_worker_waits() — resolve outstanding waits against task.finished
    # _close_worker_session_on_case_close() — best-effort warm-worker cleanup
    #
    # Affiliation helpers (_set/_clear/_persist/_resolve) keep session↔case
    # membership in sync across DB and in-memory state.
    # ===========================================================================

    def _set_session_case_affiliation(
        self,
        session_id: str,
        case_id: str,
        role: Optional[str] = None,
    ) -> None:
        """[A36] Persist a session's DURABLE Case affiliation (best-effort).

        Writes ``current_case_id`` + ``case_role`` on the Session so membership
        survives across turns, replacing the per-read most-recent-link derive.
        Isolated and idempotent: a no-op when the value is already current (so a
        long-lived attachment writes ONCE, not per turn), and any failure logs and
        returns — a session write must never fail or delay admission. When ``role``
        is None it is resolved from the authoritative session→case link, defaulting
        to 'worker'. Cleared on Case close (A37).
        """
        try:
            sid = (session_id or "").strip()
            if not sid or not case_id:
                return
            store = getattr(self, "session_store", None)
            if store is None:
                return
            session = store.get(sid)
            if session is None:
                return
            # Steady-state fast path: already affiliated to this Case and no
            # explicit role override ⇒ nothing to change. Return BEFORE resolving
            # the role, so a long-lived attachment costs zero extra writes per
            # turn (the role lookup only runs on a genuine first attach / switch).
            already_here = getattr(session, "current_case_id", None) == case_id
            if role is None:
                if already_here:
                    return
                role = self._resolve_session_case_role(case_id, sid)
            if already_here and getattr(session, "case_role", None) == role:
                return  # steady-state: no redundant write
            session.current_case_id = case_id
            session.case_role = role
            store.save(session)
            # Authoritative, targeted column write — the source of truth. A generic
            # full-session save (e.g. a stale turn-end persist) can no longer clobber
            # these columns, so this write is the one that sticks.
            self._persist_session_case(sid, case_id, role)
        except Exception as e:
            logger.warning(
                "event=session_case_affiliation_failed session_id=%s err=%s",
                session_id, e,
            )

    def _persist_session_case(
        self, session_id: str, case_id: Optional[str], role: Optional[str],
    ) -> None:
        """Authoritative DB write of a session's Case affiliation (best-effort).

        Targets ONLY ``current_case_id`` / ``case_role`` via ``db.set_session_case``
        — the single column-owner path. Because the generic ``upsert_session`` no
        longer writes these columns on conflict, this write cannot be undone by a
        concurrent full-session save. Never raises."""
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.set_session_case(session_id, case_id, role)
        except Exception as e:
            logger.warning(
                "event=persist_session_case_failed session_id=%s err=%s",
                session_id, e,
            )

    def _resolve_session_case_role(self, case_id: str, session_id: str) -> str:
        """Role a session holds in a Case, read from the authoritative link.

        Defaults to 'worker' when no explicit link role is found. Never raises.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return "worker"
            links = db.list_flow_links(
                flow_run_id=case_id, entity_type="session", entity_id=session_id,
            )
            if links:
                return str(links[0].get("role") or "worker")
        except Exception:
            pass
        return "worker"

    def open_case(
        self,
        objective: str,
        session_id: str,
        role: str = "manager",
        completion_criteria: Optional[str] = None,
        round_cap: Optional[int] = None,
    ) -> Optional[str]:
        """[A36] Orchestrator seam over ``db.open_case`` — the sanctioned Case birth.

        Creates a managed Case and durably affiliates the session to it. This is
        the entrypoint the Manager role (M3.1) drives; it is NOT called inside the
        per-turn enqueue path (that is admission's job). Best-effort/isolated:
        returns the new flow_run_id, or None if the DB is unavailable / the write
        failed (a Case-birth failure must never crash the caller).
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return None
            flow_run_id = db.open_case(
                objective, session_id, role=role,
                completion_criteria=completion_criteria,
                round_cap=round_cap,
            )
            self._set_session_case_affiliation(session_id, flow_run_id, role=role)
            return flow_run_id
        except Exception as e:
            logger.warning(
                "event=open_case_failed session_id=%s err=%s", session_id, e,
            )
            return None

    def close_case(
        self,
        flow_run_id: str,
        *,
        outcome: str = "closed",
        actor: str = "operator",
        criteria_reconciliation: Optional[List[Dict[str, Any]]] = None,
        continuation_plan: Optional[str] = None,
        exhaustion_attestation: Optional[str] = None,
        close_worker_sessions: bool = False,
    ) -> Dict[str, Any]:
        """[A37] Orchestrator seam over ``db.close_case`` — authoritative closure.

        Returns ``{"ok", "closed", "reason"}``: ``ok`` False with a human ``reason``
        when the Case cannot honestly close (unresolved approval / open child work /
        unmet completion_criteria) or the id is unknown — a structured refusal, not
        an exception. On a real close, clears the durable Case affiliation of every
        session linked to the Case (A36 item 4), best-effort/isolated.

        [A48] Closing a joined worker's SESSION is NOT an automatic side-effect of
        closing the Case — the default is to leave workers WARM and reusable (their
        backend process + cache stay alive so a follow-up dispatch is a cheap resume,
        not a cold token-burn). A worker session only loses its Case *affiliation*
        (``current_case_id``→NULL) here; its process is closed only when the Manager
        explicitly decides so (via the ``release_worker`` tool → ``close_session``).
        ``close_worker_sessions=True`` opts back into the legacy PR #22 auto-close for
        callers that genuinely want it; it stays OFF by default.
        """
        from src.control.db import get_db, CaseCloseBlocked, manager_advancement_gate_enabled
        db = get_db()
        if db is None:
            return {"ok": False, "closed": False, "reason": "db_unavailable"}
        plan = (continuation_plan or "").strip()
        if actor == "manager" and not plan:
            return {
                "ok": False,
                "closed": False,
                "reason": (
                    "continuation_plan required: record the next jobs, monitoring/follow-up, "
                    "research forks, or named existing jobs that remain the priority"
                ),
            }
        # [Advancement gate] flag-gated ⇒ OFF is byte-identical. A manager close must
        # be EARNED by evidence in the ledger that the Case was actually advanced —
        # never rubber-stamped on a single accepted worker. Close is refused unless the
        # Case shows a second dispatch OR a rework verdict (ledger facts, not prose) OR
        # the manager records an explicit exhaustion_attestation tying the stop to the
        # objective. This makes "keep the goal in mind + continue the work" an enforced
        # gate, not advisory doctrine.
        attestation = (exhaustion_attestation or "").strip()
        _MIN_ATTESTATION_CHARS = 40
        if actor == "manager" and manager_advancement_gate_enabled():
            try:
                events = db.list_flow_events(flow_run_id)
            except Exception:
                events = []
            dispatch_count = sum(
                1 for e in events if e.get("event_type") == "task.dispatched"
            )
            had_rework = any(
                e.get("event_type") == "review.rework_requested" for e in events
            )
            advanced = dispatch_count >= 2 or had_rework
            if not advanced and len(attestation) < _MIN_ATTESTATION_CHARS:
                return {
                    "ok": False,
                    "closed": False,
                    "reason": (
                        "advancement gate: this Case shows no interrogation of the result "
                        "(a single dispatch, no rework). Before closing, either advance the "
                        "work — re-dispatch/derive the next loop or send the worker back with "
                        "rework — or record an `exhaustion_attestation` (>=40 chars) stating "
                        "mechanistically why this lane is exhausted against the objective and "
                        "what the next priority is."
                    ),
                }
        try:
            closed = db.close_case(
                flow_run_id, outcome=outcome, actor=actor,
                criteria_reconciliation=criteria_reconciliation,
            )
        except CaseCloseBlocked as e:
            return {"ok": False, "closed": False, "reason": e.reason}
        except ValueError as e:
            return {"ok": False, "closed": False, "reason": str(e)}
        if closed:
            try:
                for link in db.list_flow_links(
                    flow_run_id=flow_run_id, entity_type="session",
                ):
                    entity_id = str(link.get("entity_id") or "")
                    # [A48] Keep workers WARM by default: do NOT close the session
                    # process on Case close. Only the explicit opt-in re-enables the
                    # legacy auto-close. Must run BEFORE the affiliation clear below,
                    # which NULLs case_role (the guard the close path reads).
                    if close_worker_sessions:
                        self._close_worker_session_on_case_close(entity_id, flow_run_id)
                    self._clear_session_case_affiliation(entity_id, flow_run_id)
            except Exception as e:
                logger.warning(
                    "event=case_affiliation_clear_failed flow_run_id=%s err=%s",
                    flow_run_id, e,
                )
            if attestation:
                try:
                    db.append_flow_event(
                        flow_run_id,
                        "case.exhaustion_attested",
                        actor,
                        payload={"exhaustion_attestation": attestation},
                    )
                except Exception as e:
                    logger.warning(
                        "event=case_exhaustion_attest_append_failed flow_run_id=%s err=%s",
                        flow_run_id,
                        e,
                    )
            if plan:
                try:
                    db.append_flow_event(
                        flow_run_id,
                        "case.continuation_planned",
                        actor,
                        payload={"continuation_plan": plan},
                    )
                except Exception as e:
                    logger.warning(
                        "event=case_continuation_plan_append_failed flow_run_id=%s err=%s",
                        flow_run_id,
                        e,
                    )
        return {"ok": True, "closed": bool(closed), "reason": None}

    def record_review(
        self,
        flow_run_id: str,
        *,
        verdict: str,
        reason: Optional[str] = None,
        task_id: Optional[str] = None,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[M3.2] Orchestrator seam — record a Manager review verdict as a review.*
        flow_event on the Case audit trail.

        Mirrors the ``close_case`` seam: gets the db via ``get_db()``, maps the
        verdict to its canonical ``review.*`` event_type, appends the append-only
        event, and returns ``{"ok", ...}``. Returns ``{"ok": False,
        "reason": "db_unavailable"}`` when the db is unavailable and
        ``{"ok": False, "reason": "invalid_verdict"}`` for an unknown verdict.

        When ``task_id`` is supplied the verdict is TAGGED to that worker task
        (``entity_type='task'``). Beyond richer per-worker audit, a tagged review is
        read by the Wake-Dispatcher as a consumption signal: a finish the Manager
        reviewed out-of-band (e.g. during an operator poke) is no longer re-surfaced
        as a redundant continuation wake (see ``compute_continuation_tick``). Omitting
        ``task_id`` records a Case-level review exactly as before (no behaviour change).
        """
        from src.control.db import get_db, REVIEW_VERDICT_EVENT_TYPES
        event_type = REVIEW_VERDICT_EVENT_TYPES.get(verdict)
        if event_type is None:
            return {"ok": False, "reason": "invalid_verdict"}
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        event_id = db.append_flow_event(
            flow_run_id, event_type, actor,
            entity_type="task" if task_id else None,
            entity_id=task_id or None,
            payload={"verdict": verdict, "reason": reason},
        )
        return {"ok": True, "event_type": event_type, "event_id": event_id}

    def publish_artifact(
        self,
        flow_run_id: str,
        artifact_id: str,
        *,
        kind: str = "artifact",
        title: Optional[str] = None,
        uri: Optional[str] = None,
        actor: str = "manager",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """[A56/M4] Orchestrator seam — publish a durable artifact onto a Case
        (:func:`db.publish_artifact`). Mirrors the ``record_review`` seam. Flag-gated
        in the db layer (``SPEC_AUTHORING_ENABLED`` OFF ⇒ nothing written). Returns
        the db layer's ``{"ok", ...}`` (or ``{"ok": False, "reason": "db_unavailable"}``)."""
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        return db.publish_artifact(
            flow_run_id, artifact_id, kind=kind, title=title, uri=uri,
            actor=actor, metadata=metadata,
        )

    def publish_spec(
        self,
        flow_run_id: str,
        spec_id: str,
        spec_body: str,
        *,
        title: Optional[str] = None,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A56/M4] Orchestrator seam — author a spec onto a Case as durable evidence
        (:func:`db.publish_spec`). Mirrors the ``publish_artifact`` seam; flag-gated in
        the db layer. Returns the db ``{"ok", ...}`` or ``{"ok": False, "reason": "db_unavailable"}``."""
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        return db.publish_spec(flow_run_id, spec_id, spec_body, title=title, actor=actor)

    def record_spec_review(
        self,
        flow_run_id: str,
        spec_id: str,
        scores: Dict[str, Any],
        *,
        reviewer: str = "reviewer",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """[A56/M4] Orchestrator seam — score a spec against R1 by a separate
        plan-reviewer seat (:func:`db.record_spec_review`). The verdict is computed
        from the scores, not taken on trust. Mirrors the ``publish_artifact`` seam;
        flag-gated in the db layer. Returns the db ``{"ok", ...}`` or
        ``{"ok": False, "reason": "db_unavailable"}``."""
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        return db.record_spec_review(
            flow_run_id, spec_id, scores, reviewer=reviewer, reason=reason,
        )

    def decompose_case(
        self,
        flow_run_id: str,
        spec_id: str,
        tasks: List[Dict[str, Any]],
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A56/M4] Orchestrator seam — expand an APPROVED objective into a task-DAG
        of N ``task_attached`` links ON ONE CASE (:func:`db.decompose_case`). Refuses
        (writes nothing) unless the spec's latest scored review PASSED, and refuses a
        cyclic/malformed DAG. Creates ZERO new flow_runs. Mirrors the
        ``publish_artifact`` seam; flag-gated in the db layer. Returns the db
        ``{"ok", ...}`` or ``{"ok": False, "reason": "db_unavailable"}``."""
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        return db.decompose_case(flow_run_id, spec_id, tasks, actor=actor)

    def record_worker_wait(
        self,
        flow_run_id: str,
        task_id: str,
        *,
        timeout: Optional[float] = None,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A46/M3.3] Orchestrator seam — record a durable pending-wait marker for a
        dispatched worker so a resumed Manager can reconcile its outstanding waits.

        Mirrors the ``record_review`` seam: gets the db via ``get_db()`` and appends
        the append-only ``worker.wait_pending`` marker (flag-gated in the db layer).
        Returns ``{"ok": True, "event_id"}`` (event_id may be None when
        ``DURABLE_RELAY_ENABLED`` is OFF ⇒ no marker written), or
        ``{"ok": False, "reason": "db_unavailable"}``.
        """
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        event_id = db.record_worker_wait(
            flow_run_id, task_id, timeout=timeout, actor=actor,
        )
        return {"ok": True, "event_id": event_id}

    def arm_wait_group(
        self,
        flow_run_id: str,
        wait_group_id: str,
        condition: str,
        member_task_ids: List[str],
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[M3.4] Orchestrator seam — arm a Manager wait-group over a dispatch set so
        the Wake-Dispatcher re-enters the Case when the group is satisfied.

        Mirrors the ``record_worker_wait`` seam. ``condition`` ∈ {ANY, ALL, NAMED};
        the write is flag-gated in the db layer (``CASE_CONTINUATION_ENABLED`` OFF ⇒
        ``event_id`` None, nothing written). Returns ``{"ok": True, "event_id"}`` or
        ``{"ok": False, "reason": "db_unavailable"}``.
        """
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        event_id = db.arm_wait_group(
            flow_run_id, wait_group_id, condition, member_task_ids, actor=actor,
        )
        return {"ok": True, "event_id": event_id}

    def reconcile_worker_waits(
        self,
        flow_run_id: str,
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A46/M3.3] Orchestrator seam — reconcile a Case's outstanding worker waits
        against the durable ``task.finished`` events (resolve finished, report open).

        Mirrors the ``record_review`` seam. Returns the db layer's structured
        ``{"ok", "resolved", "pending"}`` (or ``{"ok": False, ...}`` when the flag is
        OFF / the db is unavailable).
        """
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        return db.reconcile_worker_waits(flow_run_id, actor=actor)

    def get_case_brief(self, flow_run_id: str) -> Dict[str, Any]:
        """[A54] Orchestrator seam — the full working state of a Case from the DB
        alone (:func:`db.get_case_brief`), for a Manager reconstructing after a
        context reset. Read-only; mirrors the ``reconcile_worker_waits`` seam.
        Returns ``{"ok": True, "brief": {...}}`` or ``{"ok": False, reason}`` for an
        unknown Case / unavailable db."""
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        brief = db.get_case_brief(flow_run_id)
        if brief is None:
            return {"ok": False, "reason": "case_not_found"}
        return {"ok": True, "brief": brief}

    def boot_reconcile_case(
        self,
        flow_run_id: str,
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A54] Orchestrator seam — the boot-time reconcile+re-arm hook
        (:func:`db.boot_reconcile_case`) a resuming Manager fires onto an existing
        OPEN Case. Idempotent + flag-gated in the db layer. Mirrors the
        ``reconcile_worker_waits`` seam."""
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return {"ok": False, "reason": "db_unavailable"}
        return db.boot_reconcile_case(flow_run_id, actor=actor)

    def _clear_session_case_affiliation(self, session_id: str, case_id: str) -> None:
        """[A37] Clear a session's durable Case affiliation on Case close.

        Only clears when the session still points at THIS Case — a session that has
        already moved to another Case is left untouched. Best-effort; never raises.
        """
        try:
            sid = (session_id or "").strip()
            if not sid:
                return
            store = getattr(self, "session_store", None)
            if store is None:
                return
            session = store.get(sid)
            if session is None:
                return
            if getattr(session, "current_case_id", None) != case_id:
                return  # already moved on / not affiliated — leave it
            session.current_case_id = None
            session.case_role = None
            store.save(session)
            # Authoritative, targeted clear — a stale full-session save can no
            # longer re-attach this session to the (now closed) Case.
            self._persist_session_case(sid, None, None)
        except Exception as e:
            logger.warning(
                "event=session_case_clear_failed session_id=%s err=%s",
                session_id, e,
            )

    def _close_worker_session_on_case_close(self, session_id: str, case_id: str) -> None:
        """[§7] Close a joined WORKER session when its Case closes.

        Only a session still affiliated to THIS Case (``current_case_id ==
        case_id``) with ``case_role == 'worker'`` is closed — the Manager
        session and any session that has already moved to another Case are
        left untouched. Must be called BEFORE ``_clear_session_case_affiliation``,
        which NULLs ``case_role``. Best-effort/isolated; never raises so one
        session's failure cannot abort the close loop.
        """
        try:
            sid = (session_id or "").strip()
            if not sid:
                return
            store = getattr(self, "session_store", None)
            if store is None:
                return
            session = store.get(sid)
            if session is None:
                return
            if getattr(session, "current_case_id", None) != case_id:
                return  # already moved on / not affiliated — leave it
            if getattr(session, "case_role", None) != "worker":
                return  # only worker sessions are closed here
            self.session_service.close_session(
                sid, backends=getattr(self, "_backends", {}),
            )
        except Exception as e:
            logger.warning(
                "event=worker_session_close_on_case_close_failed session_id=%s err=%s",
                session_id, e,
            )

    # ---------------------------------------------------------------------------
    # MANAGER INVOCATION  (M3.1)
    # Flag: MANAGER_ROLE_ENABLED (default OFF).
    #
    # invoke_manager() is the single entry point for /api/manager.  It opens a
    # Case, creates or reuses a session, applies the Manager role boot
    # (system-prompt + scoped MCP tools), and submits the first-turn objective
    # via submit_instruction().  Everything after that is driven by the Manager
    # itself (dispatch_worker → wait/arm → review → close_case).
    # ---------------------------------------------------------------------------

    def _manager_role_enabled(self) -> bool:
        """[A38] Master gate for the Phase 3.1 Manager-role invocation path.

        Default OFF ⇒ ``invoke_manager`` refuses (the new surface is inert), so the
        gateway is byte-identical to pre-A38. Mirrors the driver's
        ``_manager_role_enabled`` (same env var) — a Manager booted here only picks
        up its role prompt + scoped tools when the driver gate is also ON.
        """
        from src.control.db import manager_role_enabled
        return manager_role_enabled()

    async def invoke_manager(
        self,
        objective: str,
        *,
        repo_path: str,
        backend: str = "claude",
        model: Optional[str] = None,
        node_id: str = "__local__",
        completion_criteria: Optional[str] = None,
        context_refs: Optional[List[str]] = None,
        branch: Optional[str] = None,
        continued_from: Optional[str] = None,
        continue_inline: Optional[str] = None,
        continues: Optional[str] = None,
    ) -> Dict[str, Any]:
        """[A38] Boot a Manager session bound to one new Case (M3.1 vertical slice).

        Orchestrates: create a Session → ``open_case`` (stamps ``case_role='manager'``)
        → deliver the objective as the Manager's first assignment turn. The driver's
        ``_role_boot`` then loads the stable role prompt + scoped manager tools for
        that session. Returns ``{ok, session_id, case_id, task_id}``; a structured
        ``{ok: False, reason}`` when the role path is disabled or a step fails.
        Raises ``HarnessAdmissionBlocked`` from the first-turn submit (the caller
        translates it), exactly like the ``/api/instructions`` seam.

        [Manager-fork] Optionally seed the boot from a prior conversation:
        ``continued_from`` stamps session→session lineage; ``continue_inline`` (a
        marked-message digest) or ``continues`` (a prior task_id) inject a bounded,
        fenced ``<prior_context>`` block onto the Manager's first assignment turn via
        the existing compact-context path — so a forked Manager wakes with the prior
        line of work AND its role prompt AND its worker-dispatch tools. All three are
        optional; absent all three the boot is byte-identical to the legacy path.

        DEFERRED EDGE (A38): if the Level-3 guard blocks the first-turn submit, the
        already-created session (left IDLE, reusable) and the freshly-opened Case
        (left OPEN, visible in /api/flows) are NOT rolled back — a clean cancel is
        blocked by close_case's completion_criteria guard when criteria were set.
        Low-risk on this OFF-by-default path (a short rendered assignment rarely
        trips Level-3); revisit with a criteria-waiving cancel if it shows up live.
        """
        from src.core.interfaces import SessionOrigin
        from src.core.roles import ManagerInvocation, render_first_assignment

        if not self._manager_role_enabled():
            return {"ok": False, "reason": "manager_role_disabled"}

        # The Manager's Case machinery (per-turn attach + worker JOIN + task.finished
        # timeline) is all guarded by HARNESS_FLOW_DRIVE. With it OFF the Manager still
        # boots with its role prompt, but workers can't join the Case and wait_for_worker
        # has no timeline to watch — surface that mismatch instead of failing silently.
        if not self._harness_flow_drive_enabled():
            logger.warning(
                "event=manager_invoke_without_flow_drive session_repo=%s — MANAGER_ROLE_ENABLED "
                "is ON but HARNESS_FLOW_DRIVE is OFF; Case attach/JOIN/timeline are inert.",
                repo_path,
            )

        result = self.session_service.create_session(
            backend=backend, repo_path=repo_path, model=model, node_id=node_id,
            origin=SessionOrigin(channel="web", kind="user"), bind_chat=False,
            continued_from=continued_from,
        )
        if not getattr(result, "ok", False) or getattr(result, "session", None) is None:
            return {"ok": False, "reason": getattr(result, "reason", "create_session_failed")}
        session = result.session

        case_id = self.open_case(
            objective, session.session_id, role="manager",
            completion_criteria=completion_criteria,
        )
        if not case_id:
            return {"ok": False, "reason": "open_case_failed"}

        inv = ManagerInvocation(
            case_id=case_id, objective=objective, session_id=session.session_id,
            context_refs=list(context_refs or []), branch=branch, trigger="operator",
        )
        # [Manager-fork] Seed the FIRST assignment turn with a prior conversation via the
        # proven compact-context injector. `continue_inline` (a marked-message digest)
        # takes precedence over `continues` (a prior task_id) — the injector enforces the
        # same precedence, once-guard, fence-defusing, and hard char cap. Absent both ⇒
        # extra_metadata is None ⇒ byte-identical legacy boot turn.
        fork_meta: Optional[Dict[str, str]] = None
        if isinstance(continue_inline, str) and continue_inline.strip():
            fork_meta = {"continue_inline": continue_inline}
        elif isinstance(continues, str) and continues.strip():
            fork_meta = {"continues": continues.strip()}
        assignment = render_first_assignment(inv)
        # [Manager-fork] Point the Manager at the source session so it can pull the FULL
        # prior conversation on demand (read_session_history) — the boot excerpt is a
        # bounded snapshot, not the whole thing. Only when we actually forked one.
        if isinstance(continued_from, str) and continued_from.strip():
            assignment += (
                f"\n\nYou were forked from session {continued_from.strip()}. The prior-context "
                f"excerpt above is a bounded snapshot; to familiarize yourself with the FULL prior "
                f"line of work, call read_session_history(session_id='{continued_from.strip()}') "
                f"(page with `limit` if it is long)."
            )
        task_id = await self.submit_instruction(
            description=assignment,
            session_id=session.session_id,
            cwd=session.repo_path,
            source="manager_invoke",
            extra_metadata=fork_meta,
        )
        return {
            "ok": True,
            "session_id": session.session_id,
            "case_id": case_id,
            "task_id": task_id,
        }

    # ===========================================================================
    # FLOW TRACKING  (M1 flow_runs / M2 flow_events + flow_links)
    # Flag: HARNESS_FLOW_DRIVE (default OFF ⇒ shadow-only, nothing reads stage).
    #
    # These are WRITE-ONLY observability records.  No execution path reads
    # current_stage or flow_events to decide what runs — they exist purely for
    # the read model (/api/flows, /api/cases/{id}/timeline, the Web UI Work tab).
    #
    # _record_flow_stage()         — update flow_runs.current_stage (A19/M1)
    # _record_flow_link()          — append to flow_links (M2 dispatch lineage)
    # _record_flow_event()         — append to flow_events (M2 event ledger)
    # _stamp_child_dispatch_lineage() — set parent_flow_run_id on child at dispatch
    # _flow_stage_transition()     — convenience: stage + event in one call
    # _flow_terminal_outcome()     — write terminal stage + emit terminal event
    # ===========================================================================

    def _record_flow_stage(self, flow_run_id: Optional[str], stage: str) -> None:
        """Best-effort FlowRun stage-transition update (A19). Swallows failures.

        SHADOW ONLY: this WRITES current_stage; nothing reads it to decide what
        runs. Wrapped so a write failure logs and returns — it can never raise
        into task execution.
        """
        if not flow_run_id:
            return
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.update_flow_stage(flow_run_id, stage)
        except Exception as e:
            logger.warning("event=flow_run_stage_failed flow_run_id=%s err=%s", flow_run_id, e)

    def _record_flow_link(
        self,
        flow_run_id: Optional[str],
        entity_type: str,
        entity_id: str,
        role: str,
        created_by: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """[A26] Best-effort authoritative case↔entity link. Swallows failures.

        SHADOW/RECORD ONLY — a relationship row, never read to drive execution.
        Idempotent at the DB layer (unique-keyed). Wrapped so any failure logs and
        returns; a link write can NEVER raise into task execution.
        """
        if not flow_run_id:
            return
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.create_flow_link(
                flow_run_id, entity_type, entity_id, role,
                created_by=created_by, metadata=metadata,
            )
        except Exception as e:
            logger.warning(
                "event=flow_link_failed flow_run_id=%s role=%s err=%s",
                flow_run_id, role, e,
            )

    def _record_flow_event(
        self,
        flow_run_id: Optional[str],
        event_type: str,
        actor: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """[A26] Best-effort append-only case lifecycle event. Swallows failures.

        SHADOW/RECORD ONLY — audit trail, never read to drive execution. Wrapped
        so any failure logs and returns; an event write can NEVER raise into task
        execution.
        """
        if not flow_run_id:
            return
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.append_flow_event(
                flow_run_id, event_type, actor,
                from_state=from_state, to_state=to_state,
                entity_type=entity_type, entity_id=entity_id, payload=payload,
            )
        except Exception as e:
            logger.warning(
                "event=flow_event_failed flow_run_id=%s type=%s err=%s",
                flow_run_id, event_type, e,
            )

    # [FlowRun A22] Metadata key under which the flow_run_id is stashed on a task
    # so the worker-loop transition points (execution / impl_review / closure)
    # can resolve it. Underscored so it never collides with a user metadata field.
    _FLOW_RUN_META_KEY = "__flow_run_id"

    # [A36] Metadata keys for Case admission. `_CASE_ID_META_KEY` stashes the
    # SHARED Case a turn ATTACHED to — deliberately DISTINCT from
    # `_FLOW_RUN_META_KEY` so the per-turn stage/terminal helpers (which key off
    # `_FLOW_RUN_META_KEY`) never fire on the shared Case and auto-close it. The
    # `_MANAGED_CASE_*` keys mark a task as an explicit managed-Case root (the
    # producer is the Manager role / open_case dispatch at M3.1); when present,
    # `_record_flow_run_start` BIRTHS a Case for the task rather than attaching.
    _CASE_ID_META_KEY = "__case_id"
    # [A38] Explicit "join this Case" signal (Manager→worker membership). When a
    # worker dispatch carries it, `_record_flow_run_start` ATTACHES the task to the
    # named open Case (a `task` link) instead of birthing a child Case — distinct
    # from `_PARENT_FLOW_RUN_META_KEY` (which births a child Case with lineage).
    _JOIN_CASE_META_KEY = "__join_case_id"
    _MANAGED_CASE_META_KEY = "__managed_case"
    _MANAGED_CASE_OBJECTIVE_KEY = "__managed_case_objective"
    _MANAGED_CASE_CRITERIA_KEY = "__managed_case_criteria"

    # [M2] Dispatch-lineage metadata keys. When a parent flow/task dispatches a
    # child task, these are stamped onto the CHILD task's metadata (flag-guarded,
    # by _stamp_child_dispatch_lineage) so _record_flow_run_start can persist them
    # onto the child's flow_runs row. Underscored so they never collide with a
    # user metadata field. RECORD only — nothing reads them to drive execution.
    _PARENT_FLOW_RUN_META_KEY = "__parent_flow_run_id"
    _DISPATCHED_BY_META_KEY = "__dispatched_by"
    _DISPATCH_FILE_META_KEY = "__dispatch_file"

    def _dispatch_lineage_fields(self, task: "Task") -> Dict[str, str]:
        """[M2] Extract the flow_runs lineage columns from a child task's metadata.

        Reads the lineage keys a spawn site stamped on the child (via
        _stamp_child_dispatch_lineage) and maps them to create_flow_run kwargs
        (parent_flow_run_id / dispatched_by / dispatch_file). Only present keys
        are returned, so an unstamped task yields ``{}`` (⇒ NULL columns). Pure
        read of metadata; never raises (returns ``{}`` on any error).
        """
        try:
            meta = getattr(task, "metadata", None) or {}
            fields: Dict[str, str] = {}
            parent = meta.get(self._PARENT_FLOW_RUN_META_KEY)
            if parent:
                fields["parent_flow_run_id"] = parent
            dispatched_by = meta.get(self._DISPATCHED_BY_META_KEY)
            if dispatched_by:
                fields["dispatched_by"] = dispatched_by
            dispatch_file = meta.get(self._DISPATCH_FILE_META_KEY)
            if dispatch_file:
                fields["dispatch_file"] = dispatch_file
            return fields
        except Exception:
            return {}

    def _stamp_child_dispatch_lineage(
        self,
        child_task: "Task",
        parent_task: Optional["Task"] = None,
        *,
        parent_flow_run_id: Optional[str] = None,
        dispatched_by: Optional[str] = None,
        dispatch_file: Optional[str] = None,
    ) -> None:
        """[M2] Stamp dispatch-lineage onto a CHILD task before it is enqueued.

        Called at the seam where one flow/task dispatches another. The parent's
        own flow_run_id is stashed on ``parent_task.metadata[_FLOW_RUN_META_KEY]``
        (set by _record_flow_run_start); this copies it — plus dispatched_by and
        dispatch_file — onto the child so its flow_runs row records the link.

        [A32] A caller that only has the parent's *loose* flow_run_id (e.g. the
        HTTP ``/api/instructions`` path, where a Manager session passes its own
        case id but there is no in-process parent ``Task`` object) may pass
        ``parent_flow_run_id`` directly. An explicit value takes precedence over
        one derived from ``parent_task``; either seam records the same edge.

        Flag-guarded: when HARNESS_FLOW_DRIVE is OFF this is a NO-OP — the child's
        metadata is left untouched ⇒ byte-identical to today. SHADOW/best-effort:
        wrapped so any failure logs and returns; it can NEVER raise into the
        dispatch path. Nothing reads the stamped keys to drive execution.
        """
        try:
            if not self._harness_flow_drive_enabled():
                return
            # Explicit loose id (A32 HTTP seam) wins; else derive from parent_task.
            if parent_task is not None:
                pmeta = getattr(parent_task, "metadata", None) or {}
                if not parent_flow_run_id:
                    parent_flow_run_id = pmeta.get(self._FLOW_RUN_META_KEY)
                if dispatched_by is None:
                    dispatched_by = getattr(parent_task, "id", None)
            if not (parent_flow_run_id or dispatched_by or dispatch_file):
                return
            if child_task.metadata is None:
                child_task.metadata = {}
            if parent_flow_run_id:
                child_task.metadata[self._PARENT_FLOW_RUN_META_KEY] = parent_flow_run_id
            if dispatched_by:
                child_task.metadata[self._DISPATCHED_BY_META_KEY] = dispatched_by
            if dispatch_file:
                child_task.metadata[self._DISPATCH_FILE_META_KEY] = dispatch_file
        except Exception as e:
            logger.warning(
                "event=dispatch_lineage_stamp_failed child_task_id=%s err=%s",
                getattr(child_task, "id", "?"), e,
            )

    @staticmethod
    def _harness_flow_drive_enabled() -> bool:
        """Whether authoritative stage transitions are written (A22).

        Opt-in via ``HARNESS_FLOW_DRIVE`` (truthy: 1/true/yes/on); default OFF.
        When OFF, flow-stage behavior is byte-identical to A19 (legacy
        `dispatch_start`/`queued` record only). When ON, the §11 vocabulary
        (FLOW_STAGES) is written at each harness transition — a SHADOW record:
        NO code path reads current_stage to decide what runs.
        """
        from src.control.db import flow_drive_enabled
        return flow_drive_enabled()

    def _flow_stage_transition(self, task: "Task", stage: str) -> None:
        """[A22] Single flag-guarded, best-effort stage-transition helper.

        Called at each harness transition on the loop/driver surface. When
        HARNESS_FLOW_DRIVE is OFF this is a no-op (⇒ byte-identical to A19). When
        ON it resolves the flow_run_id stashed on the task and writes the given
        FLOW_STAGES vocabulary stage (update_flow_stage also stamps updated_at).

        SHADOW ONLY — this only ever WRITES current_stage. It is wrapped so any
        failure logs and returns; it can NEVER raise into task execution.
        """
        try:
            if not self._harness_flow_drive_enabled():
                return
            meta = getattr(task, "metadata", None) or {}
            flow_run_id = meta.get(self._FLOW_RUN_META_KEY)
            if not flow_run_id:
                return
            self._record_flow_stage(flow_run_id, stage)
            # [A26] Mirror the transition into the append-only case audit trail.
            # current_stage stays the mutable summary; flow_events is the trail.
            self._record_flow_event(
                flow_run_id, "flow.stage_changed", "system", to_state=stage,
            )
        except Exception as e:
            logger.warning(
                "event=flow_stage_transition_failed task_id=%s stage=%s err=%s",
                getattr(task, "id", "?"), stage, e,
            )

    def _flow_terminal_outcome(
        self, task: "Task", *, success: bool, error_class: str = "",
    ) -> None:
        """[A37] Record a task's terminal outcome as a `task.finished` case event.

        **Task-only** (the A37 correction of A29): a task ending updates TASK state
        only — it does NOT write ``flow_runs.status``. ``Task finished != Case
        completed``: a completed or failed task leaves its Case OPEN; a Case's
        status changes solely via an authoritative ``close_case`` (or a real
        reviewer at M3.2), never as a task-end side effect.

        Emits one append-only ``task.finished`` event (compact outcome reference)
        onto the task's owning Case — resolving the Case id from either the birth
        key (``_FLOW_RUN_META_KEY``, a dispatched/managed root) or the attach key
        (``_CASE_ID_META_KEY``, an ordinary turn on a shared Case) so both the
        first and Nth turn of a Case leave an honest audit trail. Flag-guarded
        (no-op when OFF ⇒ byte-identical) and best-effort/isolated — a write
        failure logs and returns; it can NEVER raise into task execution.
        """
        try:
            if not self._harness_flow_drive_enabled():
                return
            meta = getattr(task, "metadata", None) or {}
            # Birth case (owns a flow_run) OR the shared Case an ordinary turn
            # attached to — either way the task ran under this Case.
            flow_run_id = meta.get(self._FLOW_RUN_META_KEY) or meta.get(self._CASE_ID_META_KEY)
            if not flow_run_id:
                return
            self._record_flow_event(
                flow_run_id, "task.finished", "system",
                entity_type="task", entity_id=getattr(task, "id", None),
                payload={
                    "outcome": "success" if success else "failed",
                    "error_class": (error_class or None) if not success else None,
                },
            )
        except Exception as e:
            logger.warning(
                "event=flow_terminal_outcome_failed task_id=%s err=%s",
                getattr(task, "id", "?"), e,
            )

    # ===========================================================================
    # TASK SUBMISSION & CONTEXT INJECTION                        ★ ENTRY POINT
    # submit_instruction() is the canonical inbound gate called by Telegram,
    # the Web UI control API, and invoke_manager().  It builds the Task, optionally
    # injects compact prior-context or restart-recovery context, then calls
    # _enqueue_task().
    #
    # Context injection (both opt-in, neither changes task logic):
    #   _maybe_inject_compact_context()       — prepend `continues:` prior context
    #   _maybe_inject_restart_recovery_context() — prepend SDK-driver restart ctx
    # ===========================================================================

    # ★ PRIMARY ENTRY POINT — called by Telegram, Web UI, invoke_manager()
    async def submit_instruction(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        source: str = "telegram",
        extra_metadata: Optional[Dict] = None,
        parent_task: Optional["Task"] = None,
        parent_flow_run_id: Optional[str] = None,
        dispatched_by: Optional[str] = None,
        dispatch_file: Optional[str] = None,
        join_case_id: Optional[str] = None,
    ) -> str:
        """Direct runtime entrypoint for Telegram/CLI instructions.

        [M2] When this call is a child dispatch (a parent flow/task spawning
        another), pass ``parent_task`` (whose metadata carries the parent
        flow_run_id) and/or ``dispatched_by`` / ``dispatch_file``. These are
        stamped onto the child task's flow_runs row for lineage — but ONLY when
        HARNESS_FLOW_DRIVE is ON; otherwise stamping is a no-op ⇒ byte-identical.

        [A32] When the caller only has the parent's *loose* flow_run_id (the HTTP
        ``/api/instructions`` seam — a Manager session dispatching a worker via
        ``mcp_manager``), pass ``parent_flow_run_id`` directly; it is stamped onto
        the child's flow_runs row exactly like the ``parent_task``-derived edge.
        """
        task = self._make_task(
            description=description,
            task_type=task_type,
            target_files=target_files,
            session_id=session_id,
            cwd=cwd,
            source=source,
            extra_metadata=extra_metadata,
        )
        # [M2/A32] Stamp dispatch lineage before enqueue (flag-guarded no-op when OFF).
        if parent_task is not None or parent_flow_run_id or dispatched_by or dispatch_file:
            self._stamp_child_dispatch_lineage(
                task,
                parent_task,
                parent_flow_run_id=parent_flow_run_id,
                dispatched_by=dispatched_by,
                dispatch_file=dispatch_file,
            )
        # [A38] Manager→worker MEMBERSHIP: stash the Case to JOIN so admission
        # attaches the task to it instead of birthing a child Case. Distinct from
        # lineage above; the attach itself is flag-guarded in _record_flow_run_start.
        if join_case_id:
            self._stash_task_meta(task, self._JOIN_CASE_META_KEY, join_case_id)
        return await self._enqueue_task(task)

    async def compact_session(self, session_id: str):
        """Send /compact to the backend for the given session, collapsing context."""
        from src.core.interfaces import ExecutionResult
        session = self.session_store.get(session_id)
        if not session:
            return ExecutionResult(success=False, output="", errors=["Session not found"])
        if not session.backend_session_id:
            return ExecutionResult(success=False, output="", errors=["Session has no backend context yet"])
        backend = self._backends.get(session.backend)
        if not backend:
            return ExecutionResult(success=False, output="", errors=[f"Unknown backend: {session.backend}"])

        # If the session is pinned to a remote mesh node, dispatch there — running
        # the backend locally would use the wrong cwd (the Pi doesn't have the
        # Windows/remote path that session.repo_path points to).
        if config.mesh.enabled and session.machine_id and session.machine_id != socket.gethostname():
            compact_task = Task(
                id=f"compact-{session_id[:8]}-{int(time.time())}",
                type=TaskType.ANALYZE,
                priority=TaskPriority.HIGH,
                status=TaskStatus.PENDING,
                created=now_iso(),
                title="Compact session context",
                target_files=[],
                prompt="/compact",
                success_criteria=["Context compacted"],
                context="",
                metadata={"session_id": session_id, "source": "compact", "task_origin": "runtime"},
            )
            backend_name = session.backend or "claude"
            self._mesh_enqueue_task(compact_task, backend_name)
            result = await self._process_task_remote(compact_task, session, time.time(), config.system.task_timeout)
            from src.core.interfaces import ExecutionResult as ER
            return ER(
                success=result.success,
                output=result.output,
                errors=result.errors or [],
            )

        return await asyncio.to_thread(backend.compact_session, session)

    def load_compact_context(self, task_id: str) -> Dict[str, Any]:
        """Load compact, prompt-ready context for a given task_id.

        Delegates to a lightweight internal loader that reads the latest
        artifact via `results/index.json` with a scan fallback. Keeps output
        under small token/char caps.
        """
        if self._context_loader is None:
            from src.control.db import get_db
            self._context_loader = _ContextLoader(self._artifact_index_path, Path(config.system.results_dir), get_db)
        return self._context_loader.load(task_id)

    # Hard cap on the assembled prior-context prefix, independent of the loader's
    # own per-field caps. A bound MUST exist — an unbounded paste overflows the
    # window and re-costs tokens every turn — but the old 4000 (~700 words) was
    # far too small to actually "carry the work over". Default is a generous
    # working budget (~12k tokens) and env-tunable; when a selection still exceeds
    # it we keep the MOST RECENT tail (see `_clamp_keep_tail`), never the stale head.
    _COMPACT_PREFIX_MAX_CHARS = int(os.getenv("AI_TEAM_COMPACT_PREFIX_MAX_CHARS", "48000"))
    _COMPACT_MAX_FILES = 20

    def _clamp_keep_tail(self, text: str, budget: int) -> str:
        """Clamp `text` to `budget` chars keeping the TAIL (most recent) content.

        For "continue the work" the latest turns — current state, last decisions —
        are what matter; the head is throat-clearing. So when we must drop, we drop
        from the FRONT and mark it, rather than chopping off the end (the old bug).
        """
        if budget <= 0 or len(text) <= budget:
            return text
        marker = "…(earlier context truncated)…\n"
        keep = budget - len(marker)
        if keep <= 0:
            return text[-budget:]
        return marker + text[-keep:]

    # ── Restart-recovery context injection ─────────────────────────────────────
    # When a worker restarts (deliberate or crash) its SDK sessions get
    # driver_status='lost'. The next task for those sessions starts a fresh Claude
    # Code subprocess via create_session — it has no memory of prior turns even
    # though the conversation history is DB-canonical in mesh_tasks. This injector
    # prepends a bounded <prior_context> block of recent completed turns so the
    # new agent knows where it left off and can continue without operator nudging.
    #
    # TODO(A50-tier2): desired future state — instead of raw turn injection, spawn
    # a Haiku-class agent to summarise the session history (last ~10 turns → ≤500
    # word prose) and inject the summary. More token-efficient for long sessions;
    # same prior_context wrapper. Fallback to raw turns on Haiku call failure.
    # Not built now: adds a paid sub-call + async complexity; defer until the
    # restart path is proven stable with the simpler approach.

    _RESTART_CTX_TURN_LIMIT: int = 3          # max turn pairs to include
    _RESTART_CTX_PER_TURN_CHARS: int = 1_500  # truncate each assistant reply here
    _RESTART_CTX_TOTAL_CHARS: int = 4_000     # hard cap on the entire block
    _RESTART_CTX_MAX_AGE_HOURS: int = 24      # skip if most-recent turn is older

    @staticmethod
    def _restart_ctx_enabled() -> bool:
        # ON by default. Set RESTART_CONTEXT_RESTORE_DISABLED=true to opt out.
        from src.control.db import restart_context_restore_disabled
        return not restart_context_restore_disabled()

    async def _maybe_inject_restart_recovery_context(self, task: "Task") -> None:
        """Prepend recent completed turns when a session's driver was lost on restart.

        No-op unless:
        - RESTART_CONTEXT_RESTORE_DISABLED is not set (feature is ON by default)
        - session.driver_status == 'lost' (set by node re-registration on incarnation change)
        - session.backend_session_id is non-empty (session was previously live)
        - the session has ≥1 completed turn in mesh_tasks within the age window

        Idempotent — shares the _compact_injected_ids once-guard with
        _maybe_inject_compact_context so neither can double-inject the same task.
        Any failure is swallowed and the original prompt is left intact.
        """
        try:
            if not self._restart_ctx_enabled():
                return

            session_id = (task.metadata or {}).get("session_id", "").strip() or None
            if not session_id:
                return

            session = self.session_store.get(session_id)
            if not session:
                return
            if session.driver_status != "lost" or not session.backend_session_id:
                return

            # Once-guard: shared with compact-context so both injectors can't
            # both fire on the same task.id.
            if task.id in self._compact_injected_ids:
                return

            turns = await asyncio.to_thread(
                self._db_get_session_turns_tail, session_id
            )
            if not turns:
                return

            # Age gate — skip stale dormant sessions (e.g. a session from weeks
            # ago that was left open and got a new task for an unrelated reason).
            most_recent_ts = turns[-1].get("created_at", "")
            if most_recent_ts:
                try:
                    from src.core.timeutil import parse_iso
                    import datetime as _dt
                    age_h = (
                        _dt.datetime.now(_dt.timezone.utc) - parse_iso(most_recent_ts)
                    ).total_seconds() / 3600
                    if age_h > self._RESTART_CTX_MAX_AGE_HOURS:
                        logger.info(
                            "event=restart_context_skipped reason=stale_session "
                            "task_id=%s session_id=%s age_hours=%.1f",
                            task.id, session_id, age_h,
                        )
                        return
                except Exception:
                    pass  # can't parse age → proceed anyway

            lines: list[str] = [
                '<prior_context source="restart-recovery">',
                "Your Claude Code session was interrupted by a worker restart.",
                "The following are the most recent completed turns for continuity.",
                "Check git status and recent commits before continuing any task.",
                "",
            ]
            total_chars = 0
            turns_included = 0
            for turn in turns:
                prompt_text = (turn.get("prompt") or "").strip()
                reply_text  = (turn.get("reply_text") or "").strip()
                if len(reply_text) > self._RESTART_CTX_PER_TURN_CHARS:
                    reply_text = reply_text[:self._RESTART_CTX_PER_TURN_CHARS] + "\n… [truncated]"
                block = f"User:\n{self._defuse_fence(prompt_text)}\n\nAssistant:\n{self._defuse_fence(reply_text)}"
                if total_chars + len(block) > self._RESTART_CTX_TOTAL_CHARS:
                    lines.append("… [earlier turns omitted — total context cap reached]")
                    break
                lines.append(block)
                lines.append("---")
                total_chars += len(block)
                turns_included += 1

            lines.append("</prior_context>")
            ctx_block = "\n".join(lines)
            task.prompt = ctx_block + "\n\n" + (task.prompt or "")
            self._compact_injected_ids.add(task.id)
            logger.info(
                "event=restart_context_injected task_id=%s session_id=%s "
                "turns=%d chars=%d",
                task.id, session_id, turns_included, total_chars,
            )
        except Exception as e:
            logger.warning(
                "event=restart_context_error task_id=%s error=%s",
                getattr(task, "id", "?"), e,
            )

    def _db_get_session_turns_tail(self, session_id: str) -> list:
        """Sync wrapper — calls db.get_session_turns_tail off the event loop."""
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return []
            return db.get_session_turns_tail(session_id, self._RESTART_CTX_TURN_LIMIT)
        except Exception as e:
            logger.warning("event=restart_context_db_error session_id=%s error=%s", session_id, e)
            return []

    async def _maybe_inject_compact_context(self, task: "Task") -> None:
        """Opt-in: prepend bounded prior context when a task declares `continues:`.

        No-op (prompt untouched, no loader call) unless `task.metadata["continues"]`
        is a non-empty task id. Injects at most once per task id. Any failure is
        swallowed and the original prompt is left intact — a continuation must never
        crash a turn. The prior context is fenced as reference-only; the original
        instruction is preserved verbatim inside `<current_instruction>`.
        """
        try:
            meta = task.metadata or {}
            # [Session-fork] Inline carry-over path: a fork stashes a verbatim digest
            # of the marked messages under `continue_inline` (client-held, attached on
            # the new session's FIRST instruction). Takes precedence over `continues:`
            # (a prior task_id) — a fork never also references a parent task. Same
            # once-guard, same fence-defused reference block, same hard char cap.
            inline_raw = meta.get("continue_inline", "")
            if isinstance(inline_raw, str) and inline_raw.strip():
                if task.id in self._compact_injected_ids:
                    return
                prefix = self._build_inline_compact_prefix(inline_raw.strip())
                if not prefix:
                    return
                original = task.prompt or ""
                task.prompt = f"{prefix}\n\n<current_instruction>\n{original}\n</current_instruction>"
                self._compact_injected_ids.add(task.id)
                logger.info(
                    f"event=compact_context_injected task_id={task.id} source=inline "
                    f"prefix_chars={len(prefix)}"
                )
                return
            raw = meta.get("continues", "")
            # [F6] Coerce/validate cheaply; reject non-str (e.g. a YAML list) and blanks.
            if not isinstance(raw, str):
                if raw:
                    logger.info(f"event=compact_context_skipped reason=continues_not_string task_id={task.id}")
                return
            parent_id = raw.strip()
            if not parent_id:
                return
            # [F5/R1] Instance-local guard — inject once, never via task.metadata.
            if task.id in self._compact_injected_ids:
                return
            # [F4] Self-reference is meaningless.
            if parent_id == task.id:
                logger.info(f"event=compact_context_skipped reason=self_reference task_id={task.id}")
                return

            # [F2] Loader is sync + does DB/file IO; keep it off the event loop.
            ctx = await asyncio.to_thread(self.load_compact_context, parent_id)

            # [F4] Nothing usable to inject.
            summary = (ctx.get("summary") or "").strip()
            files = [f for f in (ctx.get("files_modified") or []) if f]
            if ctx.get("source") == "none" or (not summary and not files):
                logger.info(f"event=compact_context_skipped reason=no_prior_context task_id={task.id} parent={parent_id}")
                return

            prefix = self._build_compact_prefix(parent_id, summary, files, ctx.get("errors") or [])
            if not prefix:
                return

            original = task.prompt or ""
            task.prompt = f"{prefix}\n\n<current_instruction>\n{original}\n</current_instruction>"
            self._compact_injected_ids.add(task.id)
            logger.info(
                f"event=compact_context_injected task_id={task.id} parent={parent_id} "
                f"prefix_chars={len(prefix)} files={len(files)}"
            )
        except Exception as e:
            # [F4] Never raise into process_task; proceed with the original prompt.
            logger.warning(f"event=compact_context_error task_id={getattr(task, 'id', '?')} error={e}")

    @staticmethod
    def _defuse_fence(text: str) -> str:
        """Neutralize fence tokens inside interpolated prior-task content.

        Prior summary/files/errors are a *prior task's stored output* and are not
        trusted structure. If they contained a literal `</prior_context>` or a
        `<current_instruction>` marker, they could break out of the reference fence
        and be read as a live instruction. Strip the angle brackets on those tokens
        so the fence can't be escaped.
        """
        for tok in ("</prior_context>", "<prior_context", "<current_instruction>", "</current_instruction>"):
            text = text.replace(tok, tok.replace("<", "(").replace(">", ")"))
        return text

    def _build_compact_prefix(self, parent_id: str, summary: str, files: list, errors: list) -> str:
        """Assemble the bounded, fenced prior-context block (reference only)."""
        lines = [f'<prior_context source="task {self._defuse_fence(str(parent_id))}">']
        if summary:
            lines.append(f"summary: {self._defuse_fence(summary)}")
        if files:
            shown = [self._defuse_fence(str(f)) for f in files[: self._COMPACT_MAX_FILES]]
            more = "" if len(files) <= self._COMPACT_MAX_FILES else f" (+{len(files) - self._COMPACT_MAX_FILES} more)"
            lines.append("files_modified: " + ", ".join(shown) + more)
        if errors:
            first_err = self._defuse_fence(str(errors[0]))
            lines.append(f"prior_errors: {first_err}")
        lines.append("(Reference only. Your actual instruction follows.)")
        lines.append("</prior_context>")
        block = "\n".join(lines)
        # [F3] Hard total cap regardless of field caps. Keep the most recent tail
        # (drop from the front) so the latest state survives, not the stale head.
        if len(block) > self._COMPACT_PREFIX_MAX_CHARS:
            inner = "\n".join(lines[1:-2])  # between the open fence and the trailer
            budget = self._COMPACT_PREFIX_MAX_CHARS - len(lines[0]) - len(lines[-2]) - len(lines[-1]) - 3
            inner = self._clamp_keep_tail(inner, max(0, budget))
            block = "\n".join([lines[0], inner, lines[-2], lines[-1]])
        return block

    def _build_inline_compact_prefix(self, text: str) -> str:
        """[Session-fork] Bounded, fence-defused prior-context block from a verbatim
        digest of marked messages (the fork carry-over).

        Unlike ``_build_compact_prefix`` (which reads a prior task's stored fields),
        the source here is a client-supplied digest of hand-picked messages. It is
        untrusted structure, so it is fence-defused exactly like prior-task content
        and clamped to the SAME hard char cap — a fork can never let the reference
        block dominate or escape into a live instruction.
        """
        digest = self._defuse_fence(str(text)).strip()
        if not digest:
            return ""
        # Clamp the INNER digest (keeping the most recent tail) against a budget that
        # leaves room for the fence wrapper — so a long fork keeps its latest turns
        # instead of chopping them off, and can never blow the total cap.
        wrapper_overhead = len(
            '<prior_context source="marked messages">\n\n'
            "(Reference only. Your actual instruction follows.)\n</prior_context>"
        )
        digest = self._clamp_keep_tail(digest, self._COMPACT_PREFIX_MAX_CHARS - wrapper_overhead)
        block = (
            '<prior_context source="marked messages">\n'
            f"{digest}\n"
            "(Reference only. Your actual instruction follows.)\n"
            "</prior_context>"
        )
        return block

    async def _handle_new_task_file(self, file_path: str):
        """Handle detection of a new `.task.md` file.

        Debounces duplicates, validates format, parses into `Task`, emits events,
        and enqueues for processing.
        """
        try:
            # Normalize to absolute path so relative vs absolute variants
            # of the same file don't bypass the inflight dedup check.
            path_key = str(Path(file_path).resolve())
            if path_key in self._inflight_paths:
                logger.info(f"event=task_skipped reason=already_inflight file={file_path}")
                return
            self._inflight_paths.add(path_key)
            # Track as pending for persistence
            self._pending_files.add(path_key)
            self._save_state()

            logger.info(f"event=task_received file={file_path}")
            self._emit_event("task_received", None, {"file": file_path})
            
            # Validate task file format
            errors = self.task_parser.validate_task_format(file_path)
            if errors:
                logger.error(f"Invalid task file format: {errors}")
                # Remove from pending if file is gone or invalid
                try:
                    self._pending_files.discard(path_key)
                    self._save_state()
                except Exception:
                    pass
                # Release lock on invalid format to allow future corrections
                try:
                    self._inflight_paths.discard(path_key)
                except Exception:
                    pass
                return
            
            # Parse task
            task = self.task_parser.parse_task_file(file_path)
            task.status = TaskStatus.PENDING
            # Track source file path for post-processing archival
            try:
                if getattr(task, "metadata", None) is None:
                    task.metadata = {}
                task.metadata["__file_path"] = file_path
                task.metadata.setdefault("source", "task_file")
                task.metadata.setdefault("task_origin", "file")
            except Exception:
                pass
            logger.info(f"event=parsed task_id={task.id} type={task.type.value} priority={task.priority.value}")

            # Admission (incl. the Level-3 harness gate) now lives in
            # `_enqueue_task` — the choke point shared by every ingestion lane. A
            # blocked Level-3 `.task.md` raises HarnessAdmissionBlocked there; here
            # we just release this lane's file-tracking state so an `approved: true`
            # re-write can be picked up later. The file is left un-enqueued.
            try:
                await self._enqueue_task(task)
            except HarnessAdmissionBlocked:
                try:
                    self._pending_files.discard(path_key)
                    self._inflight_paths.discard(path_key)
                    self._save_state()
                except Exception:
                    pass
                return

        except Exception as e:
            logger.error(f"Error processing task file {file_path}: {e}")
            # Best-effort release of lock on exception
            try:
                self._inflight_paths.discard(str(file_path))
                # Also drop from pending and persist to avoid stuck entries
                self._pending_files.discard(str(file_path))
                self._save_state()
            except Exception:
                pass

    @staticmethod
    def _harness_level3_allows_autopickup(task: "Task") -> bool:
        """Task-harness Level-3 admission predicate (spec §14).

        The single decision function behind the admission gate in `_enqueue_task`
        (every ingestion lane) and the `.task.md` file lane. Pure over
        `task.metadata`, so it is trivially testable.

        Returns True (allow) in every case EXCEPT: the guard flag is enabled AND
        the task declares `harness_level: 3` AND it is not `approved: true`.
        Level ≤ 2 and any task without a `harness_level` field are always allowed
        — behavior is byte-identical to before when the field is absent or the
        flag is unset.

        The guard is opt-in via `HARNESS_LEVEL3_GUARD` (truthy: 1/true/yes/on).
        The convention (a documented rule the dispatch prompt obeys) is the primary
        control; this is the enforcement backstop for when a drafter ignores it.
        """
        from src.control.db import harness_level3_guard_enabled
        if not harness_level3_guard_enabled():
            return True  # guard off ⇒ legacy behavior

        meta = getattr(task, "metadata", None) or {}
        raw_level = meta.get("harness_level", None)
        if raw_level is None:
            return True  # field absent ⇒ unchanged

        # Coerce level defensively (YAML may give int or str); only "3" gates.
        try:
            level = int(str(raw_level).strip())
        except (TypeError, ValueError):
            return True  # unparseable level ⇒ don't invent a block
        if level != 3:
            return True  # Level ≤ 2 auto-enqueues

        approved = meta.get("approved", False)
        if isinstance(approved, str):
            approved = approved.strip().lower() in ("1", "true", "yes", "on")
        return bool(approved)

    # ===========================================================================
    # WORKER LOOP
    # Each worker coroutine pulls from task_queue, calls process_task(), persists
    # artifacts, then loops.  The pool size is config.system.max_concurrent_tasks.
    # Workers are cancelled during stop() after the queue drains.
    # ===========================================================================

    async def _task_worker(self, worker_name: str):
        """Worker coroutine that processes tasks from the queue.

        Each worker pulls tasks, calls `process_task`, and persists artifacts.
        """
        logger.info(f"Task worker {worker_name} started")
        
        while self.running:
            try:
                # Get task from queue with timeout
                task = await asyncio.wait_for(
                    self.task_queue.get(), 
                    timeout=1.0
                )
                
                # Ensure cancel event exists for this task
                cancel_ev = self._task_cancel_events.get(task.id)
                if cancel_ev is None:
                    cancel_ev = asyncio.Event()
                    self._task_cancel_events[task.id] = cancel_ev

                # If cancellation was requested before start, mark and skip
                if cancel_ev.is_set():
                    task.status = TaskStatus.FAILED
                    logger.info(f"event=cancelled_before_start worker={worker_name} task_id={task.id}")
                    self._emit_event("cancelled", task, {"worker": worker_name, "when": "before_start"})
                    self._emit_turn_telemetry(
                        "turn.cancel_requested",
                        task,
                        {"reason_code": "cancelled_before_start"},
                    )
                    self._emit_turn_telemetry(
                        "turn.completed",
                        task,
                        {
                            "status": "cancelled",
                            "timeout_status": "none",
                            "exit_code": None,
                        },
                        flush=True,
                    )
                    self.task_queue.task_done()
                    # Release inflight locks and pending state, similar to completion path
                    try:
                        if getattr(task, "metadata", None):
                            self._inflight_paths.discard(task.metadata.get("__file_path", ""))
                            self._pending_files.discard(task.metadata.get("__file_path", ""))
                            self._save_state()
                    except Exception:
                        pass
                    continue

                backend_name = self._resolve_task_backend(task)
                start_event = self._backend_event_name(backend_name, "started")
                logger.info(f"event={start_event} worker={worker_name} task_id={task.id}")
                self._emit_event(start_event, task, {"worker": worker_name, "backend": backend_name})
                self._emit_turn_telemetry("turn.started", task, backend=backend_name)
                # [Restart-recovery context] When a session's driver was lost on worker
                # restart, auto-inject the last N completed turns as <prior_context> so
                # the new Claude Code process isn't blind. No-op when
                # RESTART_CONTEXT_RESTORE_DISABLED is set or the session is
                # healthy — byte-identical to the prior behaviour in all normal paths.
                await self._maybe_inject_restart_recovery_context(task)
                # [Manager-fork / compact-context] Inject bounded prior context BEFORE the
                # mesh snapshot. `_mesh_enqueue_task` freezes `task.prompt` into the remote
                # payload (see `payload["prompt"]`), so a node worker executes exactly this
                # string — but the worker never runs the injector itself. Injecting here (vs
                # only inside `process_task`, which runs AFTER the snapshot) is what makes a
                # `continues:`/`continue_inline` turn — including a node-pinned forked Manager
                # — actually carry its prior line of work to the remote carrier. Idempotent
                # (once-guard on task.id), so the later call in `process_task` is a no-op;
                # a no-seed task is untouched ⇒ byte-identical.
                await self._maybe_inject_compact_context(task)
                self._mesh_enqueue_task(task, backend_name)

                # [FlowRun A22] Harness transition → `execution`. Flag-guarded,
                # best-effort SHADOW write (no-op when HARNESS_FLOW_DRIVE is OFF);
                # nothing below reads current_stage to decide what runs.
                self._flow_stage_transition(task, "execution")

                # Process the task
                result = await self.process_task(task)

                # Detached: the gateway is shutting down while a remote worker
                # keeps running and owns this task's real state in the DB. This
                # is NOT a failure — do not notify Telegram, do not write a
                # terminal artifact, do not mark the task FAILED. Leave the DB
                # row as 'claimed' so startup reattach reports the worker's real
                # result. Just release in-process bookkeeping and move on.
                if getattr(result, "detached", False):
                    logger.info(
                        "event=task_detached worker=%s task_id=%s reason=gateway_shutdown",
                        worker_name, task.id,
                    )
                    # Release in-process bookkeeping but DO NOT touch the DB row,
                    # Telegram, or session status — the remote worker owns this
                    # task and startup reattach will report its real result.
                    try:
                        self._running_exec_tasks.pop(task.id, None)
                        self.active_tasks.pop(task.id, None)
                        if getattr(task, "metadata", None):
                            self._inflight_paths.discard(task.metadata.get("__file_path", ""))
                    except Exception:
                        pass
                    self.task_queue.task_done()
                    continue

                # Store result
                self.task_results[task.id] = result

                # Update task status
                task.status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED

                # Log completion
                status = "SUCCESS" if result.success else "FAILED"
                finish_backend = getattr(result, "backend_name", backend_name)
                finish_event = self._backend_event_name(finish_backend, "finished")
                logger.info(f"event={finish_event} task_id={task.id} status={status} duration_s={result.execution_time:.2f} class={getattr(result,'error_class','')}")
                self._emit_event(
                    finish_event,
                    task,
                    {"status": status, "duration_s": result.execution_time, "error_class": getattr(result, "error_class", ""), "backend": finish_backend},
                )
                final_status = (
                    "success"
                    if result.success
                    else "timed_out"
                    if getattr(result, "error_class", "") == "timeout"
                    else "cancelled"
                    if any("cancelled" in str(error).lower() for error in (result.errors or []))
                    else "failed"
                )
                self._emit_turn_telemetry(
                    "turn.result_recorded",
                    task,
                    {
                        "status": final_status,
                        "error_code": getattr(result, "error_class", "") or None,
                    },
                    invocation_id=getattr(result, "telemetry_invocation_id", None),
                    backend=finish_backend,
                )
                # [A37] No auto `impl_review`/`closure` stage stamp. Those stages
                # were fabricated on EVERY task even though no reviewer/closer ran
                # (Task finished != Case completed). A task-end writes ONLY the task
                # outcome now; a Case's stage/status changes only via a real
                # reviewer (M3.2) or an authoritative close_case.
                self._emit_turn_telemetry(
                    "turn.completed",
                    task,
                    {
                        "status": final_status,
                        "timeout_status": (
                            "gateway_timeout" if final_status == "timed_out" else "none"
                        ),
                        "exit_code": getattr(result, "return_code", None),
                    },
                    invocation_id=getattr(result, "telemetry_invocation_id", None),
                    backend=finish_backend,
                    flush=True,
                )
                # [A37] Terminal OUTCOME — task-only. Records the task's result as a
                # `task.finished` case audit event WITHOUT touching flow_runs.status.
                # A completed/failed task leaves its Case OPEN; closure is a separate
                # authoritative decision (close_case), never a task-end side effect.
                self._flow_terminal_outcome(
                    task,
                    success=bool(result.success),
                    error_class=str(getattr(result, "error_class", "") or ""),
                )
                # [quota-resume] A Manager turn refused for quota PAUSES its Case
                # durably, so the reopening window (not an unrelated worker
                # finishing) is what brings it back. No-op for every other turn.
                self._record_quota_pause(task, result)
                # [transient-resume] A Manager turn that outlived its burst retries
                # on a transient provider 5xx (529 Overloaded) PAUSES its Case on a
                # short backoff so the Wake-Dispatcher self-heals it. No-op for
                # every other turn (and byte-identical when the flag is OFF).
                self._record_transient_pause(task, result)

                # Send notification via the central notification dispatcher
                try:
                    session_id_for_notify = (task.metadata or {}).get("session_id", "").strip()
                    notify_chat_id: Optional[int] = None
                    if session_id_for_notify:
                        _s = self.session_store.get(session_id_for_notify)
                        if _s:
                            notify_chat_id = _s.telegram_chat_id

                    await self.notifier.notify_task_outcome(
                        task.id,
                        result,
                        session=self.session_store.get(session_id_for_notify) if session_id_for_notify else None,
                        chat_id=notify_chat_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to send completion notification: {e}")
                
                # Write artifacts
                artifact_path: Optional[str] = None
                try:
                    self._write_artifacts(task.id, result, task=task)
                    artifact_path = str(Path(config.system.results_dir) / f"{task.id}.json")
                    logger.info(f"event=artifacts_written task_id={task.id}")
                    self._emit_event("artifacts_written", task)
                except Exception as e:
                    logger.error(f"event=artifacts_error task_id={task.id} error={e}")
                    self._emit_event("artifacts_error", task, {"error": str(e)})
                self._mesh_complete_task(task, result, artifact_path)

                # Update session record + write compact summary + per-session event log
                try:
                    session_id = (task.metadata or {}).get("session_id", "").strip()
                    if session_id:
                        session = self.session_store.get(session_id)
                        if session:
                            session.last_task_id = task.id
                            if not result.success:
                                # A failed turn may still carry a deliverable reply — e.g. a
                                # context-overflow turn that salvaged the agent's real progress
                                # (driver builds banner + bounded work into result.output). Prefer
                                # that so the session preview shows the work, not just a terse
                                # reason. Mirrors the same precedence in _mesh_complete_task.
                                salvaged = (getattr(result, "output", "") or "").strip()
                                full_out = salvaged or (self._short_failure_reason(result) or "(failed)")
                            else:
                                full_out = self._session_reply_text(result).strip()
                            # last_result_summary is a short preview used by Telegram
                            # and the session list — keep it brief (last 400 chars).
                            session.last_result_summary = full_out[-400:] if len(full_out) > 400 else full_out
                            session.last_summary = session.last_result_summary
                            session.last_files_modified = result.files_modified or []
                            artifact_path = str(Path(config.system.results_dir) / f"{task.id}.json")
                            session.last_artifact_path = artifact_path
                            session.task_history.append({
                                "task_id": task.id,
                                "timestamp": result.timestamp,
                                "success": result.success,
                                "execution_time": round(result.execution_time or 0.0, 2),
                                "user_message": session.last_user_message,
                                "result_summary": full_out,
                                "files_modified": session.last_files_modified[:20],
                            })
                            session.task_history = session.task_history[-20:]
                            session.status = _session_status_after_result(result)
                            self.session_store.save(session)

                            # Compact summary  state/summaries/<session_id>.md
                            self._write_session_summary(session, result)

                            # Per-session event log  logs/session_events/<session_id>.log
                            self._append_session_event(session_id, task.id, result)
                except Exception as e:
                    logger.warning(f"session_update_failed task_id={task.id} error={e}")

                # Archive processed task file to avoid reprocessing
                try:
                    source_path_str = None
                    if getattr(task, "metadata", None):
                        source_path_str = task.metadata.get("__file_path")
                    if source_path_str:
                        source_path = Path(source_path_str)
                        processed_dir = Path(config.system.tasks_dir) / "processed"
                        processed_dir.mkdir(parents=True, exist_ok=True)
                        target_name = f"{task.id}.{task.status.value}.task.md"
                        target_path = processed_dir / target_name
                        # Only move if source exists and is not already in processed
                        if source_path.exists() and source_path.parent != processed_dir:
                            source_path.replace(target_path)
                            logger.info(f"event=task_archived task_id={task.id} to={target_path}")
                            self._emit_event("task_archived", task, {"to": str(target_path)})
                except Exception as e:
                    logger.warning(f"event=task_archive_failed task_id={task.id} error={e}")
                    self._emit_event("task_archive_failed", task, {"error": str(e)})
                finally:
                    # Release in-flight lock now that processing is complete
                    try:
                        if getattr(task, "metadata", None):
                            self._inflight_paths.discard(task.metadata.get("__file_path", ""))
                            # Clear pending and persist
                            self._pending_files.discard(task.metadata.get("__file_path", ""))
                            self._save_state()
                    except Exception:
                        pass
                
                # Cleanup cancellation and running maps
                try:
                    self._task_cancel_events.pop(task.id, None)
                    self._running_exec_tasks.pop(task.id, None)
                    self._shutdown_interrupted_tasks.discard(task.id)
                    self.active_tasks.pop(task.id, None)
                except Exception:
                    pass
                # Mark task as done in queue
                self.task_queue.task_done()
                
            except asyncio.TimeoutError:
                # No tasks available, continue
                continue
            except asyncio.CancelledError:
                logger.info(f"Worker {worker_name} cancelled")
                break
            except Exception as e:
                logger.error(f"Worker {worker_name} error: {e}")
                # Continue processing other tasks
                continue
        
        logger.info(f"Task worker {worker_name} stopped")
    
    # ===========================================================================
    # TASK EXECUTION — LOCAL PATH                                ★ ENTRY POINT
    # process_task() is THE execution entry point.  It decides local vs remote:
    #   Local  → _dispatch_or_run_local() → _run_backend_local()
    #   Remote → _process_task_remote() (MESH_ENABLED + session.machine_id set)
    # After execution: _write_artifacts(), _emit_event(), session state update.
    # ===========================================================================

    # ★ EXECUTION ENTRY POINT — called by _task_worker() and directly by tests
    async def process_task(self, task: Task) -> TaskResult:
        """Process a single task through the complete pipeline.

        Steps:
        1) Execute the task via backend-native session resume or stateless Claude bridge
        2) Summarize results with LLAMA (or fallback) for `summaries/*.txt`
        3) Run validation engine and attach metadata
        4) Persist `results/*.json` and emit events
        """
        start_time = time.time()
        
        try:
            task.status = TaskStatus.PROCESSING

            # Opt-in continuation: if this task declares `continues: <prior_task_id>`
            # (via .task.md frontmatter or submit_instruction extra_metadata), prepend
            # bounded, fenced prior context to the prompt exactly once, before the
            # retry loop and the remote/local branch so every execution path carries
            # it. Tasks without `continues:` are byte-identical to before.
            await self._maybe_inject_compact_context(task)

            # Keep the user's prompt intact. Native Claude/Codex runtime should decide
            # how to approach the task rather than our local prompt-rewrite layer.
            logger.debug(f"Executing task {task.id}")
            max_retries = getattr(config.validation, "max_retries", 2)
            retry_delay = 1.0
            backoff_mult = max(1, getattr(config.validation, "backoff_multiplier", 2))
            attempt = 0
            last_result: Optional[TaskResult] = None
            session_recreated = False
            next_spawn_reason = "initial"
            # Per-task timeout override via frontmatter metadata `timeout_sec`, else system default
            try:
                timeout_s = int(task.metadata.get("timeout_sec", config.system.task_timeout)) if getattr(task, "metadata", None) else config.system.task_timeout
            except Exception:
                timeout_s = config.system.task_timeout
            cancel_ev = self._task_cancel_events.get(task.id)

            # Resolve session up front so we can decide whether this task is
            # pinned to a remote mesh node before entering the local retry loop.
            session_id = (task.metadata or {}).get("session_id", "").strip()
            session = self.session_store.get(session_id) if session_id else None

            # Mesh routing: only sessions explicitly pinned to a remote node
            # (`session.machine_id` set) with MESH_ENABLED=true take this path.
            # Everything else falls through to the untouched local retry loop
            # below — zero behavior change for ordinary local sessions.
            _host = socket.gethostname()
            _pinned_elsewhere = bool(
                session and session.machine_id and session.machine_id != _host
            )
            route_remote = bool(config.mesh.enabled and _pinned_elsewhere)

            # Affinity guard (A11): a session pinned to a *different* node must NOT
            # execute in this host's local worker pool. Before A11, if `route_remote`
            # came out False for any reason (mesh flag not seen at this call site,
            # etc.) while the session named another node, the task silently ran
            # locally on the wrong machine — corrupting backend_session_id continuity
            # and producing a null/duplicate gateway_node_id (the #9 smoke failure).
            # Make that case loud instead of silent: log the exact sub-conditions and
            # refuse local execution.
            if _pinned_elsewhere and not route_remote:
                logger.error(
                    "event=affinity_unrouted task_id=%s session_id=%s machine_id=%s host=%s "
                    "mesh_enabled=%s — refusing local execution of a remote-pinned session",
                    task.id, getattr(session, "session_id", None),
                    getattr(session, "machine_id", None), _host, config.mesh.enabled,
                )
                self._emit_event(
                    "affinity_unrouted",
                    task,
                    {
                        "session_id": getattr(session, "session_id", None),
                        "machine_id": getattr(session, "machine_id", None),
                        "host": _host,
                        "mesh_enabled": bool(config.mesh.enabled),
                    },
                )
                if config.mesh.enabled:
                    # Mesh is on and the node is named — honor the pin via the remote
                    # path (which fails loudly if the node is offline; no local fallback).
                    route_remote = True
                else:
                    # Mesh disabled but the session is pinned elsewhere: we cannot
                    # honor affinity and must not run on the wrong host. Fail honestly.
                    last_result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output="",
                        errors=[
                            f"Session pinned to node {session.machine_id!r} but mesh is "
                            f"disabled on {_host!r}; cannot execute without violating "
                            f"session affinity."
                        ],
                        files_modified=[],
                        execution_time=time.time() - start_time,
                        timestamp=now_iso(),
                    )
                    setattr(last_result, "backend_name", getattr(session, "backend", None))
                    last_result.error_class = self._classify_error(last_result)
                    last_result.retries = 0
                    route_remote = True  # skip the local loop below

            if route_remote and last_result is None:
                last_result = await self._process_task_remote(task, session, start_time, timeout_s)

            # Defense-in-depth (A11/A18 invariant): the local worker loop must
            # never execute a turn for a session pinned to a *different* host —
            # that would fork backend_session_id continuity onto the wrong box.
            # The affinity guard above already forces route_remote=True for such
            # sessions (so this loop is unreachable for them); assert it rather
            # than trust the guard alone. The mesh claim filter (db.py) is the
            # other, independent line of defense at claim time.
            if not route_remote:
                assert not _pinned_elsewhere, (
                    f"affinity invariant violated: session {getattr(session, 'session_id', None)!r} "
                    f"pinned to {getattr(session, 'machine_id', None)!r} reached the local worker "
                    f"loop on host {_host!r}"
                )

            while not route_remote:
                attempt += 1
                from src.core.telemetry import TelemetryContext
                from config.models import resolve_model
                local_action = (
                    "resume_session"
                    if session and session.backend_session_id
                    else "create_session"
                    if session
                    else "run_oneoff"
                )
                telemetry_context = TelemetryContext.create(
                    turn_id=task.id,
                    node_id=socket.gethostname(),
                    session_id=session_id or None,
                    backend=session.backend if session else self._resolve_task_backend(task),
                    # Record the RESOLVED model actually launched (default → catalog),
                    # not the raw stored NULL — otherwise the turn ledger reports no
                    # model for every default session. Mirrors the driver's resolve_model.
                    model=resolve_model(session) if session else None,
                    source="gateway",
                    attempt=attempt,
                    spawn_reason=next_spawn_reason,
                    retry_of_invocation_id=(
                        getattr(last_result, "telemetry_invocation_id", None)
                        if last_result is not None
                        else None
                    ),
                )
                next_spawn_reason = "retry"
                self._emit_turn_telemetry(
                    "invocation.created",
                    task,
                    {
                        "attempt": attempt,
                        "spawn_reason": telemetry_context.spawn_reason,
                        "action": local_action,
                        "retry_of_invocation_id": telemetry_context.retry_of_invocation_id,
                    },
                    invocation_id=telemetry_context.invocation_id,
                    backend=telemetry_context.backend,
                    model=telemetry_context.model,
                )
                self._emit_turn_telemetry(
                    "invocation.started",
                    task,
                    {"action": local_action},
                    invocation_id=telemetry_context.invocation_id,
                    backend=telemetry_context.backend,
                    model=telemetry_context.model,
                )
                # Run execution as a task to allow timeout/cancel
                # Use session backend (with native resume) when task belongs to a session.
                # For non-session tasks, use the native backend directly instead of
                # the legacy Claude bridge/task-file execution path.
                if session:
                    session.status = SessionStatus.BUSY
                    self.session_store.save(session)
                    backend_name = session.backend
                    backend = self._backends.get(backend_name, self._backends["claude"])
                    session.last_user_message = task.prompt
                    if session.backend_session_id:
                        from src.core.backend_call import call_backend
                        exec_task = asyncio.create_task(
                            asyncio.to_thread(
                                call_backend,
                                backend.resume_session,
                                session,
                                task.prompt,
                                telemetry_context=telemetry_context,
                                telemetry_sink=self._telemetry_sink,
                            )
                        )
                    else:
                        from src.core.backend_call import call_backend
                        exec_task = asyncio.create_task(
                            asyncio.to_thread(
                                call_backend,
                                backend.create_session,
                                session,
                                telemetry_context=telemetry_context,
                                telemetry_sink=self._telemetry_sink,
                            )
                        )
                else:
                    backend_name = str((task.metadata or {}).get("backend") or "claude").strip().lower()
                    backend = self._backends.get(backend_name, self._backends["claude"])
                    cwd_override = str((task.metadata or {}).get("cwd") or "").strip()
                    if not cwd_override:
                        cwd_override = str(getattr(config.claude, "base_cwd", "") or "").strip()
                    from src.core.backend_call import call_backend
                    exec_task = asyncio.create_task(
                        asyncio.to_thread(
                            call_backend,
                            backend.run_oneoff,
                            cwd_override,
                            task.prompt,
                            telemetry_context=telemetry_context,
                            telemetry_sink=self._telemetry_sink,
                        )
                    )
                self._running_exec_tasks[task.id] = exec_task
                # Wait for whichever happens first
                wait_set = {exec_task}
                cancel_waiter: Optional[asyncio.Task] = None
                timeout_waiter: Optional[asyncio.Task] = None
                heartbeat_task: Optional[asyncio.Task] = None
                try:
                    if cancel_ev is not None:
                        cancel_waiter = asyncio.create_task(cancel_ev.wait())
                        wait_set.add(cancel_waiter)
                    if timeout_s and timeout_s > 0:
                        timeout_waiter = asyncio.create_task(asyncio.sleep(timeout_s))
                        wait_set.add(timeout_waiter)
                    heartbeat_interval = getattr(config.system, "task_heartbeat_interval_sec", 300)
                    if self.telegram_interface and heartbeat_interval > 0:
                        heartbeat_task = asyncio.create_task(
                            self._send_task_heartbeats(task, session, start_time, heartbeat_interval, timeout_s)
                        )
                    done, pending = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
                    if exec_task in done:
                        raw = exec_task.result()
                        # Normalize ExecutionResult (from backends) to TaskResult
                        from src.core.interfaces import ExecutionResult as _ER
                        if isinstance(raw, _ER):
                            # Persist backend session ID back onto the session record.
                            # Save on both success and failure — if the backend created/
                            # recreated a session (e.g. after server restart) but the
                            # first message failed, we still want the new ID persisted so
                            # the next turn can resume rather than starting fresh again.
                            if session and (
                                raw.backend_session_id
                                or session.cache_health != "unknown"
                                or session.driver_status
                            ):
                                # _observe_cache_health mutates cache_health /
                                # cache_unhealthy_count in place on this session
                                # object during the backend call; persist them
                                # (alongside any new backend_session_id) so the
                                # cache_unhealthy_count>=2 guard survives across
                                # turns rather than resetting each time.
                                if raw.backend_session_id:
                                    session.backend_session_id = raw.backend_session_id
                                self.session_store.save(session)
                            result = TaskResult(
                                task_id=task.id,
                                success=raw.success,
                                output=raw.output,
                                errors=raw.errors,
                                files_modified=raw.files_modified,
                                execution_time=raw.execution_time,
                                timestamp=now_iso(),
                                file_changes=getattr(raw, "file_changes", []),
                                raw_stdout=getattr(raw, "raw_stdout", ""),
                                raw_stderr=getattr(raw, "raw_stderr", ""),
                                parsed_output=getattr(raw, "parsed_output", None),
                                return_code=getattr(raw, "return_code", 0),
                            )
                            setattr(result, "backend_name", backend_name)
                            if raw.telemetry is not None:
                                setattr(
                                    result,
                                    "telemetry_invocation_id",
                                    raw.telemetry.invocation_id,
                                )
                        else:
                            result = raw
                    elif cancel_waiter and cancel_waiter in done:
                        # Cooperative cancellation
                        self._emit_turn_telemetry(
                            "turn.cancel_requested",
                            task,
                            {
                                "reason_code": (
                                    "gateway_shutdown"
                                    if task.id in self._shutdown_interrupted_tasks
                                    else "user_cancel"
                                )
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        self._emit_turn_telemetry(
                            "process.termination_requested",
                            task,
                            {"reason_code": "gateway_cancel"},
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        if session:
                            with contextlib.suppress(Exception):
                                backend.cancel(session)
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("cancelled", task, {"when": "during_execution"})
                        interrupted = task.id in self._shutdown_interrupted_tasks
                        if session:
                            session.status = SessionStatus.ERROR if interrupted else SessionStatus.CANCELLED
                            self.session_store.save(session)
                        result = TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=["interrupted by gateway restart" if interrupted else "cancelled"],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=now_iso(),
                        )
                        setattr(result, "backend_name", backend_name)
                        setattr(result, "telemetry_invocation_id", telemetry_context.invocation_id)
                        self._emit_turn_telemetry(
                            "invocation.completed",
                            task,
                            {
                                "status": "failed",
                                "duration_ms": round(execution_time * 1000),
                                "error_code": "cancelled",
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        return result
                    else:
                        # Timeout
                        self._emit_turn_telemetry(
                            "process.termination_requested",
                            task,
                            {"reason_code": "gateway_timeout"},
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        if session:
                            with contextlib.suppress(Exception):
                                backend.cancel(session)
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("timeout", task, {"timeout_s": timeout_s})
                        self._emit_turn_telemetry(
                            "turn.timeout_requested",
                            task,
                            {
                                "timeout_kind": "gateway_timeout",
                                "timeout_ms": timeout_s * 1000,
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        if session:
                            session.status = SessionStatus.ERROR
                            self.session_store.save(session)
                        elapsed_min = int(execution_time // 60)
                        timeout_min = int(timeout_s // 60)
                        timeout_error = (
                            f"Task timed out after {elapsed_min}m (limit: {timeout_min}m). "
                            f"Claude was still running when the gateway cut it off. "
                            f"To allow more time set GATEWAY_TASK_TIMEOUT_SEC (currently {timeout_s}). "
                            f"You can retry with a larger scope split or use /session_cancel then resubmit."
                        )
                        result = TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=[timeout_error],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=now_iso(),
                        )
                        setattr(result, "backend_name", backend_name)
                        setattr(result, "telemetry_invocation_id", telemetry_context.invocation_id)
                        self._emit_turn_telemetry(
                            "invocation.completed",
                            task,
                            {
                                "status": "failed",
                                "duration_ms": round(execution_time * 1000),
                                "error_code": "timeout",
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        return result
                finally:
                    # Cancel any pending helper waiters
                    for w in (cancel_waiter, timeout_waiter, heartbeat_task):
                        if w and not w.done():
                            w.cancel()
                error_class = self._classify_error(result)
                result.error_class = error_class
                result.retries = attempt - 1
                setattr(result, "telemetry_invocation_id", telemetry_context.invocation_id)
                self._emit_turn_telemetry(
                    "invocation.completed",
                    task,
                    {
                        "status": "success" if result.success else "failed",
                        "duration_ms": round((result.execution_time or 0.0) * 1000),
                        "exit_code": getattr(result, "return_code", None),
                        "error_code": error_class or None,
                    },
                    invocation_id=telemetry_context.invocation_id,
                    backend=backend_name,
                    model=telemetry_context.model,
                )

                if session and not result.success and not session_recreated and self._is_missing_backend_conversation(result):
                    stale_id = session.backend_session_id
                    session.backend_session_id = ""
                    session.status = SessionStatus.BUSY
                    self.session_store.save(session)
                    session_recreated = True
                    logger.warning(
                        "event=session_recreated task_id=%s stale_backend_session_id=%s reason=missing_conversation",
                        task.id,
                        stale_id,
                    )
                    self._emit_event(
                        "session_recreated",
                        task,
                        {"stale_backend_session_id": stale_id, "reason": "missing_conversation"},
                    )
                    last_result = result
                    next_spawn_reason = "session_recreate"
                    continue

                # Determine retry strategy per error class
                strategy = self._get_retry_strategy(error_class)
                max_retries = strategy.get("max_retries", max_retries)
                if attempt == 1:
                    retry_delay = strategy.get("initial_delay", retry_delay)
                    backoff_mult = strategy.get("backoff_multiplier", backoff_mult)
                if (not result.success) and attempt <= max_retries:
                    jitter = random.uniform(0.85, 1.35)
                    delay = max(0.0, retry_delay * jitter)
                    logger.warning(f"event=retry task_id={task.id} attempt={attempt} class={error_class} delay_s={delay:.2f}")
                    self._emit_event("retry", task, {"attempt": attempt, "class": error_class, "delay_s": delay})
                    self._emit_turn_telemetry(
                        "invocation.retry_scheduled",
                        task,
                        {
                            "retry_reason": error_class,
                            "delay_ms": round(delay * 1000),
                            "next_attempt": attempt + 1,
                            "retry_of_invocation_id": telemetry_context.invocation_id,
                        },
                        invocation_id=telemetry_context.invocation_id,
                        backend=backend_name,
                    )
                    await asyncio.sleep(delay)
                    retry_delay = retry_delay * backoff_mult if retry_delay > 0 else strategy.get("initial_delay", 1.0) * backoff_mult
                    last_result = result
                    next_spawn_reason = "retry"
                    continue
                last_result = result
                break
            # Retries (if any) for this error class are exhausted at this point —
            # only now, on the final result, correct a salvaged terminal error to
            # success (must run after the retry decision above, or a legitimately
            # retry-eligible usage_limit/rate_limit turn would never get retried).
            _reclassify_salvaged_turn_success(last_result)

            # Step 4: Summarize results with LLAMA — skip for session tasks so
            # Claude's actual response is preserved unmodified in output.
            if not session_id:
                logger.debug(f"Step 4: Summarizing results for task {task.id}")
                summary = self.llama_mediator.summarize_result(last_result, task)
                last_result.output = summary + "\n\n" + last_result.output
                logger.info(f"event=summarized task_id={task.id}")
                self._emit_event("summarized", task)
            else:
                logger.debug(f"Step 4: Skipping LLAMA summarization for session task {task.id}")
            
            # Step 5: Validation pass (MVP) — skip sentence-transformer similarity
            # for session tasks; the llama check is meaningless there and triggers
            # the expensive SentenceTransformer encode on every turn.
            try:
                if session_id:
                    llama_validation = self.validation_engine.validate_task_result(
                        result=last_result,
                        expected_files=task.target_files or [],
                        task_type=task.type,
                    ).__dict__
                    validation_summary = {"llama": {"valid": True, "skipped": True}, "result": llama_validation}
                else:
                    validation_summary = {
                        "llama": self.validation_engine.validate_llama_output(
                            input_text=task.prompt or "",
                            output=last_result.output or "",
                            task_type=task.type,
                        ).__dict__,
                        "result": self.validation_engine.validate_task_result(
                            result=last_result,
                            expected_files=task.target_files or [],
                            task_type=task.type,
                        ).__dict__,
                    }
                # Attach lightweight validation data into parsed_output for artifacts
                if isinstance(last_result.parsed_output, dict):
                    last_result.parsed_output.setdefault("validation", validation_summary)
                else:
                    last_result.parsed_output = {"content": last_result.output, "validation": validation_summary}
                # Also surface at top level for artifact visibility
                setattr(last_result, "validation", validation_summary)
                logger.info(
                    f"event=validated task_id={task.id} "
                    f"valid_llama={validation_summary['llama']['valid']} "
                    f"valid_result={validation_summary['result']['valid']}"
                )
                self._emit_event("validated", task, {
                    "valid_llama": validation_summary["llama"]["valid"],
                    "valid_result": validation_summary["result"]["valid"],
                })
            except Exception as _:
                # Non-fatal; continue
                pass

            return last_result
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Task processing failed for {task.id}: {e}")
            return TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[str(e)],
                files_modified=[],
                execution_time=execution_time,
                timestamp=now_iso()
            )

    # ===========================================================================
    # TASK EXECUTION — REMOTE PATH  (MESH_ENABLED + session.machine_id set)
    # _process_task_remote() mirrors process_task()'s bookkeeping but dispatches
    # to the remote node via _dispatch_to_node() and polls for completion.
    # Session-affinity is the hard correctness invariant here: this method is
    # only reached when session.machine_id names a node that is NOT this host.
    # ===========================================================================

    async def _process_task_remote(
        self,
        task: "Task",
        session: Any,
        start_time: float,
        timeout_s: int,
    ) -> "TaskResult":
        """Execute a mesh-pinned session's task on its assigned remote node.

        Only reachable when `MESH_ENABLED=true` AND `session.machine_id` is
        set — see the routing check in `process_task`. Mirrors the bookkeeping
        `process_task`'s local retry loop performs (BUSY status, heartbeats,
        cancellation, error classification, session error state) around a
        single call to `_dispatch_to_node`, which enqueues the task, waits for
        the pinned worker to claim and post a result, and returns a terminal
        `TaskResult`.

        Session affinity is a hard requirement: if the pinned node is not
        registered or not online, this fails loudly with no local fallback —
        silently running on this machine would corrupt `backend_session_id`
        continuity, since backend sessions are machine-local.
        """
        from src.control.node_registry import get_registry
        from src.core.observability import set_log_context

        # Correlate every log line + event emitted during this remote dispatch
        # with the task and session, across this gateway's logs and (by the same
        # task_id) the worker's logs on the remote machine.
        set_log_context(task_id=task.id, session_id=session.session_id)

        backend_name = session.backend
        session.status = SessionStatus.BUSY
        session.last_user_message = task.prompt
        self.session_store.save(session)

        def _routing_failure(msg: str) -> "TaskResult":
            logger.error("event=mesh_routing_failed task_id=%s session_id=%s machine_id=%s reason=%s",
                         task.id, session.session_id, session.machine_id, msg)
            self._emit_event("mesh_routing_failed", task, {"machine_id": session.machine_id, "reason": msg})
            result = TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[msg],
                files_modified=[],
                execution_time=time.time() - start_time,
                timestamp=now_iso(),
            )
            setattr(result, "backend_name", backend_name)
            return result

        registry = get_registry()

        def _check_pinned_liveness() -> Tuple[Any, bool]:
            """Return ``(node_handle_or_None, is_online)`` for the pinned node.

            Checks the in-memory registry first, then falls back to the shared
            DB. The in-memory registry is only populated when this process also
            runs the task server (co-located deployment); in a split setup, or
            after a gateway restart that wiped the in-memory registry, the node
            may only exist in the DB — without the fallback a live node reads as
            offline and needlessly kills the session. Used both for the initial
            check and for each poll during the A18 offline grace hold."""
            _node = registry.get(session.machine_id)
            if _node is not None and _node.status == "online":
                return _node, True
            try:
                from src.control.db import get_db as _get_db
                _db = _get_db()
                if _db is not None:
                    _row = _db.get_node(session.machine_id)
                    if _row and _row.get("status") == "online":
                        return _node, True
            except Exception:
                logger.warning(
                    "event=affinity_liveness_db_check_failed task_id=%s machine_id=%s",
                    task.id, session.machine_id, exc_info=True,
                )
            return _node, False

        node, node_online = _check_pinned_liveness()

        if not node_online:
            # A18 — pinned-worker offline fallback. `grace=0` ⇒ disabled: reproduce
            # the pre-A18 (A11) behavior byte-for-byte (immediate honest fail,
            # terminal ERROR, no hold). `grace>0` ⇒ bounded hold-and-requeue: a
            # transient worker blip no longer permanently kills a healthy session.
            grace_sec = max(0, int(getattr(config.mesh, "affinity_offline_grace_sec", 0) or 0))
            if grace_sec <= 0:
                result = _routing_failure(f"Node {session.machine_id!r} is offline; cannot continue session (no local fallback — affinity is required)")
                session.status = SessionStatus.ERROR
                self.session_store.save(session)
                result.error_class = self._classify_error(result)
                result.retries = 0
                return result

            # Option A — bounded hold-and-requeue. The pinned node is offline right
            # now, but the outage may clear within the grace window. Hold the
            # session in a distinct, honest PAUSED state and poll liveness. The
            # turn is NEVER relocated: it is only dispatched (below) once the node
            # is confirmed online again, and the mesh claim filter (db.py) still
            # guarantees only the pinned node can ever claim it. A11 invariant is
            # preserved exactly — no off-host execution, ever.
            poll_interval = max(0.5, float(getattr(config.mesh, "affinity_offline_poll_interval_sec", 5.0) or 5.0))
            poll_interval = min(poll_interval, float(grace_sec))
            deadline = time.time() + grace_sec

            logger.warning(
                "event=affinity_hold_started task_id=%s session_id=%s machine_id=%s grace_sec=%s poll_sec=%.1f",
                task.id, session.session_id, session.machine_id, grace_sec, poll_interval,
            )
            self._emit_event("affinity_hold_started", task, {
                "session_id": session.session_id,
                "machine_id": session.machine_id,
                "grace_sec": grace_sec,
            })
            session.status = SessionStatus.PAUSED_PINNED_NODE_OFFLINE
            self.session_store.save(session)

            hold_cancel_ev = self._task_cancel_events.get(task.id)
            polls = 0
            while time.time() < deadline:
                # Honor an operator cancel during the hold rather than pinning the
                # session to a node that may never return.
                if hold_cancel_ev is not None and hold_cancel_ev.is_set():
                    break
                await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.time())))
                polls += 1
                node, node_online = _check_pinned_liveness()
                if node_online:
                    break

            if not node_online:
                # Grace expired (or cancelled) with the node still down. Honest,
                # resumable terminal state + a distinct event — the operator can
                # retry once the node returns, or re-pin the session to another
                # node. Not a bare ERROR; not an off-host fallback.
                cancelled = hold_cancel_ev is not None and hold_cancel_ev.is_set()
                reason = (
                    f"Node {session.machine_id!r} still offline after {grace_sec}s affinity "
                    f"grace window; session paused (retry when the node returns, or re-pin to "
                    f"another node). No off-host fallback — affinity is required."
                )
                logger.error(
                    "event=affinity_offline_timeout task_id=%s session_id=%s machine_id=%s "
                    "grace_sec=%s polls=%s cancelled=%s",
                    task.id, session.session_id, session.machine_id, grace_sec, polls, cancelled,
                )
                self._emit_event("affinity_offline_timeout", task, {
                    "session_id": session.session_id,
                    "machine_id": session.machine_id,
                    "grace_sec": grace_sec,
                    "polls": polls,
                    "cancelled": cancelled,
                })
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=[reason],
                    files_modified=[],
                    execution_time=time.time() - start_time,
                    timestamp=now_iso(),
                )
                setattr(result, "backend_name", backend_name)
                session.status = (
                    SessionStatus.CANCELLED if cancelled else SessionStatus.PINNED_NODE_OFFLINE
                )
                self.session_store.save(session)
                result.error_class = self._classify_error(result)
                result.retries = polls
                return result

            # Node re-registered within the grace window — the blip was invisible
            # to the operator. Resume normal dispatch below (emits mesh_dispatch).
            logger.info(
                "event=affinity_hold_resolved task_id=%s session_id=%s machine_id=%s polls=%s",
                task.id, session.session_id, session.machine_id, polls,
            )
            self._emit_event("affinity_hold_resolved", task, {
                "session_id": session.session_id,
                "machine_id": session.machine_id,
                "polls": polls,
            })
            session.status = SessionStatus.BUSY
            self.session_store.save(session)

        cancel_ev = self._task_cancel_events.get(task.id)
        heartbeat_task: Optional[asyncio.Task] = None
        heartbeat_interval = getattr(config.system, "task_heartbeat_interval_sec", 300)
        try:
            try:
                if self.telegram_interface and heartbeat_interval > 0:
                    heartbeat_task = asyncio.create_task(
                        self._send_task_heartbeats(task, session, start_time, heartbeat_interval, timeout_s)
                    )
                # node may be None here when liveness was confirmed via the DB
                # fallback (the in-memory registry didn't have it). machine_id is
                # the reliable identifier in every case.
                target_node = node.node_id if node is not None else session.machine_id
                # A18/A11 defense-in-depth: a pinned turn dispatches to its own
                # node or not at all. The mesh claim filter (db.py) already keeps
                # the local worker pool from ever claiming a pinned task; enforce
                # the invariant here too so a routing regression fails CLOSED
                # rather than silently forking the conversation on a substitute
                # host. Deliberately NOT an `assert` — this is a hard correctness
                # invariant that must survive `python -O` / PYTHONOPTIMIZE, which
                # strips asserts. Never reached for unpinned/mesh-disabled work.
                if target_node != session.machine_id:
                    result = _routing_failure(
                        f"affinity violation: session pinned to {session.machine_id!r} "
                        f"but dispatch target resolved to {target_node!r}; refusing off-host dispatch"
                    )
                    session.status = SessionStatus.ERROR
                    self.session_store.save(session)
                    result.error_class = self._classify_error(result)
                    result.retries = 0
                    return result
                logger.info("mesh_dispatch backend=%s -> %s", backend_name, target_node)
                self._emit_event("mesh_dispatch", task, {
                    "backend": backend_name,
                    "target_node": target_node,
                })
                # A11 follow-up: attribute this turn's execution_node_id to the
                # remote node instead of leaving it null. The worker's own rich
                # per-invocation telemetry (tool calls, model usage) ships
                # separately via the worker's own sink and may or may not arrive
                # depending on its connectivity to this gateway (see the A10 §T1
                # revalidation: it didn't, for the mesh path exercised there).
                # This pair of events is the one fact the gateway can assert
                # unconditionally around the dispatch call — that execution
                # happened on `target_node`, not here. Without it,
                # telemetry_projection.project_turn() has no event carrying
                # event_name in ("invocation.started", "process.spawned") for
                # mesh-dispatched turns, so execution_node_id (and
                # llm_invocations.node_id, derived from it) silently default to
                # null. If the worker's own telemetry later starts arriving too,
                # this will show as a second invocation row for the same
                # turn_id — acceptable overlap, not a correctness bug, since
                # each invocation_id is independently minted.
                _mesh_invocation_id = None
                try:
                    from src.core.telemetry import (
                        EMITTER_PROCESS_INSTANCE_ID,
                        build_event,
                        new_telemetry_id,
                    )
                    from config.models import resolve_model
                    # Resolved (gateway-side), not the raw session.model — mirrors
                    # _session_dispatch_payload so the gateway's own mesh_dispatch
                    # telemetry never reports a blank/wrong model for default-model turns.
                    _mesh_model = resolve_model(session)
                    _mesh_invocation_id = new_telemetry_id("inv")
                    self._telemetry_sink.emit(
                        build_event(
                            "invocation.started",
                            turn_id=task.id,
                            session_id=session.session_id,
                            node_id=target_node,
                            emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                            source="worker",
                            invocation_id=_mesh_invocation_id,
                            backend=backend_name,
                            model=_mesh_model,
                            attributes={"action": "mesh_dispatch"},
                        )
                    )
                except Exception:
                    logger.warning("event=mesh_dispatch_telemetry_emit_failed task_id=%s", task.id, exc_info=True)
                result = await self._dispatch_to_node(task, session, node)
                if _mesh_invocation_id is not None:
                    try:
                        self._telemetry_sink.emit(
                            build_event(
                                "invocation.completed",
                                turn_id=task.id,
                                session_id=session.session_id,
                                node_id=target_node,
                                emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                                source="worker",
                                invocation_id=_mesh_invocation_id,
                                backend=backend_name,
                                model=_mesh_model,
                                attributes={
                                    "status": "success" if getattr(result, "success", False) else "failed",
                                    "duration_ms": int(getattr(result, "execution_time", 0.0) * 1000),
                                    "exit_code": getattr(result, "return_code", None),
                                },
                            )
                        )
                    except Exception:
                        logger.warning("event=mesh_dispatch_telemetry_emit_failed task_id=%s", task.id, exc_info=True)
            finally:
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
        except Exception as exc:
            # Unexpected dispatch failure — return a clean error and unblock
            # the session rather than leaving it stuck as BUSY.
            result = _routing_failure(f"Unexpected dispatch error: {exc}")

        result.error_class = self._classify_error(result)
        result.retries = 0
        _reclassify_salvaged_turn_success(result)

        # Detached = gateway is shutting down while the remote worker keeps
        # running. Leave the session BUSY (do not touch its status) so startup
        # recovery reattaches and reports the worker's real result. Marking it
        # CANCELLED/ERROR here would be the fabricated state we're fixing.
        if getattr(result, "detached", False):
            logger.info(
                "event=mesh_dispatch_detached task_id=%s session_id=%s reason=gateway_shutdown",
                task.id, session.session_id,
            )
            return result

        session.status = _session_status_after_result(
            result,
            cancel_requested=cancel_ev is not None and cancel_ev.is_set(),
        )
        self.session_store.save(session)

        # Annotate failure errors with the node name so users see *which* machine failed.
        node_label = node.node_id if node is not None else session.machine_id
        if not result.success and node_label:
            if not result.errors:
                result.errors = [f"[{node_label}] Task failed with no error details"]
            else:
                result.errors = [
                    (f"[{node_label}] {e}" if e.strip() else f"[{node_label}] Task failed (no error message)")
                    if not str(e).startswith(f"[{node_label}]") else e
                    for e in result.errors
                ]

        first_error = result.errors[0] if result.errors else ""
        logger.info(
            "mesh_result success=%s elapsed=%.1fs%s",
            result.success, result.execution_time,
            "" if result.success else f" error={first_error}",
        )
        error_detail = getattr(result, "error_detail", "") or getattr(result, "raw_stderr", "") or ""
        if not result.success and error_detail:
            logger.info(
                "mesh_result_detail task_id=%s node=%s detail=%s",
                task.id,
                node_label,
                error_detail[:4000],
            )
        self._emit_event("mesh_result", task, {
            "success": result.success,
            "target_node": node.node_id if node is not None else session.machine_id,
            "duration_s": round(result.execution_time, 3),
            "error_class": result.error_class,
            "error": first_error,
            "error_detail": error_detail[:4000],
        })

        return result

    # ===========================================================================
    # ARTIFACT WRITE & CONTENT RECONSTRUCTION
    # _write_artifacts()          — persist TaskResult to mesh_tasks (canonical)
    #                               and results/*.json (fallback/debug).
    # _reconstruct_task_content() — rebuild the original prompt text when the
    #                               Task object was partially deserialized.
    # ===========================================================================

    def _reconstruct_task_content(self, task: Task) -> str:
        """Reconstruct a `.task.md` representation for LLAMA processing.

        Used to provide consistent context to LLAMA summarization/optimizations.
        """
        content = f"""---
id: {task.id}
type: {task.type.value}
priority: {task.priority.value}
created: {task.created}
---

# {task.title}

**Target Files:**
{chr(10).join('- ' + f for f in task.target_files)}

**Prompt:**
{task.prompt}

**Success Criteria:**
{chr(10).join('- [ ] ' + c for c in task.success_criteria)}

**Context:**
{task.context}
"""
        return content

    def _write_artifacts(self, task_id: str, result: TaskResult, task: Optional[Task] = None):
        """Persist results and summaries to disk"""
        results_dir = Path(config.system.results_dir)
        summaries_dir = Path(config.system.summaries_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        summaries_dir.mkdir(parents=True, exist_ok=True)

        # Write raw JSON artifact with structured fields
        artifact = {
            "schema_version": "1.0",
            "task_id": task_id,
            "success": result.success,
            "return_code": result.return_code,
            "timestamp": result.timestamp,
            "execution_time": result.execution_time,
            "errors": result.errors,
            "files_modified": result.files_modified,
            "file_changes": getattr(result, "file_changes", []),
            # Linkage for multi-turn/threaded contexts (optional)
            "parent_task_id": getattr(result, "parent_task_id", None),
            "turn_of": getattr(result, "turn_of", None),
            # Keep full stdout/stderr for now, but add triage previews
            "raw_stdout": result.raw_stdout,
            "raw_stderr": result.raw_stderr,
            "triage": {
                "stdout_head": (result.raw_stdout or "")[:2048],
                "stdout_tail": (result.raw_stdout or "")[-2048:] if result.raw_stdout else "",
                "stderr_head": (result.raw_stderr or "")[:2048],
                "stderr_tail": (result.raw_stderr or "")[-2048:] if result.raw_stderr else "",
            },
            "parsed_output": result.parsed_output,
            "validation": getattr(result, "validation", None),
            "retry": {
                "retries": getattr(result, "retries", 0),
                "error_class": getattr(result, "error_class", ""),
            },
            "security": {
                "guarded_write": bool(getattr(config.system, "guarded_write", False)),
                "allowlist_root": getattr(config.claude, "allowed_root", None),
                "violations": [],
            },
            "suggested_actions": self._suggest_actions(getattr(result, "error_class", ""), result) if not result.success else [],
            # Minimal status blocks for operability/triage
            "orchestrator": {
                "components": self.component_status,
                "workers": len(self.worker_tasks),
            },
            "runtime": {
                "backend": getattr(result, "backend_name", "claude"),
                "claude_executable": shutil.which("claude") or "claude",
                "codex_executable": shutil.which("codex") or "codex",
                "max_turns": getattr(config.claude, "max_turns", 3),
                "timeout": getattr(config.claude, "timeout", 600),
                "skip_permissions": bool(getattr(config.claude, "skip_permissions", True)),
            },
            "bridge": {
                "available": bool(shutil.which("claude")),
                "claude_executable": shutil.which("claude") or "claude",
                "max_turns": getattr(config.claude, "max_turns", 3),
                "timeout": getattr(config.claude, "timeout", 600),
                "skip_permissions": bool(getattr(config.claude, "skip_permissions", True)),
            },
            "llama": self.llama_mediator.get_status(probe=False),
            "tool_summary": self._extract_tool_summary(result.raw_stdout or ""),
        }
        if task is not None:
            artifact["task"] = {
                "type": getattr(task.type, "value", str(task.type)),
                "priority": getattr(task.priority, "value", str(task.priority)),
                "title": task.title,
                # FULL user instruction — `title` is only a truncated display label
                # (`Task: {description[:50]}...`); persisting `prompt` is what lets
                # the transcript show the complete message instead of a 50-char clip.
                "prompt": task.prompt or "",
                "target_files": list(task.target_files or []),
                "source": str((task.metadata or {}).get("source") or "runtime"),
                "cwd": str((task.metadata or {}).get("cwd") or ""),
            }
            session_id = str((task.metadata or {}).get("session_id") or "").strip()
            if session_id:
                session = self.session_store.get(session_id)
                artifact["session"] = {
                    "session_id": session_id,
                    "backend": session.backend if session else getattr(result, "backend_name", "claude"),
                    "backend_session_id": session.backend_session_id if session else "",
                    "repo_path": session.repo_path if session else str((task.metadata or {}).get("cwd") or ""),
                    "owner_user_id": session.owner_user_id if session else None,
                    "telegram_chat_id": session.telegram_chat_id if session else None,
                }

        import json
        # Allowlist enforcement on files_modified (telemetry + artifact note)
        try:
            allow_root = getattr(config.claude, "allowed_root", None)
            if allow_root and artifact.get("files_modified"):
                from pathlib import Path as _P
                root = _P(allow_root).resolve()
                bad = []
                for f in list(artifact.get("files_modified") or []):
                    try:
                        p = _P(f).resolve()
                        if not (root in p.parents or p == root):
                            bad.append(f)
                    except Exception:
                        bad.append(f)
                if bad:
                    artifact["security"]["violations"] = [
                        {"type": "out_of_root", "path": b} for b in bad
                    ]
                    # Emit security event
                    try:
                        self._emit_event("security_violation", None, {"paths": bad})
                    except Exception:
                        pass
        except Exception:
            pass

        # raw_stdout is 87% of artifact bytes (264 MB across the corpus) and pure
        # debug NDJSON — nothing product-facing reads it back once the reply +
        # usage are extracted into mesh_tasks. When `slim_artifacts` is on, move it
        # to a gzipped sidecar (~10x smaller) and drop it from the JSON, so the DB
        # is the self-sufficient source and the on-disk files shrink to metadata.
        slim = bool(getattr(config.system, "slim_artifacts", False))
        if slim and artifact.get("raw_stdout"):
            try:
                import gzip
                raw_dir = results_dir / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                with gzip.open(raw_dir / f"{task_id}.ndjson.gz", "wt", encoding="utf-8") as gz:
                    gz.write(artifact.get("raw_stdout") or "")
            except Exception as e:
                logger.warning(f"event=raw_archive_failed task_id={task_id} error={e}")
            else:
                artifact["raw_stdout"] = ""
                artifact["raw_stdout_archived"] = f"raw/{task_id}.ndjson.gz"

        flat_artifact_path = results_dir / f"{task_id}.json"
        flat_artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        # Update artifact index (best-effort)
        try:
            self._update_artifact_index(task_id, flat_artifact_path)
        except Exception:
            pass


        # Write human readable summary (extract the LLAMA-generated summary)
        # LLAMA generates a summary and prepends it to result.output
        if result.output:
            # The LLAMA summary is prepended to the output, separated by double newlines
            # So we take everything before the first double newline as the summary
            summary_text = result.output.split("\n\n", 1)[0]
            
            # If the summary is too short (just a title), try to get more content
            if len(summary_text.strip()) < 50:
                # Look for the actual summary content after the title
                paragraphs = result.output.split("\n\n")
                if len(paragraphs) > 1:
                    # Take first 2-3 paragraphs that look like actual content
                    meaningful_paras = []
                    for para in paragraphs[1:4]:  # Skip first (title), take next 3
                        para = para.strip()
                        if para and len(para) > 30 and not para.startswith("#"):
                            meaningful_paras.append(para)
                    if meaningful_paras:
                        summary_text = "\n\n".join(meaningful_paras)
        else:
            summary_text = ""
            
        (summaries_dir / f"{task_id}_summary.txt").write_text(
            summary_text,
            encoding="utf-8"
        )

    # ===========================================================================
    # ERROR CLASSIFICATION & RETRY
    # _classify_error()       — map a TaskResult to an error_class string:
    #                           none | rate_limit | usage_limit | timeout |
    #                           context_overflow | auth | fatal | interactive
    # _get_retry_strategy()   — return max_retries / delay / backoff per class
    # _suggest_actions()      — human-readable next-step hints for the operator
    # cancel_task()           — hard-cancel a queued or running task
    # ===========================================================================

    def _get_retry_strategy(self, error_class: str) -> Dict[str, Any]:
        """Return retry strategy for an error class.

        Fields: max_retries, initial_delay, backoff_multiplier
        """
        default_max = max(0, getattr(config.validation, "max_retries", 2))
        default_mult = max(1, getattr(config.validation, "backoff_multiplier", 2))
        if error_class in ("none", "interactive", "auth", "fatal", "context_overflow", "max_turns", "sdk_stream_closed"):
            # max_turns: resubmitting the identical prompt would just hit the
            # same turn budget again — not retry-eligible, needs an operator
            # decision (raise CLAUDE_SDK_MAX_TURNS / split the task).
            # sdk_stream_closed: the SDK/CLI stream ended without a terminal
            # ResultMessage and may already have applied file edits, so a blind
            # duplicate retry is not safe.
            return {"max_retries": 0, "initial_delay": 0.0, "backoff_multiplier": 1}
        if error_class == "timeout":
            return {"max_retries": min(1, default_max), "initial_delay": 1.0, "backoff_multiplier": 1}
        if error_class in ("network", "upstream_error"):
            return {"max_retries": max(1, default_max), "initial_delay": 1.5, "backoff_multiplier": default_mult}
        if error_class == "usage_limit":
            # [quota-resume] A spent subscription window reopens hours later, so
            # retrying the SAME turn seconds later is guaranteed to fail again
            # and only re-sends a large prompt. Not retry-eligible: the Case is
            # PAUSED and resumes through the quota-restore path instead
            # (_handle_quota_paused_case), which is timed off real telemetry.
            return {"max_retries": 0, "initial_delay": 0.0, "backoff_multiplier": 1}
        if error_class == "rate_limit":
            return {"max_retries": max(2, default_max), "initial_delay": 2.0, "backoff_multiplier": max(2, default_mult)}
        return {"max_retries": default_max, "initial_delay": 1.0, "backoff_multiplier": default_mult}

    def _suggest_actions(self, error_class: str, result: TaskResult) -> List[str]:
        """Return actionable hints for common failure classes."""
        actions: List[str] = []
        ec = (error_class or "").lower()
        if ec == "interactive":
            actions.append("Enable skip-permissions or trust the folder; ensure non-interactive flags.")
        elif ec in ("rate_limit", "usage_limit"):
            info = self._extract_rate_limit_info(result)
            if info and info.get("resetsAt"):
                try:
                    reset_dt = datetime.fromtimestamp(int(info["resetsAt"]))
                    actions.append(f"Usage limit active. Tasks will resume automatically after {reset_dt.strftime('%H:%M')}.")
                except Exception:
                    actions.append("Usage limit active. Tasks will resume when the limit resets.")
            else:
                actions.append("Usage limit active. Tasks will resume when the limit resets.")
        elif ec == "timeout":
            backend_name = str(getattr(result, "backend_name", "") or "").lower()
            if backend_name.startswith("opencode"):
                actions.append("Increase OPENCODE_TIMEOUT_SEC or reduce task scope.")
            else:
                actions.append("Increase GATEWAY_TASK_TIMEOUT_SEC or reduce task scope.")
        elif ec == "network":
            actions.append("Check connectivity/VPN; retry with backoff.")
        elif ec == "upstream_error":
            actions.append("Anthropic's API returned a server-side error (5xx) — transient; retrying automatically.")
        elif ec == "context_overflow":
            actions.append("Session context is full. Run /compact on the session or start a new session.")
        elif ec == "max_turns":
            actions.append("Turn limit reached before finishing. Increase CLAUDE_SDK_MAX_TURNS or split the task into smaller steps.")
        elif ec == "sdk_stream_closed":
            actions.append("Claude SDK stream ended before a terminal ResultMessage; inspect worker-side Claude/SDK logs and resume from the modified tree, not by blind retry.")
        elif ec == "auth":
            actions.append("Run 'claude auth status' and re-authenticate if needed.")
        elif ec == "fatal":
            actions.append("Inspect stderr for root cause; adjust prompt/targets.")
        return actions

    def cancel_task(self, task_id: str) -> bool:
        """Request cooperative cancellation for a task.

        Returns True if a cancel signal was set for a queued or running task.
        """
        ev = self._task_cancel_events.get(task_id)
        if ev is None:
            # If task exists but no event yet (e.g., still queued elsewhere), create and set
            if task_id in self.active_tasks:
                ev = asyncio.Event()
                self._task_cancel_events[task_id] = ev
            else:
                return False
        if not ev.is_set():
            ev.set()
            t = self.active_tasks.get(task_id)
            # Interrupt the live backend turn directly, right now. The execution
            # loop's own graceful-cancel branch (which calls backend.cancel(session)
            # before tearing down exec_task) only fires if it's watching a
            # cancel_waiter built from THIS event — but that loop reads
            # `self._task_cancel_events.get(task.id)` once, before the task starts,
            # so for any task that was already running when cancel is requested
            # (the normal case) the event created just above is invisible to it.
            # Without this direct call, only the asyncio wrapper around
            # `asyncio.to_thread(...)` gets cancelled: the backend thread and its
            # live subprocess/session keep running unattended, never removed from
            # the driver's session pool, so the next turn on this session queues
            # up behind the abandoned one and appends the same prompt into the
            # same still-live conversation (the ever-growing-session bug).
            if t is not None:
                try:
                    session_id = (t.metadata or {}).get("session_id", "").strip()
                    session = self.session_store.get(session_id) if session_id else None
                    if session is not None:
                        backend_name = str(
                            (t.metadata or {}).get("backend") or session.backend or "claude"
                        ).strip().lower()
                        backend = self._backends.get(backend_name)
                        if backend is not None:
                            backend.cancel(session)
                except Exception:
                    logger.warning(
                        "event=cancel_backend_interrupt_failed task_id=%s", task_id, exc_info=True
                    )
            # Best-effort cancel running exec task
            task = self._running_exec_tasks.get(task_id)
            if task is not None and not task.done():
                task.cancel()
            # Emit cancel_requested event
            self._emit_event("cancel_requested", t if t else None, None)
            return True
        return False

    def _classify_error(self, result: TaskResult) -> str:
        """Classify error type for retry policy.

        Returns one of: none|interactive|rate_limit|usage_limit|timeout|network|upstream_error|
        context_overflow|max_turns|auth|fatal
        """
        if result.success:
            return "none"
        # Prefer explicit interactive marker from bridge
        try:
            if any(isinstance(e, str) and "interactive_prompt_detected" in e for e in (result.errors or [])):
                return "interactive"
        except Exception:
            pass
        # Structured SDK signal (the terminal ResultMessage's own subtype /
        # api_error_status) is more precise than guessing from free text —
        # prefer it before falling back to keyword matching below. A turn
        # that hit its max-turns budget is never expressible via the text
        # markers below (the CLI doesn't put that in prose); an
        # api_error_status means the turn's own lifecycle finished fine and
        # the underlying Anthropic API call failed (429 = rate limited,
        # 5xx = transient server-side failure) — reuse the existing
        # rate_limit/network retry policies rather than inventing new ones.
        subtype, api_error_status = self._extract_result_terminal_signal(result)
        if subtype == "error_max_turns":
            return "max_turns"
        if api_error_status == 429:
            return self._usage_limit_class(result)
        if api_error_status is not None and api_error_status >= 500:
            return "upstream_error"
        if self._extract_rate_limit_info(result) is not None:
            # A rejected rate_limit_event IS the subscription window (it carries
            # rateLimitType five_hour/daily + resetsAt) — the same condition the
            # 429 above reports, so it must land in the same class. Splitting it
            # off as "rate_limit" would give the identical failure two retry
            # policies and hide half the quota pauses from the resume path.
            return self._usage_limit_class(result)
        text = self._failure_text(result)
        text_lower = text.lower()
        # SUBSCRIPTION-WINDOW wording ("you've hit your limit", "session limit",
        # "usage limit", the overage banner) means the ACCOUNT's window is spent:
        # it reopens hours later, so it is a quota PAUSE with no quick retry.
        if any(s in text_lower for s in ("hit your limit", "hit your session limit", "session limit", "usage limit", "you've hit your limit", "overagestatus")):
            return "usage_limit"
        # GENERIC burst wording ("rate limit exceeded, please retry later", "too
        # many requests") is NOT evidence of a spent window — it is the classic
        # transient the retry policy exists for. Kept in its own class so a burst
        # still gets its two quick retries. Both classes still PAUSE a Case if
        # they end up terminal (QUOTA_PAUSE_ERROR_CLASSES covers both).
        if any(s in text_lower for s in ("rate limit", "rate-limit", "too many requests", "\"error\":\"rate_limit\"")):
            return "rate_limit"
        if any(s in text_lower for s in ("timeout", "timed out", "inactivity")):
            return "timeout"
        if any(s in text_lower for s in ("connection reset", "connection aborted", "network error", "503", "504", "temporarily unavailable", "terminated process", "cannot write to")):
            return "network"
        if any(s in text_lower for s in ("prompt is too long", "blocking_limit", "context_window", "context window")):
            return "context_overflow"
        if any(s in text_lower for s in ("unauthorized", "forbidden", "permission denied", "not logged in", "authentication")):
            return "auth"
        return "fatal"

    # ===========================================================================
    # EVENTS & TELEMETRY
    # _emit_event()             — append NDJSON line to logs/events.ndjson
    #                             (legacy envelope, backwards-compatible readers)
    # _emit_turn_telemetry()    — write LLM-turn telemetry to llm_events via the
    #                             telemetry sink (M3 Claude stream-json adapter)
    # create_task_from_description() — alternate Task factory used by control API
    # _send_task_heartbeats()   — periodic Telegram progress pings during long tasks
    # ===========================================================================

    def _emit_event(self, name: str, task: Optional[Task] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        """Append a single NDJSON event line to logs/events.ndjson.

        Thin wrapper over the shared observability spine. Preserves the legacy
        envelope keys (task_id, task_type, priority, status) so the existing
        `main.py stats` / `tail-events` readers keep parsing, while letting the
        spine fill node_id and any task_id/session_id from the correlation
        context automatically.
        """
        from src.core.observability import emit_event as _emit
        fields: Dict[str, Any] = {}
        task_id = None
        if task is not None:
            task_id = task.id
            fields.update({
                "task_type": getattr(task.type, "value", str(task.type)),
                "priority": getattr(task.priority, "value", str(task.priority)),
                "status": getattr(task.status, "value", str(task.status)),
            })
        if extra:
            fields.update(extra)
        task_id = fields.pop("task_id", task_id)
        _emit(name, task_id=task_id, **fields)

    def _emit_turn_telemetry(
        self,
        name: str,
        task: Task,
        attributes: Optional[Dict[str, Any]] = None,
        *,
        invocation_id: Optional[str] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        flush: bool = False,
    ) -> None:
        """Best-effort normalized telemetry through the controller sink."""
        try:
            from src.core.telemetry import EMITTER_PROCESS_INSTANCE_ID, build_event
            session_id = str((task.metadata or {}).get("session_id") or "") or None
            session = self.session_store.get(session_id) if session_id else None
            event_attributes = dict(attributes or {})
            if (
                name == "turn.started"
                and session is not None
                and session.backend_session_id
            ):
                event_attributes.setdefault(
                    "backend_session_id_start", session.backend_session_id
                )
            elif (
                name == "turn.completed"
                and session is not None
                and session.backend_session_id
            ):
                event_attributes.setdefault(
                    "backend_session_id_end", session.backend_session_id
                )
            self._telemetry_sink.emit(
                build_event(
                    name,
                    turn_id=task.id,
                    session_id=session_id,
                    node_id=socket.gethostname(),
                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                    source="gateway",
                    invocation_id=invocation_id,
                    backend=backend or (session.backend if session else self._resolve_task_backend(task)),
                    model=model or (session.model if session else None),
                    attributes=event_attributes,
                )
            )
            if name == "turn.started" and session is None:
                self._telemetry_sink.emit(
                    build_event(
                        "telemetry.coverage",
                        turn_id=task.id,
                        session_id=None,
                        node_id=socket.gethostname(),
                        emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                        source="gateway",
                        invocation_id=invocation_id,
                        backend=backend or self._resolve_task_backend(task),
                        model=model,
                        attributes={
                            "area": "postprocess",
                            "coverage": "unsupported",
                            "reason_code": "llama_postprocess_uninstrumented",
                            "adapter_version": "gateway-v1",
                        },
                    )
                )
            if flush:
                self._telemetry_sink.flush()
        except Exception:
            logger.warning("event=gateway_telemetry_emit_failed", exc_info=True)
    
    # Simulation execution removed: system now always runs real Claude Code CLI
    
    def create_task_from_description(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        """Create and persist a `.task.md` task from a natural language description.

        May use LLAMA to expand metadata; heuristically extracts `cwd` hints.
        Returns the created file path.
        """
        
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        
        # Use simple template for now - can be enhanced with LLAMA later
        parsed = self._parse_description_simple(description)

        # Override task type if provided
        if task_type:
            parsed["type"] = task_type
        
        # Override target files if provided
        if target_files:
            parsed["target_files"] = target_files
        
        # Heuristic: detect inline path hints like "in C:\\Users\\..." or "in /path/..."
        # and inject into frontmatter as `cwd` if allowed by config.
        try:
            import re
            path_hint = None
            # Windows-style absolute path after 'in '
            m = re.search(r"\bin\s+([A-Za-z]:\\[^\n\r]+)", description)
            if m:
                path_hint = m.group(1).strip()
            else:
                # POSIX-like
                m2 = re.search(r"\bin\s+(/[^\n\r]+)", description)
                if m2:
                    path_hint = m2.group(1).strip()
            if path_hint:
                parsed.setdefault("metadata", {})["cwd"] = path_hint
        except Exception:
            pass

        explicit_cwd = (cwd or parsed.get("metadata", {}).get("cwd") or "").strip()
        if explicit_cwd:
            resolved_cwd = PathResolver.from_config().resolve_execution_path(explicit_cwd)
            if resolved_cwd:
                parsed.setdefault("metadata", {})["cwd"] = resolved_cwd
            else:
                parsed.setdefault("metadata", {})["cwd"] = ""

        # Create task file
        task_content = f"""---
id: {task_id}
type: {parsed.get('type', 'analyze')}
priority: {parsed.get('priority', 'medium')}
created: {now_iso()}
cwd: {parsed.get('metadata', {}).get('cwd', '')}
session_id: {session_id or ""}
---

# {parsed.get('title', 'Auto-generated Task')}

**Target Files:**
{chr(10).join('- ' + f for f in parsed.get('target_files', []))}

**Prompt:**
{parsed.get('prompt', description)}

**Success Criteria:**
- [ ] Task completed successfully
- [ ] Results validated
- [ ] Documentation updated if needed

**Context:**
Generated from user description: {description}
"""
        
        # Write task file atomically: write to tmp then rename to final
        tasks_dir = Path(config.system.tasks_dir)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{task_id}.task.md"
        tmp_file = tasks_dir / f".{task_id}.task.tmp"
        tmp_file.write_text(task_content, encoding='utf-8')
        try:
            tmp_file.replace(task_file)
        except Exception:
            # Fallback to writing directly if replace fails
            task_file.write_text(task_content, encoding='utf-8')
        
        logger.info(f"Created task file: {task_file}")

        # Directly trigger processing so we don't rely solely on the file watcher.
        # On Windows, watchdog can miss the atomic rename event. Calling
        # _handle_new_task_file here ensures the task is always picked up even
        # if the watcher fires late or not at all.
        if self.running:
            asyncio.ensure_future(self._handle_new_task_file(str(task_file)))

        return task_id

    def _parse_description_simple(self, description: str) -> Dict[str, Any]:
        """Minimal task wrapper around a raw user instruction."""
        return {
            "type": "analyze",
            "title": f"Task: {description[:50]}...",
            "prompt": description,
            "priority": "medium",
            "target_files": []
        }
    
    async def _send_task_heartbeats(
        self,
        task: Task,
        session: Optional[Any],
        start_time: float,
        interval_sec: int,
        timeout_s: int,
    ) -> None:
        """Send periodic "still working" messages via the notifier."""
        chat_id = session.telegram_chat_id if session else None
        if not chat_id:
            return
        try:
            await asyncio.sleep(interval_sec)
            while True:
                elapsed = time.time() - start_time
                elapsed_min = int(elapsed // 60)
                remaining = timeout_s - elapsed if timeout_s else 0
                remaining_min = max(0, int(remaining // 60))

                await self.notifier.notify_heartbeat(
                    task.id,
                    session=session,
                    chat_id=chat_id,
                    elapsed_min=elapsed_min,
                    remaining_min=remaining_min,
                )
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            pass

    # ===========================================================================
    # STATUS / SESSION RECORDING
    # get_status()              — /api/health + /status aggregate: queue depth,
    #                             worker count, mesh state, active tasks.
    # _mesh_status()            — mesh-specific subset (node count, DB available).
    # _write_session_summary()  — persist per-session summary markdown.
    # _append_session_event()   — append to logs/session_events/<id>.log.
    # _extract_tool_summary()   — parse tool-use stats from raw_stdout.
    # ===========================================================================

    def _mesh_status(self) -> Dict[str, Any]:
        """Return operator-facing mesh mode without probing live services."""
        online_nodes: Optional[int] = None
        total_nodes: Optional[int] = None
        db_available: bool = False
        if config.mesh.enabled:
            try:
                from src.control.db import get_db
                db = get_db()
                db_available = db is not None
                if db is not None:
                    online_nodes = len(db.list_nodes(status="online"))
                    total_nodes = len(db.list_nodes())
            except Exception:
                db_available = False

        if not config.mesh.enabled:
            task_server_mode = "off"
        elif config.mesh.embedded_server:
            task_server_mode = "embedded-running" if self._embedded_task_server is not None else "embedded-configured"
        else:
            task_server_mode = "standalone"

        return {
            "enabled": bool(config.mesh.enabled),
            "task_server_mode": task_server_mode,
            "embedded_server": bool(config.mesh.embedded_server),
            "local_worker_capacity": len(self.worker_tasks),
            "configured_worker_capacity": int(config.system.max_concurrent_tasks),
            "fallback_capacity": len(self.worker_tasks) > 0,
            "db_available": db_available,
            "online_nodes": online_nodes,
            "total_nodes": total_nodes,
            "session_affinity_required": True,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive orchestrator status"""
        resolver = PathResolver.from_config()
        return {
            "running": self.running,
            "components": self.component_status,
            "tasks": {
                "active": len(self.active_tasks),
                "queued": self.task_queue.qsize(),
                "completed": len(self.task_results),
                "workers": len(self.worker_tasks)
            },
            "llama_status": self.llama_mediator.get_status(probe=False),
            "telegram": {
                "configured": bool(self.telegram_interface),
                "running": bool(self.telegram_interface and self.telegram_interface.is_running),
            },
            "mesh": self._mesh_status(),
            "scope": {
                "base_cwd": getattr(config.claude, "base_cwd", None),
                "allowed_root": getattr(config.claude, "allowed_root", None),
                "root_dirs": resolver.list_root_directories(limit=10),
            },
        }


    def _write_session_summary(self, session, result: TaskResult) -> None:
        """Write/overwrite a compact human-readable summary for a session."""
        try:
            # Same project-root anchor as SessionStore
            project_root = Path(__file__).resolve().parent.parent
            summaries_dir = project_root / "state" / "summaries"
            summaries_dir.mkdir(parents=True, exist_ok=True)
            files = result.files_modified or []
            files_section = "\n".join(f"- {f}" for f in files[:30]) if files else "(none)"
            lines = [
                f"# Session {session.session_id}",
                f"Backend: {session.backend}  |  Status: {session.status.value}",
                f"Path: {session.repo_path}",
                f"Updated: {session.updated_at}",
                f"Backend session: {session.backend_session_id or '(not yet captured)'}",
                "",
                f"## Last instruction",
                session.last_user_message or "(none)",
                "",
                f"## Last result (tail)",
                session.last_result_summary or "(none)",
                "",
                f"## Changed files",
                files_section,
                "",
                f"## Last artifact",
                session.last_artifact_path or "(none)",
            ]
            (summaries_dir / f"{session.session_id}.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"session_summary_write_failed id={session.session_id} error={e}")

    def _append_session_event(self, session_id: str, task_id: str, result: TaskResult) -> None:
        """Append one line to the per-session event log."""
        try:
            import json as _json
            log_dir = Path(config.system.logs_dir) / "session_events"
            log_dir.mkdir(parents=True, exist_ok=True)
            entry = _json.dumps({
                "timestamp": now_iso(),
                "task_id": task_id,
                "success": result.success,
                "execution_time": result.execution_time,
                "error": result.errors[0] if result.errors else "",
            }, ensure_ascii=False)
            with (log_dir / f"{session_id}.log").open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning(f"session_event_log_failed id={session_id} error={e}")

    @staticmethod
    def _extract_tool_summary(raw_stdout: str) -> dict:
        """Count tool calls by name and collect Bash commands from Claude Code's JSONL stdout."""
        counts: dict = {}
        bash_commands: list = []
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
                blocks = []
                if ev.get("type") == "assistant":
                    blocks = ev.get("message", {}).get("content") or []
                elif ev.get("type") == "tool_use":
                    blocks = [ev]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "unknown")
                    counts[name] = counts.get(name, 0) + 1
                    if name == "Bash":
                        cmd = (block.get("input") or {}).get("command", "")
                        if cmd:
                            bash_commands.append(cmd)
            except Exception:
                pass
        return {"calls": counts, "total": sum(counts.values()), "bash_commands": bash_commands}


    # ===========================================================================
    # MESH ROUTING
    # _run_backend_local()     — execute task on THIS machine using backend pool.
    # _dispatch_to_node()      — HTTP POST to remote node's task server; polls
    #                            for completion, handles pinned-node-offline grace.
    # _dispatch_or_run_local() — top-level router: local vs remote decision gate.
    # _nudge_worker_for_dispatch() — wake a sleeping worker daemon on the node.
    # _refresh_capable_nodes_before_routing() — pre-flight node capability check.
    # _dispatch_remote_close() — send a close signal to the node when a session ends.
    #
    # HARD INVARIANT: a session with machine_id set NEVER runs on a substitute
    # node — it waits, errors (PINNED_NODE_OFFLINE), or is operator re-pinned.
    #
    # FUTURE EXTRACTION → MeshRouter (post-M4, ~14 methods, highest value extract).
    #   This is the largest coherent cluster.  High coupling TODAY: needs
    #   session_store, push_service, notifier, _mesh_complete_task, _emit_event.
    #   Extract only once M4 executor shape is settled (no more routing additions).
    #   When done, inject deps via constructor; keep the affinity invariant as a
    #   MeshRouter-level assertion, not scattered across process_task callers.
    # ===========================================================================

    async def _run_backend_local(
        self,
        task: "Task",
        session: Optional[Any],
        backend_name: str,
    ) -> "TaskResult":
        """Execute the task on this machine using the local backend pool.

        This is the existing execution path extracted so that
        `_dispatch_or_run_local` can call it when mesh routing is off or no
        capable remote node is available.
        """
        from src.core.interfaces import ExecutionResult as _ER
        backend = self._backends.get(backend_name, self._backends["claude"])
        start = time.time()
        cancel_ev = self._task_cancel_events.get(task.id)

        if session:
            session.last_user_message = task.prompt
            if session.backend_session_id:
                exec_task = asyncio.create_task(
                    asyncio.to_thread(backend.resume_session, session, task.prompt)
                )
            else:
                exec_task = asyncio.create_task(
                    asyncio.to_thread(backend.create_session, session)
                )
        else:
            cwd_override = str((task.metadata or {}).get("cwd") or "").strip()
            if not cwd_override:
                cwd_override = str(getattr(config.claude, "base_cwd", "") or "").strip()
            exec_task = asyncio.create_task(
                asyncio.to_thread(backend.run_oneoff, cwd_override, task.prompt)
            )

        self._running_exec_tasks[task.id] = exec_task

        wait_set = {exec_task}
        cancel_waiter: Optional[asyncio.Task] = None
        if cancel_ev is not None:
            cancel_waiter = asyncio.create_task(cancel_ev.wait())
            wait_set.add(cancel_waiter)

        try:
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if cancel_waiter and not cancel_waiter.done():
                cancel_waiter.cancel()

        if exec_task in done:
            raw = exec_task.result()
            if isinstance(raw, _ER):
                if session and (
                    raw.backend_session_id
                    or session.cache_health != "unknown"
                    or session.driver_status
                ):
                    if raw.backend_session_id:
                        session.backend_session_id = raw.backend_session_id
                    self.session_store.save(session)
                result = TaskResult(
                    task_id=task.id,
                    success=raw.success,
                    output=raw.output,
                    errors=raw.errors,
                    files_modified=raw.files_modified,
                    execution_time=raw.execution_time,
                    timestamp=now_iso(),
                    file_changes=getattr(raw, "file_changes", []),
                    raw_stdout=getattr(raw, "raw_stdout", ""),
                    raw_stderr=getattr(raw, "raw_stderr", ""),
                    parsed_output=getattr(raw, "parsed_output", None),
                    return_code=getattr(raw, "return_code", 0),
                )
                setattr(result, "backend_name", backend_name)
                return result
            setattr(raw, "backend_name", backend_name)
            return raw

        # Cancel signal
        if session:
            import contextlib
            with contextlib.suppress(Exception):
                backend.cancel(session)
        exec_task.cancel()
        import contextlib
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task
        result = TaskResult(
            task_id=task.id,
            success=False,
            output="",
            errors=["cancelled"],
            files_modified=[],
            execution_time=time.time() - start,
            timestamp=now_iso(),
        )
        setattr(result, "backend_name", backend_name)
        return result

    def _dispatch_remote_close(self, session: Any) -> None:
        """Enqueue a fire-and-forget close_session task pinned to the session's
        owning node so the remote worker tears down its live backend session and
        frees the claude process.

        Injected into SessionService: a mesh /close used to be a no-op on the
        worker (event=session_backend_close_remote_skipped), leaking the process.
        This does NOT block the caller — the worker claims the pending task on its
        next poll. If the node is offline the task simply waits; the worker's boot
        reaper reclaims the process on restart regardless, so no leak survives.
        """
        machine_id = getattr(session, "machine_id", "") or ""
        if not machine_id:
            return
        from src.control.db import get_db
        db = get_db()
        if db is None:
            logger.warning(
                "event=remote_close_no_db session_id=%s node=%s",
                getattr(session, "session_id", ""), machine_id,
            )
            return
        session_id = getattr(session, "session_id", "") or ""
        backend = getattr(session, "backend", "") or "claude"
        payload = {
            "session": {
                "session_id": session_id,
                "backend": backend,
                "backend_session_id": getattr(session, "backend_session_id", "") or "",
                "machine_id": machine_id,
            }
        }
        task_id = f"close-{session_id}-{uuid.uuid4().hex[:8]}"
        try:
            db.enqueue_task(
                task_id=task_id,
                session_id=session_id,
                machine_id=machine_id,
                backend=backend,
                action="close_session",
                payload=payload,
            )
        except Exception as e:
            logger.warning(
                "event=remote_close_enqueue_failed session_id=%s node=%s err=%s",
                session_id, machine_id, e,
            )
            return
        logger.info(
            "event=remote_close_enqueued session_id=%s node=%s task_id=%s",
            session_id, machine_id, task_id,
        )

    async def _dispatch_to_node(
        self,
        task: "Task",
        session: Optional[Any],
        node: Any,
    ) -> "TaskResult":
        """Enqueue task on the DB as pending (it is already enqueued by _mesh_enqueue_task).

        Then poll the DB until it reaches completed/failed status, up to
        `mesh.oneoff_queue_timeout_sec` seconds. Once a worker claims it, the
        queue timeout no longer applies; the worker owns execution and we wait
        for the terminal DB state.

        The session dict is embedded in the payload by _mesh_enqueue_task so
        the worker can reconstruct the Session object.
        """
        import asyncio as _aio
        from src.control.db import get_db

        db = get_db()
        if db is None:
            # DB unavailable: cannot dispatch to worker. Fail loudly rather
            # than silently falling back to local execution, which would break
            # backend_session_id continuity for machine-pinned sessions.
            result = TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=["Mesh DB unavailable; cannot dispatch to remote worker"],
                files_modified=[],
                execution_time=0.0,
                timestamp=now_iso(),
            )
            setattr(result, "backend_name", self._resolve_task_backend(task))
            return result

        pickup_timeout_sec = getattr(config.mesh, "oneoff_queue_timeout_sec", 600)
        pickup_deadline = time.time() + pickup_timeout_sec
        poll_interval = 3.0
        first_poll = True
        target_node_id = getattr(node, "node_id", None) or (session.machine_id if session else "")
        await _aio.to_thread(self._nudge_worker_for_dispatch, node, target_node_id, db)

        while True:
            row = db.get_task(task.id)
            if row is None and first_poll:
                # Row not found on the very first poll — _mesh_enqueue_task
                # must have failed silently. Fail fast instead of burning the
                # full timeout (up to 600s) before the user sees an error.
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=["Task row missing from DB — enqueue failed before dispatch; check logs for mesh_enqueue_failed"],
                    files_modified=[],
                    execution_time=0.0,
                    timestamp=now_iso(),
                )
                setattr(result, "backend_name", self._resolve_task_backend(task))
                return result
            first_poll = False
            if row:
                status = row.get("status", "pending")
                if status == "completed":
                    result_raw = row.get("result")
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                    except Exception:
                        r = {}
                    # Propagate the worker's backend_session_id so the next
                    # turn can resume the remote-side backend session.
                    new_bsid = r.get("backend_session_id", "")
                    if session:
                        changed = False
                        if new_bsid:
                            session.backend_session_id = new_bsid
                            changed = True
                        for attr in ("driver_type", "driver_status", "cache_health"):
                            value = r.get(attr)
                            if value is not None:
                                setattr(session, attr, value)
                                changed = True
                        if "cache_unhealthy_count" in r:
                            session.cache_unhealthy_count = int(r.get("cache_unhealthy_count") or 0)
                            changed = True
                        if "previous_backend_session_ids" in r:
                            session.previous_backend_session_ids = r.get("previous_backend_session_ids") or []
                            changed = True
                        if changed:
                            self.session_store.save(session)
                    worker_output = r.get("output", "")
                    result = TaskResult(
                        task_id=task.id,
                        success=r.get("success", True),
                        output=worker_output,
                        errors=r.get("errors") or [],
                        files_modified=r.get("files_modified") or [],
                        execution_time=r.get("execution_time", 0.0),
                        timestamp=r.get("timestamp", now_iso()),
                        return_code=r.get("return_code", 0),
                        # Prefer the worker's real transcript when it ships one
                        # (full backend NDJSON); fall back to mirroring `output`
                        # for older workers so the artifact JSON (which persists
                        # raw_stdout, not output) never ends up empty (T2).
                        raw_stdout=r.get("raw_stdout") or worker_output,
                        raw_stderr=r.get("raw_stderr") or "",
                    )
                    setattr(result, "usage", r.get("usage"))
                    setattr(result, "backend_name", row.get("backend", "claude"))
                    if r.get("error_detail"):
                        setattr(result, "error_detail", r.get("error_detail"))
                    setattr(
                        result,
                        "telemetry_invocation_id",
                        r.get("telemetry_invocation_id") or None,
                    )
                    return result

                if status in ("failed", "failed_node_offline"):
                    result_raw = row.get("result")
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                    except Exception:
                        r = {}
                    if session and r:
                        changed = False
                        for attr in ("driver_type", "driver_status", "cache_health"):
                            value = r.get(attr)
                            if value is not None:
                                setattr(session, attr, value)
                                changed = True
                        if "cache_unhealthy_count" in r:
                            session.cache_unhealthy_count = int(r.get("cache_unhealthy_count") or 0)
                            changed = True
                        if "previous_backend_session_ids" in r:
                            session.previous_backend_session_ids = r.get("previous_backend_session_ids") or []
                            changed = True
                        if changed:
                            self.session_store.save(session)
                    error_msg = row.get("error") or f"Task {status}"
                    error_detail = (r.get("error_detail") if r else "") or ""
                    result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output=r.get("output", "") if r else "",
                        errors=r.get("errors") or [error_msg],
                        files_modified=r.get("files_modified") or [],
                        execution_time=r.get("execution_time", 0.0),
                        timestamp=r.get("timestamp", now_iso()) if r else now_iso(),
                        return_code=r.get("return_code", 1) if r else 1,
                        # Preserve the worker's full backend transcript (NDJSON)
                        # when shipped; only mirror `output` for legacy workers.
                        # Previously raw_stdout was overwritten with `output`,
                        # which erased the error_during_execution marker and hid
                        # the agent's complete payload from the artifact.
                        raw_stdout=(r.get("raw_stdout") if r else "") or (r.get("output", "") if r else ""),
                        raw_stderr=(r.get("raw_stderr") if r else "") or error_detail,
                    )
                    setattr(result, "error_detail", error_detail)
                    setattr(result, "usage", r.get("usage") if r else None)
                    setattr(result, "backend_name", row.get("backend", "claude"))
                    setattr(
                        result,
                        "telemetry_invocation_id",
                        r.get("telemetry_invocation_id") if r else None,
                    )
                    return result

                if status != "claimed" and time.time() >= pickup_deadline:
                    db.fail_task(task.id, f"dispatch timeout after {pickup_timeout_sec}s waiting for worker")
                    result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output="",
                        errors=[f"Dispatch timeout: no worker picked up the task within {pickup_timeout_sec}s"],
                        files_modified=[],
                        execution_time=pickup_timeout_sec,
                        timestamp=now_iso(),
                    )
                    setattr(result, "backend_name", self._resolve_task_backend(task))
                    return result

                # status == claimed: a worker has picked up the task. Do not
                # apply the pickup timeout to execution time; wait for the
                # worker's real completed/failed state or an offline update.
            elif time.time() >= pickup_deadline:
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=["Task row disappeared from DB while waiting for worker pickup"],
                    files_modified=[],
                    execution_time=pickup_timeout_sec,
                    timestamp=now_iso(),
                )
                setattr(result, "backend_name", self._resolve_task_backend(task))
                return result

            # Check for cancellation
            cancel_ev = self._task_cancel_events.get(task.id)
            if cancel_ev and cancel_ev.is_set():
                # Distinguish a genuine user cancel from a gateway shutdown.
                # On shutdown we are only *detaching* our poll loop — the remote
                # worker keeps running and owns the task's real terminal state in
                # the DB. Writing fail_task here would fabricate a 'failed' row
                # that overwrites the worker's truth, which is exactly the
                # restart-cancel bug. So on shutdown we leave the DB row as-is
                # (still 'claimed') and return a non-terminal detached result;
                # startup recovery (_recover_stale_busy_sessions) reattaches and
                # reports whatever the worker actually wrote.
                interrupted = task.id in self._shutdown_interrupted_tasks
                if not interrupted:
                    db.fail_task(task.id, "cancelled by gateway")
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=["interrupted by gateway restart" if interrupted else "cancelled"],
                    files_modified=[],
                    execution_time=0.0,
                    timestamp=now_iso(),
                )
                setattr(result, "backend_name", self._resolve_task_backend(task))
                setattr(result, "detached", interrupted)
                return result

            await _aio.sleep(poll_interval)

    def _nudge_worker_for_dispatch(self, node: Any, node_id: str, db: Any) -> bool:
        """Best-effort wake-up for a worker after enqueuing remote work.

        Prefers the in-memory node object (avoids a DB round-trip when we
        already have fresh registration data). Falls back to a DB lookup when
        the node object is absent or lacks address fields.
        """
        import urllib.request

        # Fast path: use the in-memory node's address when available.
        tailscale_ip = getattr(node, "tailscale_ip", None) or ""
        api_port = getattr(node, "api_port", None) or 0
        if tailscale_ip and api_port:
            try:
                url = f"http://{tailscale_ip}:{api_port}/nudge"
                req = urllib.request.Request(url, method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=2):
                    pass
                return True
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "event=nudge_failed node_id=%s err=%s", node_id, e
                )
                return False

        # Slow path: look up from DB (covers cases where node=None or has no address).
        from src.control.node_inspector import nudge_node_direct
        return nudge_node_direct(node_id, db)

    async def _refresh_capable_nodes_before_routing(self, registry: Any, backend_name: str) -> None:
        """Nudge capable workers and briefly wait for fresher live_state."""
        import asyncio as _aio
        from src.control.db import get_db

        wait_sec = float(getattr(config.mesh, "routing_freshness_wait_sec", 2.0) or 0.0)
        if wait_sec <= 0:
            return

        try:
            candidates = registry.list_capable(backend_name)
        except Exception:
            candidates = []
        if not candidates:
            return

        before = {
            node.node_id: node.live_state_updated_at
            for node in candidates
        }
        db = get_db()
        await _aio.gather(
            *[
                _aio.to_thread(self._nudge_worker_for_dispatch, node, node.node_id, db)
                for node in candidates
            ],
            return_exceptions=True,
        )

        deadline = time.time() + wait_sec
        while time.time() < deadline:
            for node in candidates:
                if node.live_state_updated_at and node.live_state_updated_at != before.get(node.node_id):
                    logger.debug(
                        "event=mesh_preroute_fresh_state node_id=%s backend=%s",
                        node.node_id,
                        backend_name,
                    )
                    return
            await _aio.sleep(0.1)

    async def _dispatch_or_run_local(
        self,
        task: "Task",
        session: Optional[Any],
        backend_name: str,
    ) -> "TaskResult":
        """Route task to a worker node or fall back to local execution.

        `MESH_ENABLED=false` (default) → always runs locally, zero regression.
        `MESH_ENABLED=true`            → routes through node registry when nodes
                                         are available; falls back to local if not.
        """
        from src.control.node_registry import get_registry

        if not config.mesh.enabled:
            return await self._run_backend_local(task, session, backend_name)

        registry = get_registry()
        if registry.is_empty():
            return await self._run_backend_local(task, session, backend_name)

        def _routing_failure(msg: str) -> "TaskResult":
            result = TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[msg],
                files_modified=[],
                execution_time=0.0,
                timestamp=now_iso(),
            )
            setattr(result, "backend_name", backend_name)
            return result

        if session and session.machine_id:
            node = registry.get(session.machine_id)
            if not node or node.status != "online":
                return _routing_failure(f"Node {session.machine_id!r} is offline; cannot continue session")
        else:
            await self._refresh_capable_nodes_before_routing(registry, backend_name)
            node = registry.pick_capable(
                backend=backend_name,
                max_live_state_age_sec=getattr(config.mesh, "routing_live_state_max_age_sec", 90),
            )
            if not node:
                return _routing_failure(
                    f"No online node supports backend {backend_name!r} with available capacity"
                )

        return await self._dispatch_to_node(task, session, node)

    # ===========================================================================
    # MESH DB SHADOW-WRITE HELPERS
    # _mesh_enqueue_task()         — insert/self-claim a mesh_tasks row at dispatch.
    # _mesh_complete_task()        — write terminal result into the mesh_tasks row.
    # _spool_mesh_completion_reconcile() — persist to results/reconcile/ if DB write
    #                                      fails (replayed on next startup).
    # reconcile_spooled_mesh_completions() — drain the reconcile spool into DB.
    # mesh_reconcile_status()      — operator read of spool health.
    # ===========================================================================

    def _mesh_enqueue_task(self, task: Task, backend_name: str) -> None:
        """Shadow-write a dispatched task into mesh_tasks.

        Two cases (the split MUST match `process_task`'s local/remote decision,
        which routes remote ⟺ machine_id is set AND machine_id != this host):

        Local execution (no machine_id, OR machine_id names THIS host, OR
        MESH_ENABLED=false):
          Insert + immediately self-claim under this host's identity so no
          worker daemon can pick up the row as claimable work. The row is a
          faithful historical mirror; `_mesh_complete_task` finalises it.

        Remote dispatch (machine_id names a DIFFERENT host and MESH_ENABLED=true):
          Insert as 'pending' WITHOUT self-claiming. The row is the actual
          dispatch signal — the pinned worker polls `get_pending_tasks`, sees
          it (machine_id filter matches), claims it, executes, and posts the
          result. `process_task` (via `_dispatch_to_node`) polls the DB for
          completion. `_mesh_complete_task` later enriches the row with the
          local artifact_path.

        BUGFIX: the self-claim used to fire only when machine_id was UNSET, so a
        session pinned to THIS host (e.g. a standalone worker daemon sharing the
        gateway's hostname as its node_id) left the row 'pending' AND was run
        locally by `process_task` — the daemon then claimed the same row and ran
        it a SECOND time (two agents for one task). Treating machine_id == host as
        local closes that: the gateway owns host-local execution, single-writer.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            session_id = (task.metadata or {}).get("session_id", "").strip() or None
            session = self.session_store.get(session_id) if session_id else None
            machine_id = (session.machine_id or None) if session else None
            host = socket.gethostname()
            action_override = (task.metadata or {}).get("task_type", "")
            if action_override == "fetch_staged_file":
                action = "fetch_staged_file"
            elif session_id and session and not session.backend_session_id:
                action = "create_session"
            elif session_id:
                action = "resume_session"
            else:
                action = "run_oneoff"
            payload = {
                "prompt": task.prompt,
                "task_id": task.id,
                "action": action,
                "metadata": task.metadata or {},
                "telemetry": {
                    "schema_version": 1,
                    "turn_id": task.id,
                    "session_id": session_id,
                    "gateway_node_id": host,
                    "attempt": 1,
                    "spawn_reason": "initial",
                },
            }
            if session:
                payload["session"] = _session_dispatch_payload(session)
                # [Remote first-turn] The node worker's create_session path seeds the
                # FIRST user message from session.last_user_message
                # (claude_code.create_session → start_session(session.last_user_message)),
                # NOT from payload["prompt"]. The gateway only sets last_user_message
                # LATER, inside process_task (after this snapshot) — so a node-pinned
                # create_session (every Manager's first turn) would ship an EMPTY first
                # message and the objective/injected prior-context would be dropped
                # (the Manager boots and reports "your message is empty"). Snapshot THIS
                # turn's prompt (already compact-context-injected above) as the first
                # message so a remote create_session carries the objective + prior context.
                payload["session"]["last_user_message"] = task.prompt
            # Runs on THIS host ⟺ no pin, or the pin names this host. Only a pin
            # to a DIFFERENT host is a true remote dispatch. This MUST mirror
            # process_task's `_pinned_elsewhere` test, or a host-pinned task both
            # runs locally AND stays claimable by a daemon → double execution.
            runs_locally = (not machine_id) or (machine_id == host)
            db.enqueue_task(
                task_id=task.id,
                session_id=session_id,
                machine_id=machine_id,
                backend=backend_name,
                action=action,
                payload=payload,
            )
            # Self-claim when this task runs on THIS host so no worker daemon can
            # pick up the row. A row pinned to a DIFFERENT host stays 'pending' so
            # that remote worker can claim it via get_pending_tasks.
            if runs_locally:
                if not db.claim_task(task.id, host):
                    logger.warning(
                        "event=mesh_self_claim_failed task_id=%s host=%s — "
                        "row may be claimable by a remote worker",
                        task.id, host,
                    )
        except Exception as e:
            if machine_id and machine_id != host:
                # Remote dispatch depends on this row existing — log loudly so
                # the operator sees it immediately rather than after a 600s poll timeout.
                logger.error(
                    "event=mesh_enqueue_failed task_id=%s machine_id=%s err=%s — "
                    "worker will never see this task; dispatch will timeout",
                    task.id, machine_id, e,
                )
            else:
                logger.debug("event=mesh_enqueue_failed task_id=%s err=%s", task.id, e)

    def _mesh_reconcile_dir(self) -> Path:
        return Path(config.system.results_dir) / "reconcile"

    def _spool_mesh_completion_reconcile(
        self,
        task: Task,
        result: "TaskResult",
        artifact_path: Optional[str],
        reason: str,
    ) -> None:
        """Persist a completed task for later DB reconciliation."""
        try:
            spool_dir = self._mesh_reconcile_dir()
            spool_dir.mkdir(parents=True, exist_ok=True)
            payload: Dict[str, Any] = {
                "schema_version": 1,
                "task": {
                    "id": task.id,
                    "type": getattr(task.type, "value", str(task.type)),
                    "priority": getattr(task.priority, "value", str(task.priority)),
                    "status": getattr(task.status, "value", str(task.status)),
                    "created": task.created,
                    "title": task.title,
                    "target_files": list(task.target_files or []),
                    "prompt": task.prompt or "",
                    "success_criteria": list(task.success_criteria or []),
                    "context": task.context or "",
                    "metadata": task.metadata or {},
                },
                "result": {
                    "task_id": result.task_id,
                    "success": result.success,
                    "output": result.output or "",
                    "errors": list(result.errors or []),
                    "files_modified": list(result.files_modified or []),
                    "execution_time": result.execution_time,
                    "timestamp": result.timestamp,
                    "file_changes": list(getattr(result, "file_changes", None) or []),
                    "raw_stdout": getattr(result, "raw_stdout", "") or "",
                    "raw_stderr": getattr(result, "raw_stderr", "") or "",
                    "parsed_output": getattr(result, "parsed_output", None),
                    "return_code": getattr(result, "return_code", 0),
                    "usage": getattr(result, "usage", None),
                    "retries": getattr(result, "retries", 0),
                    "error_class": getattr(result, "error_class", "") or "",
                    "backend_name": getattr(result, "backend_name", ""),
                },
                "artifact_path": artifact_path or "",
                "reason": reason,
                "created_at": now_iso(),
                "reconciled": False,
            }
            (spool_dir / f"{task.id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.warning(
                "event=mesh_completion_reconcile_spooled task_id=%s reason=%s",
                task.id,
                reason,
            )
        except Exception as e:
            logger.error(
                "event=mesh_completion_reconcile_spool_failed task_id=%s err=%s",
                task.id,
                e,
            )

    def _task_from_reconcile_payload(self, payload: Dict[str, Any]) -> Task:
        data = payload.get("task") or {}
        try:
            task_type = TaskType(data.get("type") or TaskType.FIX.value)
        except Exception:
            task_type = TaskType.FIX
        try:
            priority = TaskPriority(data.get("priority") or TaskPriority.MEDIUM.value)
        except Exception:
            priority = TaskPriority.MEDIUM
        try:
            status = TaskStatus(data.get("status") or TaskStatus.COMPLETED.value)
        except Exception:
            status = TaskStatus.COMPLETED
        return Task(
            id=str(data.get("id") or ""),
            type=task_type,
            priority=priority,
            status=status,
            created=str(data.get("created") or now_iso()),
            title=str(data.get("title") or data.get("id") or "reconciled task"),
            target_files=list(data.get("target_files") or []),
            prompt=str(data.get("prompt") or ""),
            success_criteria=list(data.get("success_criteria") or []),
            context=str(data.get("context") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def _result_from_reconcile_payload(self, payload: Dict[str, Any]) -> TaskResult:
        data = payload.get("result") or {}
        result = TaskResult(
            task_id=str(data.get("task_id") or (payload.get("task") or {}).get("id") or ""),
            success=bool(data.get("success")),
            output=str(data.get("output") or ""),
            errors=list(data.get("errors") or []),
            files_modified=list(data.get("files_modified") or []),
            execution_time=float(data.get("execution_time") or 0.0),
            timestamp=str(data.get("timestamp") or now_iso()),
            file_changes=list(data.get("file_changes") or []),
            raw_stdout=str(data.get("raw_stdout") or ""),
            raw_stderr=str(data.get("raw_stderr") or ""),
            parsed_output=data.get("parsed_output"),
            return_code=int(data.get("return_code") or 0),
            usage=data.get("usage"),
            retries=int(data.get("retries") or 0),
            error_class=str(data.get("error_class") or ""),
        )
        backend_name = str(data.get("backend_name") or "")
        if backend_name:
            setattr(result, "backend_name", backend_name)
        return result

    def _ensure_reconcile_task_row(self, db: Any, task: Task, result: TaskResult) -> None:
        if db.get_task(task.id):
            return
        metadata: Dict[str, Any] = task.metadata or {}
        session_id = str(metadata.get("session_id") or "").strip() or None
        backend_name = str(getattr(result, "backend_name", "") or metadata.get("backend") or "claude")
        action = "resume_session" if session_id else "run_oneoff"
        machine_id: Optional[str] = None
        if session_id:
            try:
                session = self.session_store.get(session_id)
                if session:
                    machine_id = session.machine_id or None
            except Exception:
                machine_id = None
        db.enqueue_task(
            task_id=task.id,
            session_id=session_id,
            machine_id=machine_id,
            backend=backend_name,
            action=action,
            payload={
                "prompt": task.prompt,
                "task_id": task.id,
                "action": action,
                "metadata": metadata,
            },
        )

    def reconcile_spooled_mesh_completions(self, limit: int = 100) -> Dict[str, int]:
        """Replay completed task DB mirrors that were spooled during DB outage."""
        if self._mesh_reconcile_in_progress:
            return {"checked": 0, "reconciled": 0, "failed": 0}
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            return {"checked": 0, "reconciled": 0, "failed": 0}
        if db is None:
            return {"checked": 0, "reconciled": 0, "failed": 0}

        spool_dir = self._mesh_reconcile_dir()
        if not spool_dir.exists():
            return {"checked": 0, "reconciled": 0, "failed": 0}

        checked: int = 0
        reconciled: int = 0
        failed: int = 0
        self._mesh_reconcile_in_progress = True
        try:
            for path in sorted(spool_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
                if checked >= limit:
                    break
                checked += 1
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("reconciled"):
                        continue
                    task = self._task_from_reconcile_payload(payload)
                    result = self._result_from_reconcile_payload(payload)
                    if not task.id or not result.task_id:
                        raise ValueError("missing task_id")
                    self._ensure_reconcile_task_row(db, task, result)
                    self._mesh_complete_task(task, result, payload.get("artifact_path") or None)
                    row = db.get_task(task.id)
                    if row and row.get("status") in {"completed", "failed", "failed_node_offline"}:
                        payload["reconciled"] = True
                        payload["reconciled_at"] = now_iso()
                        path.write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        reconciled += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.warning(
                        "event=mesh_completion_reconcile_failed path=%s err=%s",
                        path,
                        e,
                    )
        finally:
            self._mesh_reconcile_in_progress = False

        if checked:
            logger.info(
                "event=mesh_completion_reconcile checked=%s reconciled=%s failed=%s",
                checked,
                reconciled,
                failed,
            )
        return {"checked": checked, "reconciled": reconciled, "failed": failed}

    def mesh_reconcile_status(self, limit: int = 1000) -> Dict[str, Any]:
        """Summarize pending DB-completion reconcile spool files for operators."""
        spool_dir = self._mesh_reconcile_dir()
        if not spool_dir.exists():
            return {
                "total": 0,
                "pending": 0,
                "reconciled": 0,
                "invalid": 0,
                "oldest_pending_at": None,
                "latest_reconciled_at": None,
            }

        total: int = 0
        pending: int = 0
        reconciled: int = 0
        invalid: int = 0
        oldest_pending_at: Optional[str] = None
        latest_reconciled_at: Optional[str] = None
        for path in sorted(spool_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            if total >= max(1, limit):
                break
            total += 1
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                invalid += 1
                continue
            if payload.get("reconciled"):
                reconciled += 1
                reconciled_at = str(payload.get("reconciled_at") or "")
                if reconciled_at and (latest_reconciled_at is None or reconciled_at > latest_reconciled_at):
                    latest_reconciled_at = reconciled_at
            else:
                pending += 1
                created_at = str(payload.get("created_at") or "")
                if created_at and (oldest_pending_at is None or created_at < oldest_pending_at):
                    oldest_pending_at = created_at

        return {
            "total": total,
            "pending": pending,
            "reconciled": reconciled,
            "invalid": invalid,
            "oldest_pending_at": oldest_pending_at,
            "latest_reconciled_at": latest_reconciled_at,
        }

    # ===========================================================================
    # PROACTIVE TURNS — REACH-BACK NOTIFICATIONS
    # When a worker node completes a turn autonomously (M3.4 continuation or a
    # background worker reporting progress), the mesh task server calls
    # _handle_proactive_turn() on the orchestrator to fan out notifications
    # (Web Push + Telegram) without blocking the worker.
    # _notify_proactive_turn() does the actual async fan-out.
    # ===========================================================================

    def _handle_proactive_turn(
        self,
        session_id: str,
        task_id: str,
        text: str,
        backend_session_id: str = "",
    ) -> None:
        """Hook invoked by the mesh task server (on its threadpool) when a worker
        reports an autonomous turn. The turn is already persisted to the DB; here
        we marshal the live notification onto the orchestrator loop so the user
        gets actively reached (web push / Telegram) — the "reach back"."""
        loop = getattr(self, "_loop", None)
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._notify_proactive_turn(session_id, task_id, text, backend_session_id),
                loop,
            )
        except Exception as e:
            logger.warning("event=proactive_notify_schedule_failed session_id=%s err=%s", session_id, e)

    async def _notify_proactive_turn(
        self,
        session_id: str,
        task_id: str,
        text: str,
        backend_session_id: str = "",
    ) -> None:
        """Deliver a proactive turn through the same UI-agnostic notifier every
        normal turn uses, so WebUI (SSE), web push and Telegram all inherit it."""
        from types import SimpleNamespace
        try:
            session = self.session_store.get(session_id)
            if session is None:
                return
            # The autonomous turn advanced the live backend session — keep the
            # gateway's record in step so the next resume continues cleanly.
            if backend_session_id and backend_session_id != getattr(session, "backend_session_id", ""):
                session.backend_session_id = backend_session_id
            # Mirror it into the session's file-side history too (the file
            # transcript fallback), flagged so it's never shown with a fake user
            # message.
            try:
                session.task_history.append({
                    "task_id": task_id,
                    "timestamp": now_iso(),
                    "success": True,
                    "execution_time": 0.0,
                    "user_message": "",
                    "result_summary": text,
                    "files_modified": [],
                    "proactive": True,
                })
                session.task_history = session.task_history[-20:]
                session.last_result_summary = text[-400:] if len(text) > 400 else text
                session.last_summary = session.last_result_summary
                self.session_store.save(session)
            except Exception:
                pass
            self._emit_event("proactive_turn", None, {"session_id": session_id, "task_id": task_id})
            result_like = SimpleNamespace(
                success=True,
                output=text,
                files_modified=[],
                parsed_output=None,
                raw_stdout="",
                errors=[],
                usage=None,
                execution_time=0.0,
                error_class="",
                return_code=0,
                timestamp=now_iso(),
            )
            chat_id = getattr(session, "telegram_chat_id", None)
            await self.notifier.notify_task_outcome(
                task_id, result_like, session=session, chat_id=chat_id,
            )
        except Exception as e:
            logger.warning("event=proactive_notify_failed session_id=%s err=%s", session_id, e)

    def _mesh_complete_task(self, task: Task, result: "TaskResult", artifact_path: Optional[str]) -> None:
        """Shadow-write the task result into mesh_tasks — the canonical, file-free store.

        Two writes:
          1. The legacy ``result`` JSON (``output`` still capped at 2000 for the
             small-payload list/preview consumers and back-compat).
          2. ``enrich_task`` with the FULL untruncated reply + structured fields
             (parsed_output, file_changes, usage, prompt) so the DB holds
             everything ``results/task_*.json`` did. This is what lets the chat
             transcript and Files/Info tabs read from the DB and lets the fat
             artifact files be dropped. Only ``raw_stdout`` (debug NDJSON) stays
             on disk, gzipped.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                self._spool_mesh_completion_reconcile(task, result, artifact_path, "db unavailable")
                return
            replay = getattr(self, "reconcile_spooled_mesh_completions", None)
            if callable(replay) and not getattr(self, "_mesh_reconcile_in_progress", False):
                replay(limit=25)
            result_dict = {
                "success": result.success,
                "output": result.output[:2000] if result.output else "",  # small preview only
                "errors": result.errors or [],
                "files_modified": result.files_modified or [],
                "execution_time": result.execution_time,
                "timestamp": result.timestamp,
                "return_code": getattr(result, "return_code", 0),
            }
            if result.success:
                db.complete_task(task.id, result_dict, artifact_path)
            else:
                error_str = "; ".join(result.errors) if result.errors else "unknown error"
                db.fail_task(task.id, error_str, result=result_dict, artifact_path=artifact_path)

            # Full artifact-complete enrichment — the file-free conversation store.
            if result.success:
                reply_text = self._session_reply_text(result).strip()
            else:
                # A failed turn may still carry a deliverable reply — e.g. a
                # context-overflow turn that salvaged the agent's real progress
                # (driver builds banner + bounded work into result.output). Prefer
                # that so the user gets the work, not just a terse reason. Fall
                # back to the short failure reason when output is empty.
                salvaged = (getattr(result, "output", "") or "").strip()
                if salvaged:
                    reply_text = salvaged
                else:
                    reply_text = (self._short_failure_reason(result) or "(failed)").strip()
            usage = getattr(result, "usage", None)
            try:
                if usage is None:
                    from src.services.result_text import extract_usage_from_ndjson
                    usage = extract_usage_from_ndjson(getattr(result, "raw_stdout", "") or "")
            except Exception:
                usage = None
            # Prompt: task.prompt is the source for runtime tasks, but for some
            # dispatch paths it's empty while session.last_user_message holds the
            # full instruction (same precedence the file transcript used). Fall
            # back so the user turn is never blank.
            prompt_text = (task.prompt or "").strip()
            if not prompt_text:
                try:
                    sid = (task.metadata or {}).get("session_id", "").strip()
                    if sid:
                        _s = self.session_store.get(sid)
                        if _s:
                            prompt_text = (_s.last_user_message or "").strip()
                except Exception:
                    pass
            db.enrich_task(
                task.id,
                prompt=prompt_text or None,
                reply_text=reply_text,
                parsed_output=getattr(result, "parsed_output", None),
                file_changes=getattr(result, "file_changes", None) or None,
                files_modified=result.files_modified or [],
                usage=usage,
                error_class=getattr(result, "error_class", "") or None,
                return_code=getattr(result, "return_code", None),
            )
        except Exception as e:
            logger.debug("event=mesh_complete_failed task_id=%s err=%s", task.id, e)
            self._spool_mesh_completion_reconcile(task, result, artifact_path, str(e))


class _ContextLoader:
    """Lightweight loader that produces compact, prompt-ready context.

    Reads the canonical `mesh_tasks` row first, then falls back to
    `results/index.json` / `results/{task_id}.json` for old artifacts. Returns a
    small dictionary containing bounded prompt context, summary, constraints,
    usage, and files.
    """

    SUMMARY_LIMIT = 2000
    PROMPT_LIMIT = 1000
    ERROR_LIMIT = 5

    def __init__(
        self,
        index_path: Path,
        results_dir: Path,
        db_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._index_path = index_path
        self._results_dir = results_dir
        self._db_factory = db_factory

    def load(self, task_id: str) -> Dict[str, Any]:
        default: Dict[str, Any] = {
            "task_id": task_id,
            "source": "none",
            "prompt": "",
            "summary": "",
            "constraints": {},
            "files_modified": [],
            "usage": {},
            "errors": [],
        }
        try:
            row = self._load_db_row(task_id)
            if row is not None:
                return self._from_db_row(task_id, row)
            data = self._load_artifact(task_id)
            if data is not None:
                return self._from_artifact(task_id, data)
        except Exception:
            pass
        return default

    def _load_db_row(self, task_id: str) -> Optional[Dict[str, Any]]:
        if self._db_factory is None:
            return None
        try:
            db = self._db_factory()
            if db is None:
                return None
            row = db.get_task(task_id)
            return row if isinstance(row, dict) else None
        except Exception:
            return None

    def _load_artifact(self, task_id: str) -> Optional[Dict[str, Any]]:
        artifact_path: Optional[Path] = None
        if self._index_path.exists():
            try:
                idx = json.loads(self._index_path.read_text(encoding="utf-8"))
                p = idx.get(str(task_id))
                if p:
                    ap = Path(p)
                    if ap.exists():
                        artifact_path = ap
            except Exception:
                artifact_path = None
        if artifact_path is None:
            cand = self._results_dir / f"{task_id}.json"
            if cand.exists():
                artifact_path = cand
        if artifact_path is None or not artifact_path.exists():
            return None
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def _from_db_row(self, task_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        result = self._json_obj(row.get("result"))
        parsed_output = self._json_obj(row.get("parsed_output_json"))
        files_modified = self._json_list(row.get("files_modified_json")) or self._json_list(
            result.get("files_modified")
        )
        usage = self._json_obj(row.get("usage_json"))
        prompt = self._text(row.get("prompt")) or self._text(self._json_obj(row.get("payload")).get("prompt"))
        summary = (
            self._text(row.get("reply_text"))
            or self._summary_from_parsed(parsed_output)
            or self._text(result.get("output"))
        )
        errors = self._json_list(result.get("errors"))
        status = self._text(row.get("status"))
        prior_success = result.get("success")
        if prior_success is None:
            prior_success = status == "completed"
        return {
            "task_id": task_id,
            "source": "db",
            "prompt": prompt[:self.PROMPT_LIMIT],
            "summary": summary[:self.SUMMARY_LIMIT],
            "constraints": {
                "prior_success": bool(prior_success),
                "status": status,
                "error_class": self._text(row.get("error_class")),
                "return_code": row.get("return_code"),
            },
            "files_modified": files_modified,
            "usage": usage,
            "errors": [self._text(e)[:300] for e in errors[:self.ERROR_LIMIT]],
        }

    def _from_artifact(self, task_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        parsed_output = data.get("parsed_output") if isinstance(data.get("parsed_output"), dict) else {}
        summary = self._summary_from_parsed(parsed_output) or self._text(data.get("output"))
        errors = self._json_list(data.get("errors"))
        return {
            "task_id": task_id,
            "source": "artifact",
            "prompt": self._text(data.get("prompt"))[:self.PROMPT_LIMIT],
            "summary": summary[:self.SUMMARY_LIMIT],
            "constraints": {
                "prior_success": bool(data.get("success")),
                "status": self._text(data.get("status")),
                "error_class": self._text(data.get("error_class")),
                "return_code": data.get("return_code"),
            },
            "files_modified": self._json_list(data.get("files_modified")),
            "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
            "errors": [self._text(e)[:300] for e in errors[:self.ERROR_LIMIT]],
        }

    def _summary_from_parsed(self, parsed_output: Dict[str, Any]) -> str:
        content = parsed_output.get("content")
        return content if isinstance(content, str) else ""

    def _json_obj(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                loaded = json.loads(value)
                return loaded if isinstance(loaded, dict) else {}
            except Exception:
                return {}
        return {}

    def _json_list(self, value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            try:
                loaded = json.loads(value)
                return loaded if isinstance(loaded, list) else []
            except Exception:
                return []
        return []

    def _text(self, value: Any) -> str:
        return value if isinstance(value, str) else ""
