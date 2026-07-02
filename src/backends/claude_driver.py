"""Claude driver boundary -- replaces the per-turn spawn pattern."""

# Phase 0 viability findings (2026-06-30)
#
# Auth:      ~/.claude.json shows billingType=stripe_subscription (Pro OAuth).
#            claude-code-sdk v0.0.25 uses the local 'claude' binary, which
#            inherits that OAuth session -- no API key required. Auth-compatible.
#
# SDK:       claude-agent-sdk uses --input-format stream-json to keep ONE claude
#            process alive and receive multiple user messages via ClaudeSDKClient.
#            This is the proven continuous-session mechanism. WINNER.
#            (Package was renamed claude-code-sdk -> claude-agent-sdk at v0.1.0;
#             ClaudeCodeOptions -> ClaudeAgentOptions.)
#
# Background: claude --bg + claude agents --json lets you *launch* but there is
#             no CLI mechanism to send follow-up prompts into the bg session from
#             outside. Log-reading only. Not viable as a multi-turn driver.
#
# RemoteControl: --remote-control exists but no documented programmatic protocol
#             was discoverable. Not viable without reverse-engineering.
#
# Winner: ClaudeSDKClientDriver using claude-code-sdk 0.0.25.
#         Fallback: ClaudePrintResumeDriver (existing behavior).
#
# Async note: The SDK is async (anyio). The existing CodingBackend protocol is
# sync (called via asyncio.to_thread). ClaudeSDKClientDriver therefore runs
# its own asyncio event loop inside the to_thread call using asyncio.run(),
# which is correct because to_thread runs in a threadpool worker with no
# running event loop in the thread.

import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.interfaces import ExecutionResult, Session
from src.core.process_utils import terminate_many_popen

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_DEFAULT_TOOLS = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]

# Cache health thresholds
_CACHE_UNHEALTHY_CREATION_THRESHOLD = 50_000
_CACHE_UNHEALTHY_HIT_RATIO_THRESHOLD = 0.2


# ---------------------------------------------------------------------------
# Cache health helpers
# ---------------------------------------------------------------------------

@dataclass
class CacheStats:
    cache_read: int = 0
    cache_creation: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def hit_ratio(self) -> float:
        total = self.cache_read + self.cache_creation
        if total == 0:
            return 1.0
        return self.cache_read / total

    @property
    def is_unhealthy(self) -> bool:
        return (
            self.cache_creation > _CACHE_UNHEALTHY_CREATION_THRESHOLD
            and self.hit_ratio < _CACHE_UNHEALTHY_HIT_RATIO_THRESHOLD
        )


def _plain_usage_dict(usage: Any) -> Optional[Dict[str, Any]]:
    """Return SDK usage as a plain dict, across SDK versions."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        data = usage.model_dump()
        return data if isinstance(data, dict) else None
    if is_dataclass(usage):
        data = asdict(usage)
        return data if isinstance(data, dict) else None
    if hasattr(usage, "__dict__"):
        data = vars(usage)
        return data if isinstance(data, dict) else None
    return None


def parse_cache_stats_from_ndjson(raw_stdout: str) -> Optional[CacheStats]:
    """Extract first usage stats from NDJSON stream output."""
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        # Usage is in assistant message or result
        usage = None
        if d.get("type") == "assistant":
            msg = d.get("message", {})
            usage = msg.get("usage") if isinstance(msg, dict) else None
        elif d.get("type") == "result":
            usage = d.get("usage")
        if usage and isinstance(usage, dict):
            return CacheStats(
                cache_read=int(usage.get("cache_read_input_tokens", 0)),
                cache_creation=int(usage.get("cache_creation_input_tokens", 0)),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            )
    return None


# ---------------------------------------------------------------------------
# Driver ABC
# ---------------------------------------------------------------------------

class ClaudeDriver(ABC):
    """Narrow interface all Claude execution drivers must implement."""

    @abstractmethod
    def start_session(
        self,
        session: Session,
        message: str,
        *,
        model: Optional[str] = None,
        telemetry_context: Any = None,
        proc_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult: ...

    @abstractmethod
    def send_turn(
        self,
        session: Session,
        message: str,
        *,
        model: Optional[str] = None,
        telemetry_context: Any = None,
        proc_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult: ...

    @abstractmethod
    def cancel(self, session: Session) -> None: ...

    @abstractmethod
    def close(self, session: Session) -> None: ...

    def driver_type(self) -> str:
        return type(self).__name__


# ---------------------------------------------------------------------------
# ClaudeSDKClientDriver  (primary continuous driver)
# ---------------------------------------------------------------------------

def _mcp_jobs_configured() -> bool:
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        return "jobs" in cfg.get("mcpServers", {})
    except Exception:
        return False


class _SDKSession:
    """Holds the live async SDK client and its dedicated asyncio event loop,
    both running in a background daemon thread.

    Lifecycle:
      - Created when start_session() is first called.
      - Stays alive until close() or cancel() is called.
      - send_turn() submits work to the background thread.
    """

    # The SDK's own control-protocol handshake (query.initialize) has a 60s
    # floor. Wait comfortably past that or a legitimately-in-progress connect()
    # reads as a false failure -- the subprocess keeps running on the worker
    # while the gateway reports "failed to connect", orphaning the live task.
    _CONNECT_TIMEOUT_SEC = 90

    def __init__(self, session_key: str, cwd: str, model: Optional[str], proc_env: Dict[str, str]):
        self.session_key = session_key
        self.cwd = cwd
        self.model = model
        self.proc_env = proc_env
        self.backend_session_id: str = ""
        self._lock = threading.Lock()  # serialises concurrent send_turn calls
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None  # ClaudeSDKClient
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[Exception] = None
        self._closed = False

    def start(self) -> None:
        """Boot the background event loop and SDK client."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=f"claude-sdk-{self.session_key[:8]}")
        self._thread.start()
        if not self._ready.wait(timeout=self._CONNECT_TIMEOUT_SEC):
            self._closed = True
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(lambda: None)
            raise TimeoutError(
                f"SDK session failed to connect within {self._CONNECT_TIMEOUT_SEC}s"
            )
        if self._error:
            raise self._error

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_run())
        except Exception as e:
            self._error = e
        finally:
            self._loop.close()

    async def _async_run(self) -> None:
        try:
            from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        except ImportError:
            self._error = ImportError(
                "claude-agent-sdk is not installed. Run: pip install claude-agent-sdk"
            )
            self._ready.set()
            return

        tools = list(_DEFAULT_TOOLS)
        if _mcp_jobs_configured():
            tools.append("mcp__jobs__watch_job")

        options = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=tools,
            permission_mode="bypassPermissions",
            env={k: v for k, v in self.proc_env.items() if k not in os.environ},
            **({"model": self.model} if self.model else {}),
        )

        self._client = ClaudeSDKClient(options=options)
        try:
            await self._client.connect()
        except Exception as e:
            # Surface the REAL connect failure (missing CLI, auth, initialize
            # error) instead of masking it behind start()'s generic timeout.
            self._error = e
            self._ready.set()
            try:
                await self._client.disconnect()
            except Exception:
                pass
            return
        self._ready.set()
        try:
            # Keep the event loop alive; work arrives via run_coroutine_threadsafe
            # We run a simple await loop so the loop stays spinning until close.
            while not self._closed:
                await asyncio.sleep(0.5)
        finally:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    def submit(self, coro, timeout: Optional[float] = None) -> Any:
        """Run coro on the SDK event loop from any thread and return result.

        timeout=None means wait indefinitely (correct for long-running tasks).
        timeout=N kills the wait after N seconds and raises TimeoutError.
        """
        if not self._loop or self._closed:
            raise RuntimeError("SDK session loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except Exception:
            fut.cancel()  # don't leave the coroutine dangling on the loop thread
            raise

    async def _do_query(self, message: str) -> Tuple[str, str, str]:
        """Run one turn and return (output, backend_session_id, raw_ndjson).

        The raw_ndjson is synthesised from the SDK's typed message objects
        (which expose `usage` and `session_id` fields directly) into the same
        NDJSON shape that parse_cache_stats_from_ndjson expects. The SDK does
        NOT expose the underlying stream-json line, so we reconstruct only the
        fields the cache-health detector reads.
        """
        if self._client is None:
            raise RuntimeError("SDK client not initialised")

        # session_id here is the SDK's *internal* conversation-thread selector,
        # not the gateway session id; one _SDKSession owns one claude process,
        # so the default thread is correct. Do not pass the gateway key.
        await self._client.query(message)

        parts: List[str] = []
        backend_session_id = self.backend_session_id
        ndjson_lines: List[str] = []

        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                usage = _plain_usage_dict(getattr(msg, "usage", None))
                if usage is not None:
                    ndjson_lines.append(json.dumps({"type": "assistant", "message": {"usage": usage}}))
                sid = getattr(msg, "session_id", None) or ""
                if sid:
                    backend_session_id = sid
                    self.backend_session_id = sid
            elif isinstance(msg, ResultMessage):
                sid = getattr(msg, "session_id", "") or ""
                if sid:
                    backend_session_id = sid
                    self.backend_session_id = sid
                usage = _plain_usage_dict(getattr(msg, "usage", None))
                if usage is not None:
                    ndjson_lines.append(json.dumps({"type": "result", "usage": usage}))

        return "".join(parts).strip(), backend_session_id, "\n".join(ndjson_lines)

    def send(self, message: str) -> Tuple[str, str, str]:
        # sdk_turn_timeout_sec is the total deadline for one turn (send → full response).
        # 0 means no limit. Default 36000 (10 h) — distinct from inactivity_timeout_sec
        # which is a per-stdout-line timeout used only by the PrintResume driver.
        timeout: Optional[float] = 36000.0
        try:
            from config import config as _cfg
            raw = getattr(_cfg.system, "sdk_turn_timeout_sec", 36000)
            timeout = None if int(raw) == 0 else float(max(60, int(raw)))
        except Exception:
            pass
        with self._lock:
            return self.submit(self._do_query(message), timeout=timeout)

    def close(self) -> None:
        self._closed = True
        # Poke the event loop so the await sleep(0.5) unblocks
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)


class ClaudeSDKClientDriver(ClaudeDriver):
    """Continuous multi-turn driver backed by claude-code-sdk.

    One _SDKSession per gateway session. The SDK client keeps a single claude
    process alive using --input-format stream-json and receives multiple turns.
    """

    def __init__(self):
        self._sessions: Dict[str, _SDKSession] = {}
        self._lock = threading.Lock()

    def _get_or_create(
        self,
        session: Session,
        model: Optional[str],
        proc_env: Dict[str, str],
    ) -> _SDKSession:
        key = session.session_id
        with self._lock:
            if key not in self._sessions:
                sdk_sess = _SDKSession(key, session.repo_path, model, proc_env)
                sdk_sess.start()
                self._sessions[key] = sdk_sess
        return self._sessions[key]

    def _remove(self, session_key: str) -> Optional[_SDKSession]:
        with self._lock:
            return self._sessions.pop(session_key, None)

    def start_session(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        return self._run_turn(session, message, model=model, proc_env=proc_env or {})

    def send_turn(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        return self._run_turn(session, message, model=model, proc_env=proc_env or {})

    def _run_turn(self, session: Session, message: str, *, model: Optional[str], proc_env: Dict[str, str]) -> ExecutionResult:
        start = time.time()
        try:
            sdk_sess = self._get_or_create(session, model, proc_env)
            session.driver_type = "sdk"
            output, backend_session_id, raw_ndjson = sdk_sess.send(message)
            elapsed = time.time() - start
            session.driver_status = "live"
            return ExecutionResult(
                success=True,
                output=output,
                backend_session_id=backend_session_id,
                errors=[],
                execution_time=elapsed,
                raw_stdout=raw_ndjson,
                raw_stderr="",
            )
        except Exception as e:
            import concurrent.futures as _cf
            elapsed = time.time() - start
            err_str = str(e)
            if not err_str:
                # TimeoutError / concurrent.futures.TimeoutError str() is blank in Python 3.11+
                if isinstance(e, (TimeoutError, _cf.TimeoutError)):
                    err_str = f"SDK turn timed out after {elapsed:.0f}s — claude produced no response"
                else:
                    err_str = f"{type(e).__name__} after {elapsed:.0f}s"
            return ExecutionResult(
                success=False,
                output="",
                errors=[err_str],
                execution_time=elapsed,
            )

    def cancel(self, session: Session) -> None:
        sdk_sess = self._remove(session.session_id)
        if sdk_sess is not None:
            sdk_sess.close()

    def close(self, session: Session) -> None:
        sdk_sess = self._remove(session.session_id)
        if sdk_sess is not None:
            sdk_sess.close()

    def mark_lost(self, session_id: str) -> None:
        """Called on worker restart: mark session as detached without closing."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def driver_type(self) -> str:
        return "sdk"


# ---------------------------------------------------------------------------
# ClaudePrintResumeDriver  (fallback, existing behaviour)
# ---------------------------------------------------------------------------

class ClaudePrintResumeDriver(ClaudeDriver):
    """Existing per-turn print/resume driver, kept as fallback only.

    --include-partial-messages is intentionally removed here; it inflated
    telemetry by duplicating usage rows per assistant message.
    """

    def __init__(self):
        self._exe = shutil.which("claude") or "claude"
        self._session_procs: Dict[str, subprocess.Popen] = {}
        self._oneoff_procs: set = set()
        self._proc_lock = threading.Lock()

    def start_session(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        session.driver_type = "print_resume"
        session.driver_status = "closed"
        return self._run(
            session.repo_path, message,
            resume_id=None,
            session_id=session.backend_session_id or str(uuid.uuid4()),
            session_key=session.session_id,
            model=model,
            proc_env=proc_env or {},
        )

    def send_turn(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        session.driver_type = "print_resume"
        session.driver_status = "closed"
        return self._run(
            session.repo_path, message,
            resume_id=session.backend_session_id or None,
            session_id=None,
            session_key=session.session_id,
            model=model,
            proc_env=proc_env or {},
        )

    def cancel(self, session: Session) -> None:
        with self._proc_lock:
            proc = self._session_procs.get(session.session_id)
        if proc is not None:
            terminate_many_popen([proc])

    def close(self, session: Session) -> None:
        pass

    def driver_type(self) -> str:
        return "print_resume"

    def _build_cmd(self, resume_id: Optional[str], session_id: Optional[str], model: Optional[str] = None) -> List[str]:
        tools = list(_DEFAULT_TOOLS)
        if _mcp_jobs_configured():
            tools.append("mcp__jobs__watch_job")

        if resume_id:
            cmd = [
                self._exe,
                "--resume", resume_id,
                "--verbose",
                "--output-format", "stream-json",
                # --include-partial-messages deliberately omitted (telemetry duplication)
                "--dangerously-skip-permissions",
                "-p",
            ]
        else:
            cmd = [
                self._exe,
                "--verbose",
                "--output-format", "stream-json",
                "--dangerously-skip-permissions",
                "-p",
            ]
            if session_id:
                cmd.extend(["--session-id", session_id])

        if model:
            cmd.extend(["--model", model])

        cmd.extend(["--allowedTools", ",".join(tools)])
        return cmd

    def _register_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        stale: Optional[subprocess.Popen] = None
        with self._proc_lock:
            if session_key:
                stale = self._session_procs.get(session_key)
                self._session_procs[session_key] = proc
            else:
                self._oneoff_procs.add(proc)
        if stale is not None and stale is not proc:
            terminate_many_popen([stale])

    def _unregister_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        with self._proc_lock:
            if session_key:
                current = self._session_procs.get(session_key)
                if current is proc:
                    self._session_procs.pop(session_key, None)
            else:
                self._oneoff_procs.discard(proc)

    def _run(
        self,
        cwd: str,
        message: str,
        resume_id: Optional[str],
        session_id: Optional[str],
        session_key: Optional[str],
        model: Optional[str],
        proc_env: Dict[str, str],
    ) -> ExecutionResult:
        start = time.time()
        try:
            inactivity_sec = 36000
            try:
                from config import config as _cfg
                inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 36000)))
            except Exception:
                pass

            cmd = self._build_cmd(resume_id, session_id, model)
            proc: Optional[subprocess.Popen] = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd or None,
                    env=proc_env or None,
                    creationflags=_NO_WINDOW,
                )
                self._register_process(proc, session_key)
                proc.stdin.write(message.encode())
                proc.stdin.close()

                stdout_q: queue.Queue = queue.Queue()
                stderr_q: queue.Queue = queue.Queue()
                _SENTINEL = object()

                def _reader(pipe, q: queue.Queue) -> None:
                    try:
                        for raw_line in pipe:
                            q.put(raw_line)
                    finally:
                        q.put(_SENTINEL)

                t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_q), daemon=True)
                t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_q), daemon=True)
                t_out.start()
                t_err.start()

                stdout_lines: List[bytes] = []
                stderr_lines: List[bytes] = []
                stdout_done = False
                stderr_done = False
                killed = False

                while not (stdout_done and stderr_done):
                    if not stdout_done:
                        try:
                            item = stdout_q.get(timeout=inactivity_sec)
                            if item is _SENTINEL:
                                stdout_done = True
                            else:
                                stdout_lines.append(item)
                        except queue.Empty:
                            logger.warning("claude inactivity timeout after %.0fs -- terminating pid=%s", inactivity_sec, proc.pid)
                            killed = True
                            terminate_many_popen([proc])
                            stdout_done = True

                    if not stderr_done:
                        while True:
                            try:
                                item = stderr_q.get_nowait()
                                if item is _SENTINEL:
                                    stderr_done = True
                                    break
                                stderr_lines.append(item)
                            except queue.Empty:
                                break

                # drain
                for q_ref, lines_ref, wait in ((stdout_q, stdout_lines, False), (stderr_q, stderr_lines, True)):
                    if wait:
                        while True:
                            try:
                                item = q_ref.get(timeout=5.0)
                                if item is _SENTINEL:
                                    break
                                lines_ref.append(item)
                            except queue.Empty:
                                break
                    else:
                        while True:
                            try:
                                item = q_ref.get_nowait()
                                if item is not _SENTINEL:
                                    lines_ref.append(item)
                            except queue.Empty:
                                break

                t_out.join(timeout=5.0)
                t_err.join(timeout=5.0)
                try:
                    proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    pass

                returncode = proc.returncode if proc.returncode is not None else -1
                stdout = b"".join(stdout_lines).decode(errors="replace")
                stderr = b"".join(stderr_lines).decode(errors="replace")
                elapsed = time.time() - start

                if killed:
                    inactivity_min = int(inactivity_sec // 60)
                    return ExecutionResult(
                        success=False,
                        output="",
                        errors=[
                            f"Claude process killed after {inactivity_min}m of inactivity. "
                            f"Adjust GATEWAY_INACTIVITY_TIMEOUT_SEC (currently {inactivity_sec}) to tune this."
                        ],
                        execution_time=elapsed,
                        raw_stdout=stdout,
                        raw_stderr=stderr,
                    )

                return _parse_print_resume(stdout, stderr, returncode, elapsed, session_id or resume_id or "")

            finally:
                if proc is not None:
                    self._unregister_process(proc, session_key)

        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                errors=[str(e)],
                execution_time=time.time() - start,
            )

    def terminate_active_processes(self) -> None:
        with self._proc_lock:
            procs = list(self._session_procs.values()) + list(self._oneoff_procs)
        terminate_many_popen(procs)


# ---------------------------------------------------------------------------
# Parse helper (shared for print/resume path)
# ---------------------------------------------------------------------------

def _extract_text_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts).strip()


def _extract_output(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        for item in reversed(payload):
            text = _extract_output(item)
            if text:
                return text
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("result", "content", "output", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        text = _extract_output(value)
        if text:
            return text
    for key in ("messages", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            text = _extract_output(value)
            if text:
                return text
    if payload.get("type") in ("text", "message") and isinstance(payload.get("text"), str):
        return payload["text"].strip()
    return ""


def _parse_print_resume(stdout: str, stderr: str, returncode: int, elapsed: float, known_session_id: str = "") -> ExecutionResult:
    success = returncode == 0
    backend_session_id = known_session_id or ""
    output = stdout.strip()
    parsed_output = None
    parsed_errors: List[str] = []

    if stdout:
        try:
            data = json.loads(stdout)
            backend_session_id = data.get("session_id", "") or backend_session_id
            parsed_output = data
            output = _extract_output(data)
            maybe_errors = data.get("errors")
            if isinstance(maybe_errors, list):
                parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
        except Exception:
            assistant_text = ""
            delta_parts: List[str] = []
            final_result: Optional[Dict[str, Any]] = None
            for line in stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d: Dict[str, Any] = json.loads(line)
                    if "session_id" in d:
                        backend_session_id = d["session_id"]
                    if d.get("type") == "assistant":
                        message = d.get("message")
                        text = ""
                        if isinstance(message, dict):
                            text = _extract_text_blocks(message.get("content"))
                        if text:
                            assistant_text = text
                    elif d.get("type") == "stream_event":
                        event = d.get("event")
                        if isinstance(event, dict) and event.get("type") == "content_block_delta":
                            delta = event.get("delta")
                            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    delta_parts.append(text)
                    elif d.get("type") == "result":
                        final_result = d
                        maybe_errors = d.get("errors")
                        if isinstance(maybe_errors, list):
                            parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
                    candidate = _extract_output(d)
                    if candidate:
                        output = candidate
                        parsed_output = d
                except Exception:
                    pass
            if assistant_text:
                output = assistant_text
            elif delta_parts:
                output = "".join(delta_parts).strip()
            if final_result is not None:
                if output:
                    final_result["assistant_text"] = output
                parsed_output = final_result
                maybe_errors = final_result.get("errors")
                if isinstance(maybe_errors, list):
                    parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
            if not output:
                output = stdout.strip()

    errors = [stderr.strip()] if stderr and not success else []
    if not success and not errors and parsed_errors:
        errors = parsed_errors
    if not success and not errors:
        errors = [f"Claude exited with code {returncode}"]
    return ExecutionResult(
        success=success,
        output=output,
        backend_session_id=backend_session_id,
        errors=errors,
        execution_time=elapsed,
        raw_stdout=stdout,
        raw_stderr=stderr,
        parsed_output=parsed_output,
        return_code=returncode,
    )


# ---------------------------------------------------------------------------
# Driver registry
# ---------------------------------------------------------------------------

_SDK_AVAILABLE: Optional[bool] = None


def _sdk_available() -> bool:
    global _SDK_AVAILABLE
    if _SDK_AVAILABLE is None:
        try:
            import claude_agent_sdk  # noqa: F401
            _SDK_AVAILABLE = True
        except ImportError:
            _SDK_AVAILABLE = False
    return _SDK_AVAILABLE


def build_driver(driver_type: str = "auto") -> ClaudeDriver:
    """Return the appropriate driver.

    "sdk"         -- ClaudeSDKClientDriver; raises RuntimeError if SDK not importable
                     (no silent fallback — use "auto" if you want graceful degradation)
    "auto"        -- SDK if available, else print_resume (WARNING logged on fallback)
    "print_resume"-- ClaudePrintResumeDriver (legacy CLI, always available)
    """
    if driver_type == "sdk":
        if not _sdk_available():
            raise RuntimeError(
                "CLAUDE_DRIVER_TYPE=sdk but claude_agent_sdk is not importable. "
                "Install it in the venv: pip install claude-agent-sdk  "
                "To allow silent fallback set CLAUDE_DRIVER_TYPE=auto instead."
            )
        logger.info("event=driver_selected driver=sdk")
        return ClaudeSDKClientDriver()
    if driver_type == "auto":
        if _sdk_available():
            logger.info("event=driver_selected driver=sdk (auto)")
            return ClaudeSDKClientDriver()
        logger.warning(
            "event=driver_fallback driver=print_resume reason=sdk_unavailable "
            "— SDK package (claude_agent_sdk) not importable; falling back to LEGACY "
            "CLI/print-resume driver. Sessions will be stateless and subject to "
            "inactivity timeouts. This is a degraded mode — fix SDK availability ASAP."
        )
        return ClaudePrintResumeDriver()
    # driver_type == "print_resume" (explicit legacy request)
    logger.info("event=driver_selected driver=print_resume (explicit)")
    return ClaudePrintResumeDriver()

