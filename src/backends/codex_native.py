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
import threading
import time
import tomllib
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from src.backends.codex_app_server import CodexAppServerClient, CodexProtocolError, CodexRPCError
from src.backends.codex_ownership import CodexOwnership
from src.control.telemetry_sink import NullTelemetrySink
from src.core.interfaces import CodingBackend, ExecutionResult, Session
from src.core.process_utils import ensure_node_on_path
from src.core.telemetry import EMITTER_PROCESS_INSTANCE_ID, TelemetryContext
from src.core.telemetry_adapters.codex import CodexTelemetryAdapter

logger = logging.getLogger(__name__)
MAX_OUTPUT = 8 * 1024 * 1024
MAX_TURN_SECONDS = 36000


class ActiveTurn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    session_id: str
    task_id: str
    thread_id: str = ""
    turn_id: str = ""
    status: str = "starting"
    cancel: threading.Event = Field(default_factory=threading.Event)


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
        with self._runtime_lock:
            thread_id = session.backend_session_id
            if self._client and thread_id in self._loaded:
                self._client.unload(thread_id)
                self._loaded.pop(thread_id, None)

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

    def compact_session(self, session: Session) -> ExecutionResult:
        # Native compaction is a mutation and must use the same ownership path.
        return self._run(session.repo_path, "", session.backend_session_id or None,
                         session.session_id, compact=True)

    def list_models(self) -> list[dict[str, JsonValue]]:
        response = self._runtime().list_models()
        data = response.get("data", [])
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

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
        return config

    def _run(self, cwd: str, message: str, resume_id: str | None, session_key: str | None,
             model: str | None = None, effort: str | None = None,
             telemetry_context: TelemetryContext | None = None, telemetry_sink=None,
             *, compact: bool = False) -> ExecutionResult:
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("codex")
        started = time.monotonic()
        native_id: str = resume_id or ""
        if len(message.encode()) > 1024 * 1024:
            return ExecutionResult(False, "", native_id, errors=["codex_input_too_large"])
        if not self._capacity.acquire(blocking=False):
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
        try:
            with self._lock:
                if key in self._active:
                    raise CodexProtocolError("codex_thread_busy")
                self.prepare_execution(task_id)
                active = ActiveTurn(session_id=key, task_id=task_id,
                                    cancel=self._execution_cancels[task_id])
                self._active[key] = active
            ownership = CodexOwnership()
            workspace = str(Path(cwd or os.getcwd()).resolve(strict=True))
            try:
                native_id = ownership.acquire(key, native_id, workspace)
            except RuntimeError as exc:
                # Durable gateway ownership failures are expected adapter
                # outcomes, not an unclassified implementation exception.
                raise CodexProtocolError(str(exc)) from exc
            if compact and not native_id:
                raise CodexProtocolError("codex_compaction_requires_existing_thread")
            if active.cancel.is_set() or ownership.cancelled(task_id):
                terminal = True
                return ExecutionResult(False, "", native_id, errors=["cancelled"])
            client = self._runtime()
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
                if thread["status"]["type"] != "idle":
                    raise CodexProtocolError("codex_thread_not_idle")
                self._loaded[native_id] = workspace
            elif self._loaded[native_id] != workspace:
                raise CodexProtocolError("codex_workspace_mismatch")
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
                terminal = True
                return ExecutionResult(False, "", native_id, errors=["cancelled"])
            if compact:
                mutation_submitted = True
                release_safe = False
                client.compact(native_id)
            else:
                mutation_submitted = True
                release_safe = False
                response = client.start_turn(native_id, message, workspace, model, effort)
                active.turn_id = response["turn"]["id"]
            active.status = "inProgress"
            interrupted_at: float | None = None
            next_cancel_poll = 0.0
            while True:
                now = time.monotonic()
                if now >= next_cancel_poll:
                    next_cancel_poll = now + 1
                    if ownership.cancelled(task_id):
                        active.cancel.set()
                if now - started > MAX_TURN_SECONDS:
                    active.cancel.set()
                if active.cancel.is_set() and active.turn_id and interrupted_at is None:
                    interrupted_at = now
                    try:
                        client.interrupt(native_id, active.turn_id)
                    except CodexRPCError:
                        pass  # Completion may already be queued; its status wins.
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
                if turn_id and turn_id != active.turn_id:
                    raise CodexProtocolError("codex_turn_identity_mismatch")
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
            # Transport ambiguity must not release a mutation-capable runtime.
            if client and not terminal and (getattr(client, "failure", "")
                    or mutation_submitted and not isinstance(exc, CodexRPCError)):
                client.close()
                release_safe = True
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
