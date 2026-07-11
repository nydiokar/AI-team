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
from collections import deque
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


# ---------------------------------------------------------------------------
# Turn outcome + error classification
# ---------------------------------------------------------------------------

# Phrases that mean "the cumulative context exceeded the model's window". These
# mirror the checks in src/services/result_text.py and src/orchestrator.py so the
# whole stack agrees on what "context_overflow" looks like. Keep them in sync.
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context_window",
    "context window",
    "blocking_limit",
    "exceeds the maximum",
    "too many tokens",
)


def classify_error_text(text: str) -> str:
    """Map a backend error string to an ExecutionResult.error_class.

    Returns ``"context_overflow"`` for context-window errors (the case worth
    special recovery), else ``"backend_error"``.
    """
    low = (text or "").lower()
    if any(m in low for m in _CONTEXT_OVERFLOW_MARKERS):
        return "context_overflow"
    return "backend_error"


# Max chars of salvaged progress we inline into the chat reply. The FULL text is
# always stored untruncated in the DB (reply_text) and reachable via the artifact
# / "show full" path — this only bounds what lands in the bubble so a context
# overflow never becomes a 100k-token unreadable dump.
_SALVAGE_INLINE_CAP = 4000


def _build_salvaged_reply(error_class: str, salvaged: str) -> str:
    """Compose the user-facing reply for an error turn: a short actionable banner
    followed by the salvaged progress (bounded). Never returns the raw error
    string alone — that was the original bug.
    """
    salvaged = (salvaged or "").strip()
    if error_class == "context_overflow":
        banner = (
            "⚠️ Context window full — the agent did the work but ran out of room to "
            "write its final summary. Use /compact or start a new session to continue."
        )
    else:
        banner = (
            "⚠️ The turn ended with a backend error before a final summary. "
            "The agent's progress up to that point is below."
        )
    if not salvaged:
        return banner
    body = salvaged
    if len(body) > _SALVAGE_INLINE_CAP:
        head = body[:_SALVAGE_INLINE_CAP].rstrip()
        omitted = len(body) - len(head)
        body = (
            head
            + f"\n\n[… {omitted:,} more chars — open the full reply in the web UI …]"
        )
    return f"{banner}\n\n---\n\n{body}"


def _make_activity_cb(session_id: Optional[str], task_id: Optional[str]):
    """Return a thread-safe callback that emits task_activity events to the
    observability spine. Called from inside the SDK async loop (background
    thread) — intentionally avoids contextvars and passes IDs explicitly."""
    if not session_id and not task_id:
        return None

    def cb(label: str) -> None:
        try:
            from src.core.observability import emit_event
            # Pass turn_id explicitly (= task_id in this context) so the stale
            # contextvar inherited by the reused SDK background thread doesn't
            # overwrite the correct current-turn value inside emit_event.
            emit_event(
                "task_activity",
                session_id=session_id,
                task_id=task_id,
                turn_id=task_id,
                label=label,
            )
        except Exception:
            pass

    return cb


@dataclass
class TurnOutcome:
    """Structured result of one SDK turn — carries the error signal so an
    ``is_error`` ResultMessage is never silently treated as a success reply."""
    output: str
    backend_session_id: str
    raw_ndjson: str
    is_error: bool = False
    error_class: str = ""
    error_text: str = ""
    # The last streamed assistant narration — the real work done this turn,
    # surfaced when the terminal result was an error (e.g. wrap-up overflow).
    salvaged_output: str = ""
    # True when this turn was NOT triggered by a user query we sent — i.e. the
    # live session continued on its own (a run_in_background job finished and the
    # agent produced a follow-up). These are delivered as proactive messages.
    proactive: bool = False


@dataclass
class _TurnAccumulator:
    """Per-turn scratch state built up by the persistent stream reader.

    One accumulator spans the messages of a single agent turn (all the
    ``AssistantMessage`` deltas up to and including the terminal
    ``ResultMessage``). It is reset after every ``ResultMessage`` so the next
    turn — solicited or autonomous — starts clean.
    """
    last_assistant_text: str = ""
    backend_session_id: str = ""
    ndjson_lines: List[str] = field(default_factory=list)


@dataclass
class _PendingTurn:
    """A user query we sent and are still awaiting the terminal result for.

    The reader fulfils ``future`` when the matching ``ResultMessage`` arrives.
    ``progress_cb`` is the activity callback for *this* turn, routed by the
    reader while this turn is the active (head) one.
    """
    future: "asyncio.Future"
    progress_cb: Any = None


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


def _mcp_manager_configured() -> bool:
    """[M3 A34] Whether the ai-team 'manager' MCP server is registered in
    ~/.claude.json. Mirrors _mcp_jobs_configured(); absent ⇒ never offered."""
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        return "manager" in cfg.get("mcpServers", {})
    except Exception:
        return False


def _manager_tools_enabled() -> bool:
    """[M3 A34] Second, INDEPENDENT gate for the Manager tool grant.

    Default OFF ⇒ byte-identical: even if ~/.claude.json has the 'manager' server,
    dispatch_worker / wait_for_worker are NOT added to a session's allowed_tools
    unless MANAGER_TOOLS_ENABLED is truthy. Rationale: dispatch_worker is a
    DISPATCH primitive (materially more powerful than jobs' read-only watch_job),
    so its grant gets a kill switch the operator controls in the gateway env —
    not just the global config file. Both gates must be satisfied to grant.
    """
    return os.environ.get("MANAGER_TOOLS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _session_allowed_tools() -> List[str]:
    """Assemble a Claude session's allowed-tool list: the defaults plus any
    opt-in MCP tool grants. Pure + gated so the grant logic is unit-testable
    without booting the SDK. Byte-identical to the prior inline logic for the
    jobs path; the manager grant is double-gated (env flag AND ~/.claude.json)."""
    tools = list(_DEFAULT_TOOLS)
    if _mcp_jobs_configured():
        tools.append("mcp__jobs__watch_job")
    if _manager_tools_enabled() and _mcp_manager_configured():
        tools.extend(["mcp__manager__dispatch_worker", "mcp__manager__wait_for_worker"])
    return tools


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
        # Persistent stream reader + FIFO of unfulfilled user turns. The reader
        # drains receive_messages() continuously so a result is NEVER left
        # buffered to be mis-attributed to the next query (the -1 turn offset
        # bug). Every entry is touched only on the SDK loop thread.
        self._reader_task: Optional["asyncio.Task"] = None
        self._pending: "deque[_PendingTurn]" = deque()
        # Sink for autonomous turns (background-job continuations). Set by the
        # driver; called as on_proactive(session_key, outcome) off the loop.
        self._on_proactive: Optional[Any] = None

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

        tools = _session_allowed_tools()

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
        # One long-lived consumer of the message stream for the life of the
        # session. This is the SDK's intended pattern for an interactive chat:
        # receive_response() (read-until-first-ResultMessage) is only correct
        # when the stream is empty between turns, which is false here — the CLI
        # emits unsolicited turns (a run_in_background job finishing, task
        # notifications) with no new query(). Draining continuously keeps every
        # result matched to the turn it belongs to.
        self._reader_task = asyncio.create_task(self._reader_loop())
        try:
            # Keep the event loop alive; work arrives via run_coroutine_threadsafe
            # We run a simple await loop so the loop stays spinning until close.
            while not self._closed:
                await asyncio.sleep(0.5)
        finally:
            if self._reader_task is not None:
                self._reader_task.cancel()
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
            # The coroutine can still be alive on the SDK's own loop thread —
            # e.g. mid `receive_response()` with the real claude subprocess
            # still generating output — when this raises (most commonly a
            # `sdk_turn_timeout_sec` timeout, the SDK-driver equivalent of the
            # legacy driver's inactivity/hard-cap kill). Cancelling the
            # wrapper future alone doesn't reach it: that's the same gap the
            # cancel-button bug had. Interrupt the actual turn so it stops for
            # real instead of running unattended in the background.
            fut.cancel()
            self.cancel_inflight()
            raise

    async def _reader_loop(self) -> None:
        """Drain the SDK message stream for the life of the session.

        This is the single consumer of ``receive_messages()``. It accumulates
        each turn's assistant narration and, on the terminal ``ResultMessage``,
        builds a :class:`TurnOutcome` and dispatches it:

          * If a user query is awaiting a result (``self._pending``), the oldest
            one is fulfilled — that's the reply to that prompt.
          * Otherwise the turn is *autonomous* (a run_in_background job finished
            and the agent kept going) — it's routed to the proactive sink so the
            user gets it as its own message instead of it silently buffering and
            being mis-served as the reply to their *next* prompt (the -1 offset).

        Draining continuously is what guarantees no result is ever left waiting
        in the transport between turns, which is the whole bug.
        """
        if self._client is None:
            return
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ThinkingBlock

        acc = _TurnAccumulator(backend_session_id=self.backend_session_id)
        try:
            async for msg in self._client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    # Overwrite (not append) so only the last assistant block of
                    # the turn survives as the salvage source.
                    blocks_text = "".join(
                        block.text for block in msg.content if isinstance(block, TextBlock)
                    ).strip()
                    if blocks_text:
                        acc.last_assistant_text = blocks_text
                    # Real-time activity signals for the head (active) turn only.
                    progress_cb = self._head_progress_cb()
                    if progress_cb is not None:
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                progress_cb(f"Using {block.name}")
                            elif isinstance(block, ThinkingBlock):
                                progress_cb("Thinking…")
                            elif isinstance(block, TextBlock) and block.text:
                                progress_cb("Writing response…")
                    usage = _plain_usage_dict(getattr(msg, "usage", None))
                    if usage is not None:
                        acc.ndjson_lines.append(json.dumps({"type": "assistant", "message": {"usage": usage}}))
                    sid = getattr(msg, "session_id", None) or ""
                    if sid:
                        acc.backend_session_id = sid
                        self.backend_session_id = sid
                elif isinstance(msg, ResultMessage):
                    outcome = self._outcome_from_result(acc, msg)
                    self._dispatch(outcome)
                    acc = _TurnAccumulator(backend_session_id=self.backend_session_id)
        except asyncio.CancelledError:
            # Session closing. Fall through to `finally` so waiters don't hang.
            raise
        except Exception as e:
            # The stream broke (subprocess died, transport closed). Don't leave
            # callers blocked forever — fail every pending turn with the error.
            logger.warning(
                "event=sdk_reader_stream_ended session_key=%s err=%s",
                self.session_key, e,
            )
            self._closed = True
        finally:
            # Any turn still waiting when the stream stops will never get a
            # result — reject it rather than block the worker thread forever.
            if self._pending:
                self._fail_pending(RuntimeError("SDK session stream closed before the turn completed"))

    def _outcome_from_result(self, acc: "_TurnAccumulator", msg: Any) -> "TurnOutcome":
        """Build a :class:`TurnOutcome` from the accumulated turn + its terminal
        ``ResultMessage``.

        Error handling (critical): the SDK yields a terminal ``ResultMessage``
        with ``is_error=True`` and terminates the turn normally — it does NOT
        raise on a long-lived stream-json session. So we MUST inspect
        ``is_error`` here; we cannot rely on an exception. On an error result
        ``ResultMessage.result`` is the error string, NOT an answer, so we
        surface the salvaged narration as ``output`` and carry the error in the
        error fields. The synthesised terminal ``result`` NDJSON line keeps
        usage + structural error fields so the M3 telemetry adapter classifies
        the turn honestly.
        """
        sid = getattr(msg, "session_id", "") or ""
        if sid:
            acc.backend_session_id = sid
            self.backend_session_id = sid
        is_error = bool(getattr(msg, "is_error", False))
        subtype = str(getattr(msg, "subtype", "") or "")
        stop_reason = str(getattr(msg, "stop_reason", "") or "") or None
        errors = getattr(msg, "errors", None)
        result_text = ""
        r = getattr(msg, "result", None)
        if r and isinstance(r, str):
            result_text = r.strip()
        error_text = ""
        if is_error:
            if isinstance(errors, list) and errors:
                error_text = "; ".join(str(e).strip() for e in errors if str(e).strip())
            error_text = error_text or result_text or subtype or "backend error result"
        usage = _plain_usage_dict(getattr(msg, "usage", None))
        result_line: Dict[str, Any] = {"type": "result"}
        if subtype:
            result_line["subtype"] = subtype
        result_line["is_error"] = is_error
        if stop_reason:
            result_line["stop_reason"] = stop_reason
        if usage is not None:
            result_line["usage"] = usage
        if result_text:
            result_line["result"] = result_text
        if isinstance(errors, list) and errors:
            result_line["errors"] = [str(e) for e in errors]
        acc.ndjson_lines.append(json.dumps(result_line))

        raw_ndjson = "\n".join(acc.ndjson_lines)
        if is_error:
            return TurnOutcome(
                output=acc.last_assistant_text,
                backend_session_id=acc.backend_session_id,
                raw_ndjson=raw_ndjson,
                is_error=True,
                error_class=classify_error_text(error_text),
                error_text=error_text,
                salvaged_output=acc.last_assistant_text,
            )
        return TurnOutcome(
            output=result_text or acc.last_assistant_text,
            backend_session_id=acc.backend_session_id,
            raw_ndjson=raw_ndjson,
            is_error=False,
        )

    def _head_progress_cb(self) -> Any:
        """Activity callback of the active (oldest unfulfilled) turn, or None."""
        return self._pending[0].progress_cb if self._pending else None

    def _dispatch(self, outcome: "TurnOutcome") -> None:
        """Route a finished turn: fulfil the oldest pending query, or — if none
        is waiting — treat it as an autonomous turn and hand it to the proactive
        sink. Runs on the SDK loop thread."""
        if self._pending:
            pending = self._pending.popleft()
            if not pending.future.done():
                pending.future.set_result(outcome)
            return
        # No one asked for this turn — it's a background-job continuation.
        outcome.proactive = True
        if self._on_proactive is None:
            logger.info(
                "event=sdk_proactive_turn_dropped session_key=%s chars=%d "
                "(no proactive sink configured)",
                self.session_key, len(outcome.output or ""),
            )
            return
        asyncio.create_task(self._run_proactive(outcome))

    async def _run_proactive(self, outcome: "TurnOutcome") -> None:
        """Deliver an autonomous turn without blocking the reader loop."""
        try:
            await asyncio.to_thread(self._on_proactive, self.session_key, outcome)
        except Exception:
            logger.warning(
                "event=sdk_proactive_delivery_failed session_key=%s",
                self.session_key, exc_info=True,
            )

    def _fail_pending(self, err: Exception) -> None:
        """Reject every waiting turn — used when the stream dies."""
        while self._pending:
            pending = self._pending.popleft()
            if not pending.future.done():
                pending.future.set_exception(err)

    async def _submit_turn(self, message: str, progress_cb=None) -> "TurnOutcome":
        """Send one user turn and await its terminal result.

        Registers a pending entry BEFORE writing the query so the reader can
        never fulfil it out of order, then writes the query and awaits the
        future the reader will complete. Runs on the SDK loop thread.
        """
        if self._client is None:
            raise RuntimeError("SDK client not initialised")
        loop = asyncio.get_event_loop()
        future: "asyncio.Future" = loop.create_future()
        pending = _PendingTurn(future=future, progress_cb=progress_cb)
        self._pending.append(pending)
        try:
            # session_id here is the SDK's *internal* conversation-thread
            # selector, not the gateway session id; one _SDKSession owns one
            # claude process, so the default thread is correct.
            await self._client.query(message)
        except Exception:
            # Query never landed — drop the pending entry so it can't swallow a
            # later result and re-introduce an offset.
            try:
                self._pending.remove(pending)
            except ValueError:
                pass
            raise
        return await future

    def send(self, message: str, progress_cb=None) -> "TurnOutcome":
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
        # Guard against a stale turn still holding the lock (e.g. a cancel that
        # didn't reach this session in time, or a caller that never cancelled at
        # all). Queuing silently behind it is what produced the ever-growing
        # transcript bug: the new message would only get sent once the old,
        # abandoned turn finally finished on its own, appending straight into
        # the same live conversation. Interrupt it up front instead so the new
        # turn starts promptly on a clean, still-live session.
        if not self._lock.acquire(blocking=False):
            logger.warning(
                "event=sdk_session_turn_conflict session_key=%s — a previous turn "
                "is still in flight; interrupting it before starting this one",
                self.session_key,
            )
            self.cancel_inflight()
            self._lock.acquire()
        try:
            return self.submit(self._submit_turn(message, progress_cb=progress_cb), timeout=timeout)
        finally:
            self._lock.release()

    def cancel_inflight(self) -> None:
        """Best-effort interrupt of a turn in progress.

        Without this, cancelling only detaches the caller from the session —
        the claude subprocess is still generating the turn, which the reader
        loop keeps consuming to completion in the background thread. A
        cancelled-then-resent prompt would then race
        a brand-new session against the still-live old one, both writing to
        their own transcripts (the ever-growing-session bug). Sending the
        SDK's control-protocol interrupt makes the CLI end the current turn so
        `receive_response()` returns promptly and the background thread can
        actually stop.
        """
        if not self._loop or not self._loop.is_running() or self._client is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._client.interrupt(), self._loop)
            fut.result(timeout=10)
        except Exception:
            # The legacy print/resume driver's answer to an unresponsive turn
            # was unconditional: terminate_many_popen(), no cooperation
            # required. A cooperative interrupt that doesn't land within 10s
            # means the CLI itself is wedged, so the same escalation applies
            # here — force the session closed. `_async_run`'s idle loop picks
            # up `_closed` on its next tick and disconnects, which itself
            # escalates through SIGTERM then SIGKILL (see claude_agent_sdk's
            # transport.close()) if the process still won't exit gracefully.
            # This also unblocks whatever `send()` call is still stuck in
            # `submit()` for this session: once the subprocess actually dies,
            # its blocked read raises and that call returns. The pool
            # (`ClaudeSDKClientDriver._get_or_create`) discards a `_closed`
            # entry rather than handing a dead client to the next turn.
            logger.warning(
                "event=sdk_interrupt_failed session_key=%s — forcing a hard "
                "teardown instead of leaving an unresponsive session running",
                self.session_key, exc_info=True,
            )
            self._closed = True
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(lambda: None)

    def close(self) -> None:
        self.cancel_inflight()
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
        # Sink for autonomous (background-job continuation) turns. Injected by
        # the worker at startup; propagated to every _SDKSession. Signature:
        # on_proactive(session_key: str, outcome: TurnOutcome) -> None.
        self._on_proactive: Optional[Any] = None

    def set_proactive_sink(self, sink: Any) -> None:
        """Register the callback that delivers autonomous turns. Applies to
        sessions created after this call and back-fills live ones."""
        self._on_proactive = sink
        with self._lock:
            for sess in self._sessions.values():
                sess._on_proactive = sink

    def _get_or_create(
        self,
        session: Session,
        model: Optional[str],
        proc_env: Dict[str, str],
    ) -> _SDKSession:
        key = session.session_id
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing._closed:
                # A prior turn force-closed this session (its interrupt never
                # landed — see _SDKSession.cancel_inflight). Handing it to the
                # next turn would just fail immediately ("loop is not
                # running"); start clean instead, same as the legacy driver's
                # `_register_process` replacing a stale Popen for the same
                # session key before spawning the next one.
                self._sessions.pop(key, None)
                existing = None
            if existing is None:
                sdk_sess = _SDKSession(key, session.repo_path, model, proc_env)
                sdk_sess._on_proactive = self._on_proactive
                sdk_sess.start()
                self._sessions[key] = sdk_sess
        return self._sessions[key]

    def _remove(self, session_key: str) -> Optional[_SDKSession]:
        with self._lock:
            return self._sessions.pop(session_key, None)

    def start_session(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        return self._run_turn(session, message, model=model, proc_env=proc_env or {}, telemetry_context=telemetry_context)

    def send_turn(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        return self._run_turn(session, message, model=model, proc_env=proc_env or {}, telemetry_context=telemetry_context)

    def _run_turn(self, session: Session, message: str, *, model: Optional[str], proc_env: Dict[str, str], telemetry_context=None) -> ExecutionResult:
        start = time.time()
        try:
            sdk_sess = self._get_or_create(session, model, proc_env)
            session.driver_type = "sdk"
            # Build a lightweight progress callback so the SDK message loop can
            # emit task_activity events in real time. IDs are passed explicitly
            # (not via contextvars) because the SDK loop runs in its own thread.
            sess_id = getattr(telemetry_context, "session_id", None) or (session.session_id if session else None)
            t_id = getattr(telemetry_context, "turn_id", None)
            progress_cb = _make_activity_cb(sess_id, t_id)
            outcome = sdk_sess.send(message, progress_cb=progress_cb)
            elapsed = time.time() - start
            session.driver_status = "live"

            if outcome.is_error:
                # The turn did real work but its terminal result was an error
                # (typically the final wrap-up message overflowed the context
                # window). Fail honestly so retry/compact policy engages, but
                # DELIVER the salvaged progress + an actionable banner instead of
                # discarding 4 minutes of work or dumping a bare error string.
                reply = _build_salvaged_reply(outcome.error_class, outcome.salvaged_output)
                return ExecutionResult(
                    success=False,
                    output=reply,
                    backend_session_id=outcome.backend_session_id,
                    errors=[outcome.error_text or "backend returned an error result"],
                    error_class=outcome.error_class or "backend_error",
                    execution_time=elapsed,
                    raw_stdout=outcome.raw_ndjson,
                    raw_stderr="",
                )

            return ExecutionResult(
                success=True,
                output=outcome.output,
                backend_session_id=outcome.backend_session_id,
                errors=[],
                execution_time=elapsed,
                raw_stdout=outcome.raw_ndjson,
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
        """Abort the in-flight turn but keep the session's process alive.

        This is "stop what you're doing", not "close the session" — the
        subprocess and its conversation state stay pooled so the next turn on
        this session resumes the same live process instead of paying to spin
        up a fresh one. Popping/closing here was the bug: it looked like
        cleanup but actually just abandoned the running query with no one
        watching it, while the pool no longer knew about it either.
        """
        with self._lock:
            sdk_sess = self._sessions.get(session.session_id)
        if sdk_sess is not None:
            sdk_sess.cancel_inflight()

    def close(self, session: Session) -> None:
        sdk_sess = self._remove(session.session_id)
        if sdk_sess is not None:
            sdk_sess.close()

    def mark_lost(self, session_id: str) -> None:
        """Called on worker restart: mark session as detached without closing."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def live_session_count(self) -> int:
        """Number of pooled live SDK sessions (each owns one claude process).

        Reported in worker live_state so the mesh can reconcile pooled backend
        processes against the gateway's session view — the delta is orphans.

        Deliberately LOCKLESS: this is called from the worker heartbeat on the
        asyncio event-loop thread, and ``_get_or_create`` holds ``self._lock``
        across ``start()`` (up to a 90s connect). Taking the lock here would
        freeze the event loop for that whole window. ``len()`` on a dict is
        atomic under the GIL; a momentary off-by-one during a concurrent
        insert/pop is irrelevant for a heartbeat gauge.
        """
        return len(self._sessions)

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

    def run_oneoff(
        self,
        cwd: str,
        message: str,
        *,
        model: Optional[str] = None,
        proc_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """One-shot (stateless) call — no session, no resume.

        Replaces the legacy ``ClaudeCodeBackend._run`` path so ``run_oneoff``
        goes through the driver and benefits from the hard-cap timeout.
        """
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        return self._run(
            cwd, message,
            resume_id=None,
            session_id=None,
            session_key=None,
            model=model,
            proc_env=proc_env or {},
        )

    def driver_type(self) -> str:
        return "print_resume"

    def _build_cmd(self, resume_id: Optional[str], session_id: Optional[str], model: Optional[str] = None) -> List[str]:
        tools = _session_allowed_tools()

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
            # Absolute wall-clock cap, independent of the rolling inactivity
            # timer. The inactivity timeout resets on every line of stdout, so a
            # backend that dribbles output (or emits its banner then stalls) can
            # otherwise run forever, stacking up zombie claude.exe processes.
            # 0 disables the hard cap (falls back to inactivity-only, legacy
            # behaviour). Default to a generous multiple of the inactivity
            # window so normal long tasks are unaffected.
            hard_cap_sec = 0
            try:
                from config import config as _cfg
                inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 36000)))
                hard_cap_sec = int(getattr(_cfg.system, "task_timeout", 0) or 0)
            except Exception:
                pass
            if hard_cap_sec <= 0:
                # Safety net even when task_timeout is disabled: never let a
                # single invocation exceed 4x the inactivity window.
                hard_cap_sec = inactivity_sec * 4
            deadline = start + hard_cap_sec

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
                kill_reason = "inactivity"

                while not (stdout_done and stderr_done):
                    if not stdout_done:
                        # Wait no longer than whichever budget expires first:
                        # the rolling inactivity window or the absolute deadline.
                        remaining_hard = deadline - time.time()
                        wait_for = inactivity_sec if remaining_hard <= 0 else min(inactivity_sec, remaining_hard)
                        if wait_for <= 0:
                            wait_for = 0.1
                        try:
                            item = stdout_q.get(timeout=wait_for)
                            if item is _SENTINEL:
                                stdout_done = True
                            else:
                                stdout_lines.append(item)
                        except queue.Empty:
                            if time.time() >= deadline:
                                kill_reason = "hard_cap"
                                logger.warning(
                                    "claude hard-cap timeout after %.0fs (task_timeout) -- terminating pid=%s",
                                    hard_cap_sec, proc.pid,
                                )
                            else:
                                kill_reason = "inactivity"
                                logger.warning(
                                    "claude inactivity timeout after %.0fs -- terminating pid=%s",
                                    inactivity_sec, proc.pid,
                                )
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
                    if kill_reason == "hard_cap":
                        cap_min = int(hard_cap_sec // 60)
                        err_msg = (
                            f"Claude process killed after {cap_min}m wall-clock (hard cap). "
                            f"Adjust GATEWAY_TASK_TIMEOUT_SEC (currently {hard_cap_sec}) to tune this."
                        )
                    else:
                        inactivity_min = int(inactivity_sec // 60)
                        err_msg = (
                            f"Claude process killed after {inactivity_min}m of inactivity. "
                            f"Adjust GATEWAY_INACTIVITY_TIMEOUT_SEC (currently {inactivity_sec}) to tune this."
                        )
                    return ExecutionResult(
                        success=False,
                        output="",
                        errors=[err_msg],
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

