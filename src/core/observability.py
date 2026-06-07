"""
Observability spine — shared logging + structured event emission.

This module is the single keystone used by all three mesh processes (gateway,
worker, embedded task server) so their output is consistently formatted and
correlatable across machines. It implements the standard two-stream model:

  * **Logs** (human-readable, via `logging`): bracketed-context format
        2026-06-07T16:21:45Z INFO  [node=main-pc task=task_abc123]
          mesh_dispatch backend=claude -> LP-1
    Every line auto-carries `[node=<node_id> ...]` and, when a task is in
    context, `task=<task_id>` — without threading kwargs through call sites.

  * **Events** (machine-readable NDJSON, via `emit_event`): one complete fact
    per line appended to `logs/events.ndjson`. The envelope is a *superset* of
    the schema the existing `main.py stats` / `tail-events` readers expect, so
    those keep working unchanged.

Correlation ID is `task_id` (already flows end-to-end through the task payload).
`grep <task_id> logs/events.ndjson` on any machine reconstructs that task's
local journey; correlating across machines is the same grep on each node's file.

Design rules (match the rest of the codebase):
  * event writes are best-effort and never raise into the caller;
  * no external dependencies — stdlib only;
  * the NDJSON envelope is intentionally OTLP-shippable later without rework.
"""

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Module state — set once by init_logging()
# ---------------------------------------------------------------------------

_NODE_ID: str = ""
_LOGS_DIR: Optional[Path] = None

# Per-task correlation context. Set/cleared around a unit of work so the
# formatter and emit_event can pick up task_id/session_id automatically.
_log_context: "contextvars.ContextVar[Dict[str, str]]" = contextvars.ContextVar(
    "log_context", default={}
)


# ---------------------------------------------------------------------------
# Correlation context
# ---------------------------------------------------------------------------

def set_log_context(**fields: str) -> "contextvars.Token":
    """Merge correlation fields (task_id, session_id, ...) into the current context.

    Returns a token; pass it to `reset_log_context` to restore the prior context.
    Use the `log_context(...)` context manager for the common scoped case.
    """
    current = dict(_log_context.get())
    current.update({k: v for k, v in fields.items() if v})
    return _log_context.set(current)


def reset_log_context(token: "contextvars.Token") -> None:
    try:
        _log_context.reset(token)
    except Exception:
        pass


class log_context:
    """Context manager: scope correlation fields to a block.

        with log_context(task_id=task.id, session_id=session.session_id):
            ...  # every log line + emit_event in here carries these IDs
    """

    def __init__(self, **fields: str) -> None:
        self._fields = fields
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> "log_context":
        self._token = set_log_context(**self._fields)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            reset_log_context(self._token)


def _current_context() -> Dict[str, str]:
    return dict(_log_context.get())


# ---------------------------------------------------------------------------
# Redaction — moved here from main.py so the worker gets it too
# ---------------------------------------------------------------------------

class RedactFilter(logging.Filter):
    """Best-effort redaction of secrets in log messages."""

    def __init__(self) -> None:
        super().__init__(name="redact")
        self._patterns = [
            # Telegram bot token in URL path: /bot<token>/...
            (re.compile(r"/bot[0-9A-Za-z:_-]+"), "/bot<REDACTED>"),
            # Authorization: Bearer <token>
            (re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", flags=re.IGNORECASE), r"\1<REDACTED>"),
            # GATEWAY_TELEGRAM_BOT_TOKEN=...
            (re.compile(r"(GATEWAY_TELEGRAM_BOT_TOKEN=)[^\s]+", flags=re.IGNORECASE), r"\1<REDACTED>"),
            # WORKER_TOKEN=...
            (re.compile(r"(WORKER_TOKEN=)[^\s]+", flags=re.IGNORECASE), r"\1<REDACTED>"),
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = msg
            for pat, repl in self._patterns:
                redacted = pat.sub(repl, redacted)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


# ---------------------------------------------------------------------------
# Formatter — bracketed-context human format
# ---------------------------------------------------------------------------

class _BracketedFormatter(logging.Formatter):
    """Produces:

        2026-06-07T16:21:45Z LEVEL [node=.. task=.. session=..]
          <message>

    The context block is built from the module node_id plus whatever is in the
    correlation contextvar at emit time. Falls back to `[node=..]` only.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ctx = _current_context()
        parts = []
        if _NODE_ID:
            parts.append(f"node={_NODE_ID}")
        if ctx.get("task_id"):
            parts.append(f"task={ctx['task_id']}")
        if ctx.get("session_id"):
            parts.append(f"session={ctx['session_id']}")
        ctx_block = f"[{' '.join(parts)}]" if parts else ""
        msg = record.getMessage()
        header = f"{ts} {record.levelname:<5} {ctx_block}".rstrip()
        # Indent the message on a continuation line for readability; include the
        # module name so the source is still discoverable.
        return f"{header}\n  {record.name}: {msg}"


# ---------------------------------------------------------------------------
# init_logging — single entry point for all processes
# ---------------------------------------------------------------------------

def init_logging(
    node_id: str,
    level: str = "INFO",
    log_file: Optional[str] = None,
    logs_dir: Optional[str] = None,
) -> None:
    """Configure root logging with the bracketed-context formatter + redaction.

    Args:
        node_id:   this process's node identity (gateway hostname or WORKER_NODE_ID).
        level:     log level name (e.g. "INFO").
        log_file:  optional path to a rotating file handler (e.g. orchestrator.log).
        logs_dir:  directory for events.ndjson; defaults to log_file's parent or "logs".
    """
    global _NODE_ID, _LOGS_DIR
    _NODE_ID = node_id or ""

    if logs_dir:
        _LOGS_DIR = Path(logs_dir)
    elif log_file:
        _LOGS_DIR = Path(log_file).parent
    else:
        _LOGS_DIR = Path("logs")
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    formatter = _BracketedFormatter()
    redact = RedactFilter()

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact)
    handlers.append(stream_handler)

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redact)
            handlers.append(file_handler)
        except Exception:
            pass

    root = logging.getLogger()
    # Reset any prior handlers (e.g. a stale basicConfig) so we don't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Reduce noise from third-party HTTP logs (httpx via python-telegram-bot)
    try:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# emit_event — process-agnostic structured NDJSON writer
# ---------------------------------------------------------------------------

def _events_path() -> Path:
    base = _LOGS_DIR if _LOGS_DIR is not None else Path("logs")
    return base / "events.ndjson"


def emit_event(
    name: str,
    *,
    node_id: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """Append one NDJSON event line to logs/events.ndjson. Never raises.

    IDs default from the current correlation context, then the module node_id,
    so callers inside a `log_context(...)` block don't need to repeat them.
    The envelope is a superset of the legacy schema (event/status/duration_s/
    task_type/error_class) so existing readers keep working.
    """
    try:
        ctx = _current_context()
        payload: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "event": name,
            "node_id": node_id or _NODE_ID or None,
        }
        sid = session_id or ctx.get("session_id")
        tid = task_id or ctx.get("task_id")
        if sid:
            payload["session_id"] = sid
        if tid:
            payload["task_id"] = tid
        if fields:
            payload.update(fields)

        path = _events_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        _maybe_rotate(path)
    except Exception:
        # Observability must never break the caller.
        pass


def _maybe_rotate(path: Path, max_bytes: int = 1_000_000, backup_count: int = 3) -> None:
    """Size-based rotation mirroring the original orchestrator._emit_event logic."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        for idx in range(backup_count - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{idx}")
            dst = path.with_suffix(path.suffix + f".{idx + 1}")
            if src.exists():
                try:
                    src.replace(dst)
                except Exception:
                    pass
        first_backup = path.with_suffix(path.suffix + ".1")
        try:
            path.replace(first_backup)
            path.touch()
        except Exception:
            pass
    except Exception:
        pass
