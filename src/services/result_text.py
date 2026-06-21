"""
Pure functions for extracting user-facing text from TaskResult objects.

These are the formatting helpers previously embedded in TaskOrchestrator.
They are pure (no self, no side-effects, no I/O) so that any consumer
(Telegram, CLI, future Web UI) can produce a readable summary of a task
outcome without coupling to the orchestrator or Telegram.

Each function takes a TaskResult (or similar duck-typed dict/object) and
returns plain text.  No Telegram Markdown, no emoji, no transport concern.
"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional


def extract_text_from_payload(payload: Any) -> str:
    """Best-effort extraction of a user-visible answer from structured payloads."""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                return extract_text_from_payload(json.loads(text))
            except Exception:
                return text
        return text

    if isinstance(payload, list):
        for item in reversed(payload):
            text = extract_text_from_payload(item)
            if text:
                return text
        return ""

    if not isinstance(payload, dict):
        return ""

    for key in ("result", "content", "output", "message", "text"):
        value = payload.get(key)
        text = extract_text_from_payload(value)
        if text:
            return text

    for key in ("messages", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            text = extract_text_from_payload(value)
            if text:
                return text

    return ""


def extract_rate_limit_info(result) -> Optional[Dict[str, Any]]:
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


def is_missing_backend_conversation(result) -> bool:
    texts = list(getattr(result, "errors", []) or [])
    po = getattr(result, "parsed_output", None)
    if isinstance(po, dict):
        maybe_errors = po.get("errors")
        if isinstance(maybe_errors, list):
            texts.extend(str(item) for item in maybe_errors)
    haystack = "\n".join(str(item) for item in texts).lower()
    return "no conversation found with session id" in haystack


def session_reply_text(result) -> str:
    """User-facing answer text for a completed turn."""
    for candidate in (
        getattr(result, "output", None),
        extract_text_from_payload(getattr(result, "parsed_output", None)),
        getattr(result, "raw_stdout", None),
    ):
        text = extract_text_from_payload(candidate)
        if text:
            return text

    return (
        "Claude completed the run but returned no final reply text.\n\n"
        "Check the artifact JSON for raw stdout/stderr and backend metadata."
    )


def failure_text(result) -> str:
    """Aggregate likely error-bearing text from the result payload."""
    parts: List[str] = []

    def _append(value: Any) -> None:
        if value is None:
            return
        text = extract_text_from_payload(value)
        if text:
            parts.append(text)
        elif isinstance(value, str) and value.strip():
            parts.append(value.strip())

    for err in (getattr(result, "errors", None) or []):
        _append(err)
    _append(getattr(result, "raw_stderr", ""))
    _append(getattr(result, "raw_stdout", ""))
    _append(getattr(result, "parsed_output", None))
    _append(getattr(result, "output", ""))
    return "\n".join(parts)


def short_failure_reason(result) -> str:
    """Return a concise, user-facing failure reason."""
    if getattr(result, "success", False):
        return ""

    texts: List[str] = [str(err).strip() for err in (getattr(result, "errors", []) or []) if str(err).strip()]
    haystack = failure_text(result)
    haystack_lower = haystack.lower()

    if "cancelled" in haystack_lower:
        return "Task cancelled"
    if is_missing_backend_conversation(result):
        return "Claude session expired"
    if any(s in haystack_lower for s in ("rate_limit_event", "rate limit", "rate-limit", "too many requests", "hit your limit", '"error":"rate_limit"', "overagestatus")):
        info = extract_rate_limit_info(result)
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
        for t in texts:
            tl = t.lower()
            if "timed out" in tl or "timeout" in tl or "inactivity" in tl:
                compact = " ".join(t.split())
                if len(compact) > 20:
                    return compact[:300]
        return "Claude timeout"
    if any(s in haystack_lower for s in ("connection reset", "connection aborted", "network error", "temporarily unavailable", "service unavailable")):
        return "Claude network error"
    if any(isinstance(e, str) and "interactive_prompt_detected" in e for e in (getattr(result, "errors", []) or [])):
        return "Claude needs interactive approval"

    for text in texts:
        low = text.lower()
        if low.startswith("claude exited with code "):
            continue
        compact = " ".join(text.split())
        if compact:
            return compact[:120]

    return "Claude failed"


def format_file_change_lines(result, limit: int = 20) -> List[str]:
    """Format a list of changed files for display."""
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

    files = getattr(result, "files_modified", None) or []
    lines = [f"  `{f}`" for f in files[:limit]]
    if len(files) > limit:
        lines.append(f"  _...and {len(files) - limit} more_")
    return lines
