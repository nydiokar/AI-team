"""The one Codex ``CodingBackend`` implementation.

This module owns gateway semantics: Session-to-thread mapping, one active turn
per session, output/usage projection, and the public ``CodingBackend`` methods.
It does not parse stdio frames or implement SQLite claims; those are delegated
to ``codex_app_server`` and ``codex_ownership`` respectively.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from src.backends.codex_app_server import (
    CodexAppServerClient, CodexProtocolError, CodexRPCError, CodexRPCTimeout,
)
from src.backends.codex_ownership import CodexOwnership
from src.control.telemetry_sink import NullTelemetrySink
from src.core.interfaces import CodingBackend, ExecutionResult, Session
from src.core.process_utils import ensure_node_on_path, process_gone_proof, process_identity
from src.core.telemetry import EMITTER_PROCESS_INSTANCE_ID, TelemetryContext
from src.core.telemetry_adapters.codex import CodexTelemetryAdapter

logger = logging.getLogger(__name__)
MAX_OUTPUT = 8 * 1024 * 1024
MAX_TURN_SECONDS = 36000
# [A82 step 4a] The managed caller's deadline. Expiry NEVER interrupts: the turn
# is held for recovery and its eventual reply binds to its turn uuid only.
MANAGED_TURN_SECONDS = MAX_TURN_SECONDS
# Native thread states in which no turn of OURS runs: "notLoaded" (this carrier's
# app-server does not hold it) and "gone" (that app-server process is dead).
_QUIET_STATES = frozenset({"idle", "systemError", "notLoaded", "gone"})
# [A82 step 4 rework, m5] Native states that accept a managed turn, the SAME on
# attach and on an already-loaded thread. ``systemError`` = the thread's last
# turn failed and none is running (probe: status after a failed turn); it is
# recoverable, not busy — refusing it would wedge every session whose last turn
# failed. If native refuses the new turn, that is a visible rejected failure.
_SUBMITTABLE_STATES = frozenset({"idle", "systemError"})
# [A82 step 4 rework, m5] Capability probe cache: binary identity → (supported,
# probed-at). A definitive answer holds for that binary; a failed probe is
# retried after ``_PROBE_RETRY_SEC`` (fail closed meanwhile).
_PROTOCOL_PROBES: dict[tuple[str, int, int], tuple[bool, bool, float]] = {}
_PROBE_RETRY_SEC = 300.0
_PRE_SUBMIT_CONFLICTS = frozenset({"codex_thread_busy", "codex_capacity_exceeded"})
# [A82 pre-cutover, N1] An app-server that leaves a request unanswered this long
# while alive is hung: it is recycled once no live turn runs on it (held turns
# on it then resolve by process-death proof) — never a session wedged until a
# worker restart.
UNRESPONSIVE_AFTER_SEC = 300.0


def _managed_protocol_supported(executable: str, env: dict[str, str]) -> bool:
    """[A82 step 4 rework, m5] The installed app-server speaks the managed
    protocol iff its OWN generated schema lists the ``thread/read`` request and
    ``turn/start``'s ``clientUserMessageId``. Offline: ``codex app-server
    generate-json-schema`` (no app-server session, no model call); cached per
    binary identity (path, mtime, size)."""
    try:
        stat = os.stat(executable)
    except OSError:
        return False
    key = (os.path.realpath(executable), stat.st_mtime_ns, stat.st_size)
    cached = _PROTOCOL_PROBES.get(key)
    if cached is not None and (cached[1] or time.monotonic() - cached[2] < _PROBE_RETRY_SEC):
        return cached[0]
    supported, definitive = False, False
    with tempfile.TemporaryDirectory(prefix="codex-schema-") as out:
        try:
            subprocess.run([executable, "app-server", "generate-json-schema", "--out", out], env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=True)
            requests = (Path(out) / "ClientRequest.json").read_text()
            turn_start = json.loads((Path(out) / "v2" / "TurnStartParams.json").read_text())
            properties = turn_start.get("properties") if isinstance(turn_start, dict) else None
            supported = '"thread/read"' in requests and isinstance(properties, dict) \
                and "clientUserMessageId" in properties
            definitive = True
        except subprocess.CalledProcessError:
            definitive = True  # the CLI has no schema generator: an older app-server
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    _PROTOCOL_PROBES[key] = (supported, definitive, time.monotonic())
    if not supported:
        logger.warning("event=codex_managed_protocol_unsupported definitive=%s", definitive)
    return supported


def _publish_codex_activity(context: TelemetryContext | None, method: object, item: object) -> None:
    """Translate only known app-server item kinds into live activity categories."""
    if context is None or not isinstance(item, dict):
        return
    event_method = method if method in ("item/started", "item/completed") else None
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return
    tool_by_type = {
        "commandExecution": "Bash",
        "fileChange": "Edit",
        "mcpToolCall": "MCP tool",
        "webSearch": "WebSearch",
    }
    if item_type == "agentMessage" and event_method == "item/started":
        category, tool = "writing", None
    elif item_type in tool_by_type and event_method:
        category = "tool_started" if event_method == "item/started" else "tool_completed"
        tool = tool_by_type[item_type]
    else:
        return
    from src.core.activity import publish_activity

    publish_activity(
        session_id=context.session_id,
        task_id=context.turn_id,
        category=category,
        tool=tool,
    )


class ActiveTurn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    session_id: str
    task_id: str
    thread_id: str = ""
    turn_id: str = ""
    status: str = "starting"
    cancel: threading.Event = Field(default_factory=threading.Event)
    turn_uuid: str = ""  # [A82 step 4a] managed attempt identity ("" = legacy)


class _NotSubmitted(Exception):
    """Managed refusal decided BEFORE the prompt reached the app-server."""


class _Unattributable(Exception):
    """Managed outcome that cannot be attributed (recovery required)."""


class _ManagedCall(BaseModel):
    """One managed invocation: the submit/abandon decision is atomic under
    ``lock`` so a deadline either prevents submission or knows it happened."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    turn_uuid: str
    session_key: str = ""
    on_process: Callable[[dict], Any] | None = None
    lock: Any = Field(default_factory=threading.Lock)  # a threading.Lock
    submitted: bool = False
    abandoned: bool = False
    forgotten: bool = False


class _Hold(BaseModel):
    """A submitted managed turn whose outcome was unattributable on a still
    healthy app-server: ownership is retained until native status (or proof
    that app-server is gone) shows the turn is no longer running."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    ownership: CodexOwnership
    client: CodexAppServerClient | None
    thread_id: str
    turn_uuid: str
    native_turn_id: str = ""
    # [A82 step 4 rework, M1] Set when the submitting request (turn/start or
    # compaction) timed out: its late reply lands here and alone decides.
    late_reply: Any = None  # queue.Queue[dict] | None
    late_id: int = 0  # [A82 pre-cutover, N1] that request's JSON-RPC id


class LateManagedOutcome(BaseModel):
    """The real reply of a managed turn that outlived its deadline, delivered to
    the carrier's proactive sink and bound by ``managed_turn_uuid`` only."""
    late_managed: bool = True
    managed_turn_uuid: str
    output: str
    is_error: bool
    error_text: str = ""
    error_class: str = ""
    backend_session_id: str = ""
    raw_ndjson: str = ""


def _managed_conflict(native_id: str, reason: str) -> ExecutionResult:
    return ExecutionResult(False, "", native_id, errors=[f"not_submitted: OwnershipConflictError: {reason}"],
                           error_class="managed_conflict", return_code=1)


def _recovery_required(native_id: str, reason: str) -> ExecutionResult:
    return ExecutionResult(False, "", native_id, errors=[f"RecoveryRequiredError: {reason}"],
                           error_class="recovery_required", return_code=1)


def _resolve_model(session: Session) -> str | None:
    from config.models import resolve_model
    return resolve_model(session)


def _resolve_effort(session: Session) -> str | None:
    from config.models import validate_effort
    return validate_effort("codex", session.effort or os.getenv("CODEX_DEFAULT_EFFORT", "medium"))


class CodexBackend(CodingBackend):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtime_lock = threading.Lock()
        self._client: CodexAppServerClient | None = None
        self._loaded: dict[str, str] = {}
        self._active: dict[str, ActiveTurn] = {}
        self._execution_cancels: dict[str, threading.Event] = {}
        self._capacity = threading.BoundedSemaphore(8)
        # [A82 Stage 5] session_key → current sender capability (memory only),
        # and native thread id → the capability its loaded config carries.
        self._sender_tokens: dict[str, str] = {}
        self._sender_attached: dict[str, str | None] = {}
        # [A82 step 4a] managed turns: live calls by turn uuid, held (recovery)
        # ownerships by session key, armed cancels, and the late-reply sink.
        self._calls: dict[str, _ManagedCall] = {}
        self._held: dict[str, _Hold] = {}
        self._armed: dict[str, None] = {}
        self._proactive_sink: Callable[[str, LateManagedOutcome], Any] | None = None
        # [A82 pre-cutover, N1] session key → (app-server, request id) of a
        # forgotten hold's still-unanswered submission: busy until it is
        # answered or that app-server is gone (recycled when unresponsive).
        self._unanswered: dict[str, tuple[CodexAppServerClient, int]] = {}

    def provision_sender_capability(self, session_id: str, token: str | None) -> bool:
        """[A82 Stage 5] Per-thread sender tool: the next attach of this
        session's thread carries a dedicated stdio server whose OWN env holds
        the capability; a loaded thread whose capability changed is re-attached
        at its next turn. No global env/config.toml mutation."""
        if not session_id:
            return False
        with self._lock:
            if token:
                self._sender_tokens[session_id] = token
            else:
                self._sender_tokens.pop(session_id, None)
        return True

    def _runtime(self) -> CodexAppServerClient:
        with self._runtime_lock:
            if self._client is not None:
                try:
                    self._client.check()
                    return self._client
                except CodexProtocolError:
                    self._client.close()
                    self._loaded.clear()
            env = ensure_node_on_path()
            for key in ("SESSION_ID", "AI_TEAM_SESSION_ID", "AI_TEAM_TURN_ID", "AI_TEAM_INVOCATION_ID"):
                env.pop(key, None)
            executable = shutil.which("codex", path=env.get("PATH")) or "codex"
            client = CodexAppServerClient(executable, env)
            client.start()
            self._client = client
            logger.info("event=codex_app_server_ready pid=%s", client.process.pid)
            return client

    def prepare_execution(self, task_id: str) -> None:
        with self._lock:
            self._execution_cancels.setdefault(task_id, threading.Event())

    def cancel_execution(self, task_id: str) -> None:
        with self._lock:
            event = self._execution_cancels.get(task_id)
            if event is not None:
                event.set()

    def create_session(self, session: Session, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(session.repo_path, session.last_user_message, session.backend_session_id or None,
                         session.session_id, _resolve_model(session), _resolve_effort(session),
                         telemetry_context, telemetry_sink)

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(session.repo_path, message, session.backend_session_id or None,
                         session.session_id, _resolve_model(session), _resolve_effort(session),
                         telemetry_context, telemetry_sink)

    def run_oneoff(self, cwd: str, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(cwd, message, None, None, telemetry_context=telemetry_context,
                         telemetry_sink=telemetry_sink)

    def cancel(self, session: Session) -> None:
        with self._lock:
            active = self._active.get(session.session_id)
            if active:
                active.cancel.set()

    def close(self, session: Session) -> None:
        self.cancel(session)
        with self._lock:
            if session.session_id in self._active:
                return  # The turn's owner unloads after confirmed interruption.
        with self._lock:
            self._sender_tokens.pop(session.session_id, None)  # [A82 Stage 5]
        with self._runtime_lock:
            thread_id = session.backend_session_id
            if self._client and thread_id in self._loaded:
                self._client.unload(thread_id)
                self._loaded.pop(thread_id, None)
                self._sender_attached.pop(thread_id, None)

    def terminate_active_processes(self) -> None:
        """Compatibility spelling for the existing carrier shutdown hook."""
        with self._lock:
            for active in self._active.values():
                active.cancel.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active:
                    break
            time.sleep(0.05)
        with self._runtime_lock:
            if self._client:
                self._client.close()
                self._client = None
            self._loaded.clear()
            self._sender_attached.clear()

    def compact_session(self, session: Session) -> ExecutionResult:
        # Native compaction is a mutation and must use the same ownership path.
        return self._run(session.repo_path, "", session.backend_session_id or None,
                         session.session_id, compact=True)

    def list_models(self) -> list[dict[str, JsonValue]]:
        response = self._runtime().list_models()
        data = response.get("data", [])
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    # ------------------------------------------------------------------ #
    # [A82 step 4a] Managed (protocol-1) turn contract on the native
    # app-server protocol. Correlation: the carrier's turn uuid is written
    # ahead to ``managed_turns`` and submitted as ``clientUserMessageId`` (the
    # native userMessage ``clientId``); the native turn id from the
    # ``turn/start`` response is bound to it. Never interrupts on conflict.
    # ------------------------------------------------------------------ #
    def supports_managed_turns(self) -> bool:
        """Managed execution needs the native app-server binary on this host AND
        that binary's protocol (``thread/read`` + ``clientUserMessageId``),
        probed offline once per binary ([A82 step 4 rework, m5])."""
        env = ensure_node_on_path()
        executable = shutil.which("codex", path=env.get("PATH"))
        return executable is not None and _managed_protocol_supported(executable, env)

    def set_proactive_sink(self, sink: Callable[[str, LateManagedOutcome], Any]) -> None:
        """Carrier sink for late managed replies (bound by turn uuid there)."""
        self._proactive_sink = sink

    def run_managed_turn(self, session: Session, message: str, ownership: Any, *, telemetry_context=None,
                         telemetry_sink=None, on_process=None) -> ExecutionResult:
        return self._run_managed(session, message, ownership, telemetry_context, telemetry_sink,
                                 on_process, compact=False)

    def run_managed_compaction(self, session: Session, ownership: Any, *, telemetry_context=None,
                               telemetry_sink=None, on_process=None) -> ExecutionResult:
        """Native ``thread/compact/start`` as one managed turn (same contract)."""
        return self._run_managed(session, "", ownership, telemetry_context, telemetry_sink,
                                 on_process, compact=True)

    def _run_managed(self, session: Session, message: str, ownership: Any, telemetry_context,
                     telemetry_sink, on_process, *, compact: bool) -> ExecutionResult:
        from src.control.turn_queue import ManagedUnsupportedError, OwnershipConflictError

        if not self.supports_managed_turns():
            raise ManagedUnsupportedError("codex app-server is not available on this carrier",
                                          backend=type(self).__name__)
        if (ownership.session_id or "") != (session.session_id or ""):
            raise OwnershipConflictError("managed ownership does not match the session",
                                         task_id=ownership.task_id)
        turn_uuid: str = getattr(ownership, "turn_uuid", None) or ""
        if not turn_uuid:
            raise OwnershipConflictError("managed Codex turn needs the carrier turn uuid",
                                         task_id=ownership.task_id)
        call = _ManagedCall(turn_uuid=turn_uuid, session_key=session.session_id or "", on_process=on_process)
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                try:
                    result = self._run(session.repo_path, message, session.backend_session_id or None,
                                       session.session_id, _resolve_model(session), _resolve_effort(session),
                                       telemetry_context, telemetry_sink, compact=compact, managed=call)
                except BaseException as exc:  # surfaced to the caller if it still waits
                    with call.lock:
                        box["error"] = exc
                    return
                with call.lock:
                    box["result"] = result
                    late = call.abandoned and not call.forgotten
                if late and result.error_class not in ("recovery_required", "managed_conflict"):
                    self._deliver_late(session.session_id, turn_uuid, result)
            finally:
                # [A82 step 4 rework, m3] Popped only AFTER the late-delivery
                # attempt: until then an abandoned call keeps its session
                # non-quiescent (reconcile must not resolve a reply in flight).
                with self._lock:
                    if self._calls.get(turn_uuid) is call:
                        self._calls.pop(turn_uuid, None)

        with self._lock:
            self._calls[turn_uuid] = call
        worker = threading.Thread(target=target, name="codex-managed-turn", daemon=True)
        worker.start()
        worker.join(MANAGED_TURN_SECONDS)
        with call.lock:
            if "error" in box:
                raise box["error"]
            if "result" in box:
                return box["result"]
            call.abandoned = True
            submitted = call.submitted
        native_id = session.backend_session_id or ""
        if not submitted:
            return _managed_conflict(native_id, "managed turn deadline expired before submission")
        logger.warning("event=codex_managed_turn_deadline turn_uuid=%s — not interrupted; held", turn_uuid)
        return _recovery_required(native_id, "managed turn exceeded its deadline without a terminal "
                                             "result; backend not interrupted")

    def _deliver_late(self, session_id: str, turn_uuid: str, result: ExecutionResult) -> None:
        sink = self._proactive_sink
        if sink is None:
            logger.warning("event=codex_managed_late_result_dropped turn_uuid=%s", turn_uuid)
            return
        outcome = LateManagedOutcome(
            managed_turn_uuid=turn_uuid, output=result.output or "", is_error=not result.success,
            error_text="; ".join(result.errors or []), error_class=result.error_class or "",
            backend_session_id=result.backend_session_id or "", raw_ndjson=result.raw_stdout or "")
        try:
            sink(session_id, outcome)
        except Exception:
            logger.warning("event=codex_managed_late_result_sink_failed turn_uuid=%s", turn_uuid)

    def cancel_managed_turn(self, session: Session, turn_uuid: str) -> bool:
        """Cancel EXACTLY ``turn_uuid``: arm it (a not-yet-submitted turn is then
        never submitted) and signal its live run, whose loop interrupts only
        its own native turn id (at once, or when ``turn/start`` answers)."""
        if not turn_uuid:
            return False
        with self._lock:
            self._armed[turn_uuid] = None
            while len(self._armed) > 256:
                self._armed.pop(next(iter(self._armed)))
            for active in self._active.values():
                if active.turn_uuid == turn_uuid:
                    active.cancel.set()
            hold = next((h for h in self._held.values() if h.turn_uuid == turn_uuid), None)
        if hold is not None and hold.client is not None and hold.native_turn_id:
            try:
                hold.client.interrupt(hold.thread_id, hold.native_turn_id)
            except CodexProtocolError:
                pass  # Completion may already be queued; native status settles the hold.
        return True

    def forget_managed_turn(self, session: Session, turn_uuid: str) -> bool:
        """Drop the wait for ``turn_uuid`` (its row is terminal server-side): no
        late delivery. Quiescence still follows native truth — a turn that is
        still running natively keeps the session busy until it ends."""
        removed = False
        with self._lock:
            call = self._calls.get(turn_uuid)
            if call is not None:
                with call.lock:
                    call.forgotten = True
                removed = True
            key = next((k for k, h in self._held.items() if turn_uuid and h.turn_uuid == turn_uuid), None)
            hold = self._held.pop(key) if key is not None else None
        if turn_uuid and CodexOwnership().finish_managed(turn_uuid, "forgotten"):
            removed = True
        if hold is not None:
            # [A82 pre-cutover, N1] Drop the hold and its late route; a still-
            # unanswered submission keeps the session busy (it may yet be
            # accepted) until answered or its app-server is gone/recycled.
            removed = True
            if hold.late_reply is not None and hold.client is not None and hold.late_id \
                    and hold.client.forget_late(hold.late_id):
                with self._lock:
                    self._unanswered[key] = (hold.client, hold.late_id)
            hold.ownership.release()
        return removed

    def _recycle_unresponsive(self) -> None:
        """[A82 pre-cutover, N1] Close every app-server this backend still
        depends on that left a request unanswered past ``UNRESPONSIVE_AFTER_SEC``
        — but never one with a live run (``_active``) on it. Its process-group
        death is then the proof that settles every turn it held."""
        with self._lock:
            candidates = [self._client, *(h.client for h in self._held.values()),
                          *(c for c, _ in self._unanswered.values())]
        seen: set[int] = set()
        for client in candidates:
            if client is None or id(client) in seen or not client.unresponsive(UNRESPONSIVE_AFTER_SEC):
                continue
            seen.add(id(client))
            with self._runtime_lock, self._lock:
                if client is self._client:
                    if self._active:
                        logger.warning("event=codex_app_server_unresponsive_not_recycled reason=live_turn")
                        continue
                    self._client = None
                    self._loaded.clear()
                    self._sender_attached.clear()
            process = client.process
            logger.warning("event=codex_app_server_unresponsive_recycled pid=%s",
                           process.pid if process is not None else None)
            try:
                client.close()
            except CodexProtocolError:
                logger.warning("event=codex_app_server_recycle_incomplete")

    def is_quiescent(self, session: Session) -> bool:
        """No native work for ``session``: no live run here, no held turn still
        running natively, no other owner whose app-server is not provably gone,
        and the native thread status (on this carrier's app-server) is not
        active. Anything unknown ⇒ False."""
        key = session.session_id
        if not key:
            return False
        with self._lock:
            if key in self._active:
                return False
            if any(call.abandoned and call.session_key == key for call in self._calls.values()):
                return False  # a late reply is still being delivered
        self._recycle_unresponsive()
        with self._lock:
            unanswered = self._unanswered.get(key)
        if unanswered is not None:
            client, request_id = unanswered
            process = client.process
            if process is not None and process.poll() is None and client.outstanding(request_id):
                return False  # [N1] a forgotten submission may still be accepted
            with self._lock:
                if self._unanswered.get(key) == unanswered:
                    self._unanswered.pop(key, None)
        if not self._settle_hold(key):
            return False
        ownership = CodexOwnership()
        thread_id = session.backend_session_id or ownership.thread_for(key)
        if not ownership.clear_dead_owners(key, thread_id, process_gone_proof):
            return False
        return not thread_id or self._native_status(self._client, thread_id) in _QUIET_STATES

    def _native_status(self, client: CodexAppServerClient | None, thread_id: str) -> str | None:
        """Native status of ``thread_id`` on ``client``'s app-server, "gone" when
        that process is dead, "notLoaded" when it does not hold the thread,
        None when unknown."""
        with self._runtime_lock:
            current = client is not None and client is self._client
            loaded = current and thread_id in self._loaded
        if client is None:
            return "notLoaded"
        process = client.process
        if process is None or process.poll() is not None:
            return "gone"
        if current and not loaded:
            return "notLoaded"
        try:
            client.check()
            status = client.read_thread(thread_id)["thread"]["status"]["type"]
        except (CodexProtocolError, KeyError, TypeError):
            return None
        return status if isinstance(status, str) else None

    def _settle_hold(self, key: str) -> bool:
        """True iff no held managed turn remains for ``key`` (a hold whose turn
        is provably no longer running is released here)."""
        with self._lock:
            hold = self._held.get(key)
        if hold is None:
            return True
        state = "stopped"
        if hold.late_reply is not None:
            # [A82 step 4 rework, M1] The submitting request timed out: until its
            # late reply arrives the prompt may still be accepted (a thread/read
            # "idle" cannot prove otherwise). Only that reply — the native turn
            # id of the request carrying our clientUserMessageId — or the death
            # of that app-server decides.
            try:
                reply = hold.late_reply.get_nowait()
            except queue.Empty:
                process = hold.client.process if hold.client is not None else None
                if process is not None and process.poll() is None:
                    return False
                reply = None
            if reply is not None:
                hold.late_reply = None
                result = reply.get("result")
                turn = result.get("turn") if isinstance(result, dict) else None
                if "error" in reply:
                    state = "rejected"  # native refused the prompt: nothing of ours ran
                elif isinstance(turn, dict) and isinstance(turn.get("id"), str):
                    hold.native_turn_id = turn["id"]
                    if hold.turn_uuid:
                        hold.ownership.bind_native_turn(hold.turn_uuid, turn["id"])
        if state == "stopped" and self._native_status(hold.client, hold.thread_id) not in _QUIET_STATES:
            return False
        hold.ownership.finish_managed(hold.turn_uuid, state)
        hold.ownership.release()
        with self._lock:
            if self._held.get(key) is hold:
                self._held.pop(key, None)
        return True

    def _managed_submit_gate(self, call: _ManagedCall, ownership: CodexOwnership, active: ActiveTurn,
                             thread_id: str, identity: dict, compact: bool) -> None:
        """Last step before the prompt reaches the app-server: report the
        app-server identity, write the turn uuid ahead durably, then decide
        submit-vs-refuse atomically with the caller's deadline and any cancel."""
        if call.on_process is not None:
            call.on_process(dict(identity))
        try:
            ownership.begin_managed(call.turn_uuid, thread_id, "compaction" if compact else "turn", identity)
        except RuntimeError as exc:
            # An earlier life may already have submitted this exact attempt.
            raise _Unattributable(f"{exc}: never re-submitted") from exc
        with self._lock:
            armed = self._armed.pop(call.turn_uuid, "absent") != "absent"
        with call.lock:
            refused = call.abandoned or armed or active.cancel.is_set()
            call.submitted = not refused
        if refused:
            ownership.finish_managed(call.turn_uuid, "not_submitted")
            raise _NotSubmitted("cancelled or abandoned before submission")

    def _managed_failure(self, exc: Exception, call: _ManagedCall, ownership: CodexOwnership | None,
                         client: CodexAppServerClient | None, active: ActiveTurn | None, native_id: str,
                         submitted: bool, terminal: bool) -> tuple[ExecutionResult | None, bool]:
        """Classify a managed failure → (typed result, or None for the generic
        failure projection; whether ownership is HELD for recovery)."""
        if isinstance(exc, _NotSubmitted) or (not submitted and (
                str(exc) in _PRE_SUBMIT_CONFLICTS or isinstance(exc, CodexRPCTimeout))):
            # [A82 pre-cutover, N2] A pre-submit deadline (thread/start|resume,
            # status read) proves nothing of ours ran: requeue, never a failure.
            return _managed_conflict(native_id, str(exc)), False
        if isinstance(exc, _Unattributable):
            return _recovery_required(native_id, str(exc)), False
        if not submitted or terminal or ownership is None:
            return None, False  # nothing of ours ran / it already ended: attributable
        if isinstance(exc, CodexRPCError):
            ownership.finish_managed(call.turn_uuid, "rejected")  # native refused the prompt
            return None, False
        if client is not None and client.failure:
            # The transport is already lost; closing makes the stop provable
            # (the shared app-server process is gone ⇒ every turn it ran stopped).
            try:
                client.close()
            except CodexProtocolError:
                logger.warning("event=codex_managed_runtime_close_incomplete")
            ownership.finish_managed(call.turn_uuid, "stopped")
            if active is not None and active.turn_id:
                return None, False  # our known turn died with its process
            return _recovery_required(native_id, f"{exc}: prompt acceptance unknown"), False
        return _recovery_required(native_id, f"{exc}: outcome not attributable; not interrupted"), True

    def _thread_config(self, session_id: str) -> dict[str, JsonValue]:
        identity: dict[str, JsonValue] = {"SESSION_ID": session_id, "AI_TEAM_SESSION_ID": session_id}
        config: dict[str, JsonValue] = {"shell_environment_policy.set": identity}
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        path = root / "config.toml"
        if path.exists():
            configured = tomllib.loads(path.read_text())
            for name, server in configured.get("mcp_servers", {}).items():
                if "command" in server:
                    config[f"mcp_servers.{name}.env"] = {**server.get("env", {}), **identity}
        token = self._sender_tokens.get(session_id)
        if token:
            from src.control.agent_sender import SENDER_SERVER_NAME, codex_sender_server, sender_base_url

            server_def = codex_sender_server(token, sender_base_url(os.environ))
            for field in ("command", "args", "env"):
                config[f"mcp_servers.{SENDER_SERVER_NAME}.{field}"] = server_def[field]
        return config

    def _run(self, cwd: str, message: str, resume_id: str | None, session_key: str | None,
             model: str | None = None, effort: str | None = None,
             telemetry_context: TelemetryContext | None = None, telemetry_sink=None,
             *, compact: bool = False, managed: _ManagedCall | None = None) -> ExecutionResult:
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("codex")
        started = time.monotonic()
        native_id: str = resume_id or ""
        if len(message.encode()) > 1024 * 1024:
            return ExecutionResult(False, "", native_id, errors=["codex_input_too_large"])
        if not self._capacity.acquire(blocking=False):
            if managed is not None:
                return _managed_conflict(native_id, "codex_capacity_exceeded")
            return ExecutionResult(False, "", native_id, errors=["codex_capacity_exceeded"])
        task_id = telemetry_context.turn_id if telemetry_context else uuid.uuid4().hex
        key = session_key or f"oneoff-{task_id}"
        sink = telemetry_sink or NullTelemetrySink()
        adapter = CodexTelemetryAdapter(telemetry_context,
                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID) if telemetry_context else None
        ownership: CodexOwnership | None = None
        client: CodexAppServerClient | None = None
        active: ActiveTurn | None = None
        terminal = False
        mutation_submitted = False
        release_safe = True
        output: dict[str, str] = {}
        output_size = 0
        usage: dict[str, int] = {}
        seen_usage: set[str] = set()
        diagnostic: dict[str, JsonValue] = {}
        file_changes: dict[str, dict] = {}
        identity: dict = {}
        awaiting_reply: queue.Queue[dict] | None = None
        awaiting_id = 0
        try:
            with self._lock:
                if key in self._active:
                    raise CodexProtocolError("codex_thread_busy")
                self.prepare_execution(task_id)
                active = ActiveTurn(session_id=key, task_id=task_id,
                                    cancel=self._execution_cancels[task_id],
                                    turn_uuid=managed.turn_uuid if managed else "")
                self._active[key] = active
            ownership = CodexOwnership()
            workspace = str(Path(cwd or os.getcwd()).resolve(strict=True))
            if managed is not None:
                # [A82 step 4a] The app-server that will run the turn is known
                # BEFORE ownership: its identity is the only basis on which a
                # successor may clear this owner's no-TTL rows.
                client = self._runtime()
                identity = process_identity(client.process.pid)
                if not self._settle_hold(key):
                    raise _NotSubmitted("codex_managed_turn_held")
            elif not self._settle_hold(key):
                raise CodexProtocolError("codex_thread_busy")  # [M1] a held legacy turn still runs
            try:
                try:
                    native_id = ownership.acquire(key, native_id, workspace, process=identity or None)
                except RuntimeError as exc:
                    if not (managed is not None and str(exc).startswith("codex_thread_busy")
                            and ownership.clear_dead_owners(key, ownership.thread_id or native_id,
                                                            process_gone_proof)):
                        raise
                    native_id = ownership.acquire(key, native_id, workspace, process=identity)
            except RuntimeError as exc:
                # Durable gateway ownership failures are expected adapter
                # outcomes, not an unclassified implementation exception.
                if managed is not None and str(exc).startswith("codex_thread_busy"):
                    raise _NotSubmitted("codex_thread_busy") from exc
                raise CodexProtocolError(str(exc)) from exc
            if compact and not native_id:
                raise CodexProtocolError("codex_compaction_requires_existing_thread")
            if active.cancel.is_set() or ownership.cancelled(task_id):
                if managed is not None:
                    raise _NotSubmitted("cancelled before submission")
                terminal = True
                return ExecutionResult(False, "", native_id, errors=["cancelled"])
            client = client if managed is not None else self._runtime()
            if native_id in self._loaded and self._sender_attached.get(native_id) != self._sender_tokens.get(key):
                # [A82 Stage 5] The session's sender capability changed: re-attach
                # (no turn is active for this key) so its tool config is current.
                client.unload(native_id)
                self._loaded.pop(native_id, None)
            if native_id not in self._loaded:
                response = client.attach_thread(native_id, workspace, model, self._thread_config(key))
                thread = response["thread"]
                returned_id = thread["id"]
                if native_id and returned_id != native_id:
                    raise CodexProtocolError("codex_thread_identity_mismatch")
                native_id = returned_id
                ownership.record_thread(native_id)
                if str(Path(thread["cwd"]).resolve()) != workspace:
                    raise CodexProtocolError("codex_workspace_mismatch")
                if thread["status"]["type"] not in (_SUBMITTABLE_STATES if managed else ("idle",)):
                    raise _NotSubmitted("codex_thread_not_idle") if managed else CodexProtocolError(
                        "codex_thread_not_idle")
                self._loaded[native_id] = workspace
                self._sender_attached[native_id] = self._sender_tokens.get(key)
            elif self._loaded[native_id] != workspace:
                raise CodexProtocolError("codex_workspace_mismatch")
            elif managed is not None:
                # Never submit onto native work: only an idle (or last-turn
                # errored) thread on this app-server accepts a managed turn.
                status = self._native_status(client, native_id)
                if status not in _SUBMITTABLE_STATES:
                    raise _NotSubmitted(f"codex_thread_not_idle:{status}")
            active.thread_id = native_id
            channel = client.subscribe(native_id)
            def emit(events: list) -> None:
                try:
                    sink.emit_many(events)
                except Exception:
                    logger.warning("event=codex_telemetry_emit_failed")
            if adapter:
                emit(adapter.coverage_events())
            if active.cancel.is_set() or ownership.cancelled(task_id):
                if managed is not None:
                    raise _NotSubmitted("cancelled before submission")
                terminal = True
                return ExecutionResult(False, "", native_id, errors=["cancelled"])
            if managed is not None:
                self._managed_submit_gate(managed, ownership, active, native_id, identity, compact)
            mutation_submitted = True
            release_safe = False
            start_reply: queue.Queue[dict] = queue.Queue(1)
            try:
                if compact:
                    client.compact(native_id, on_late=start_reply.put_nowait)
                else:
                    response = client.start_turn(native_id, message, workspace, model, effort,
                                                 managed.turn_uuid if managed else None,
                                                 on_late=start_reply.put_nowait)
            except CodexRPCTimeout as exc:
                awaiting_reply = start_reply  # [M1] the prompt may still be accepted, late
                awaiting_id = exc.request_id
                raise
            if not compact:
                active.turn_id = response["turn"]["id"]
                if managed is not None:
                    ownership.bind_native_turn(managed.turn_uuid, active.turn_id)
            active.status = "inProgress"
            interrupted_at: float | None = None
            next_cancel_poll = 0.0
            while True:
                now = time.monotonic()
                if now >= next_cancel_poll:
                    next_cancel_poll = now + 1
                    if ownership.cancelled(task_id):
                        active.cancel.set()
                if now - started > MAX_TURN_SECONDS and managed is None:
                    active.cancel.set()  # Managed: the caller's deadline holds; never interrupts.
                if active.cancel.is_set() and active.turn_id and interrupted_at is None:
                    interrupted_at = now
                    try:
                        client.interrupt(native_id, active.turn_id)
                    except (CodexRPCError, CodexRPCTimeout):
                        # Completion may already be queued; its status wins. A
                        # slow interrupt never fails the shared app-server: the
                        # confirmation deadline below decides (hold, never kill).
                        pass
                if interrupted_at is not None and now - interrupted_at > 10:
                    raise CodexProtocolError("codex_interrupt_unconfirmed")
                try:
                    event = channel.get()
                except queue.Empty:
                    continue
                method = event["method"]
                params = event["params"]
                turn_id = params.get("turnId")
                if method in ("turn/started", "turn/completed"):
                    turn_id = params["turn"]["id"]
                    if compact and not active.turn_id and method == "turn/started":
                        active.turn_id = turn_id
                        if managed is not None:
                            ownership.bind_native_turn(managed.turn_uuid, turn_id)
                if turn_id and turn_id != active.turn_id:
                    raise CodexProtocolError("codex_turn_identity_mismatch")
                if (managed is not None and method == "item/started"
                        and params["item"].get("type") == "userMessage"
                        and params["item"].get("clientId") not in (None, managed.turn_uuid)):
                    raise CodexProtocolError("codex_turn_identity_mismatch")  # not our prompt
                if method == "thread/tokenUsage/updated":
                    native_usage = params["tokenUsage"]
                    fingerprint = json.dumps(native_usage["total"], sort_keys=True)
                    if fingerprint not in seen_usage:
                        seen_usage.add(fingerprint)
                        mapping = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                                   "cachedInputTokens": "cached_input_tokens",
                                   "reasoningOutputTokens": "reasoning_output_tokens",
                                   "totalTokens": "total_tokens"}
                        last = {v: native_usage["last"][k] for k, v in mapping.items()}
                        for name, count in last.items():
                            usage[name] = usage.get(name, 0) + count
                        if adapter:
                            emit(adapter.consume_token_count({"info": {
                                "last_token_usage": last,
                                "total_token_usage": {v: native_usage["total"][k] for k, v in mapping.items()},
                                "model_context_window": native_usage.get("modelContextWindow")}}))
                if method in ("item/started", "item/completed"):
                    item = params["item"]
                    _publish_codex_activity(telemetry_context, method, item)
                    if method == "item/completed" and item["type"] == "agentMessage":
                        text = item["text"]
                        output_size += len(text.encode())
                        if output_size > MAX_OUTPUT:
                            raise CodexProtocolError("codex_output_limit_exceeded")
                        output[item["id"]] = text
                    if item["type"] == "fileChange" and method == "item/completed":
                        for change in item["changes"]:
                            file_changes[change["path"]] = {"path": change["path"], "kind": change["kind"]}
                    if adapter:
                        mapping = {"commandExecution": "command_execution", "fileChange": "file_change",
                                   "mcpToolCall": "mcp_tool_call", "webSearch": "web_search"}
                        translated = {**item, "type": mapping.get(item["type"], item["type"])}
                        emit(adapter.consume_line(json.dumps({
                            "type": method.replace("/", "."), "item": translated})))
                if method == "turn/completed":
                    turn = params["turn"]
                    if turn["status"] not in ("completed", "interrupted", "failed"):
                        raise CodexProtocolError("codex_nonterminal_completion")
                    terminal = True
                    release_safe = True
                    active.status = turn["status"]
                    if managed is not None:
                        ownership.finish_managed(managed.turn_uuid, active.status)
                    diagnostic = {"status": active.status, "error": turn.get("error"), "usage": usage}
                    errors = [] if active.status == "completed" else [
                        "cancelled" if active.status == "interrupted" else "codex_turn_failed"]
                    # Existing consumers accept this generic result projection. No
                    # RPC objects or native turn IDs escape through core fields.
                    projection = {"type": "turn.completed", "usage": usage,
                                  "output": "\n".join(output.values()), **diagnostic}
                    result = ExecutionResult(active.status == "completed", projection["output"], native_id,
                        files_modified=list(file_changes), errors=errors,
                        execution_time=time.monotonic() - started,
                        parsed_output=projection, raw_stdout=json.dumps(projection),
                        return_code=0 if active.status == "completed" else 1,
                        file_changes=list(file_changes.values()))
                    return result
        except Exception as exc:
            if isinstance(exc, CodexRPCError):
                diagnostic = {"error": exc.error}
            else:
                diagnostic = {"error": str(exc)}
            if managed is not None:
                outcome, hold = self._managed_failure(exc, managed, ownership, client, active, native_id,
                                                      mutation_submitted, terminal)
                if hold and ownership is not None and active is not None:
                    release_safe = False
                    with self._lock:
                        self._held[key] = _Hold(ownership=ownership, client=client, thread_id=native_id,
                                                turn_uuid=managed.turn_uuid, native_turn_id=active.turn_id,
                                                late_reply=awaiting_reply, late_id=awaiting_id)
                    return outcome
                if outcome is not None:
                    release_safe = True  # nothing of ours runs (never submitted / provably stopped)
                    return outcome
            # Transport ambiguity must not release a mutation-capable runtime.
            if client and not terminal and getattr(client, "failure", ""):
                client.close()  # transport already lost: closing makes the stop provable
                release_safe = True
            elif (client and not terminal and mutation_submitted and not isinstance(exc, CodexRPCError)
                    and ownership is not None and active is not None):
                # [A82 step 4 rework, M1] Healthy SHARED app-server, ambiguous
                # outcome: never kill it (that stops every other thread's turn).
                # Ownership is held until native status shows this thread quiet.
                release_safe = False
                with self._lock:
                    self._held[key] = _Hold(ownership=ownership, client=client, thread_id=native_id,
                                            turn_uuid="", native_turn_id=active.turn_id,
                                            late_reply=awaiting_reply, late_id=awaiting_id)
            elif isinstance(exc, CodexRPCError):
                release_safe = True
            return ExecutionResult(False, "\n".join(output.values()), native_id,
                errors=[str(exc) if isinstance(exc, CodexProtocolError) else "codex_adapter_failed"],
                parsed_output=diagnostic, execution_time=time.monotonic() - started, return_code=1)
        finally:
            if client and native_id:
                client.unsubscribe(native_id)
            if ownership and release_safe:
                ownership.release()
            with self._lock:
                if active and self._active.get(key) is active:
                    self._active.pop(key, None)
                    self._execution_cancels.pop(task_id, None)
            self._capacity.release()
            try:
                sink.flush()
            except Exception:
                logger.warning("event=codex_telemetry_flush_failed")
