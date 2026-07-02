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


def _text_from_content_blocks(content: Any) -> str:
    """Join the ``text`` of a claude-style content block array (skip tool_use etc.)."""
    if not isinstance(content, list):
        return ""
    out: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = (block.get("text") or "").strip()
            if t:
                out.append(t)
    return "\n".join(out).strip()


def _extract_from_ndjson(text: str) -> str:
    """Pull the agent's reply out of a newline-delimited JSON event stream.

    Handles all three backends' streams:
      • codex    — ``item.completed`` of type ``agent_message`` (``item.text``).
      • claude   — terminal ``result`` event (``result`` string), else the last
                   ``assistant`` event's ``message.content`` text blocks.
      • opencode — flat ``message``/``assistant`` events carrying ``text``.
    Returns "" when the blob isn't an event stream we recognise, so callers fall
    back to the structured/raw paths. The terminal ``result`` text, when present,
    wins over accumulated assistant chunks (it is the final, deduped answer)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or not all(ln.startswith("{") for ln in lines):
        return ""
    parts: List[str] = []
    result_text = ""
    saw_event = False
    for ln in lines:
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if not isinstance(ev, dict) or "type" not in ev:
            continue
        saw_event = True
        etype = ev.get("type")
        if etype == "item.completed":  # codex
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                t = (item.get("text") or "").strip()
                if t:
                    parts.append(t)
        elif etype == "result":  # claude terminal event (authoritative)
            r = ev.get("result")
            if isinstance(r, str) and r.strip():
                result_text = r.strip()
        elif etype == "assistant":  # claude assistant turn (content blocks)
            t = _text_from_content_blocks((ev.get("message") or {}).get("content"))
            if not t and ev.get("text"):
                t = str(ev["text"]).strip()
            if t:
                parts.append(t)
        elif etype == "text":  # opencode — text lives at part.text (or flat text)
            part = ev.get("part") or {}
            t = (part.get("text") if isinstance(part, dict) else None) or ev.get("text") or ""
            t = str(t).strip()
            if t:
                parts.append(t)
        elif etype == "message" and ev.get("text"):  # generic flat shape
            parts.append(str(ev["text"]).strip())
    if not saw_event:
        return ""
    # Prefer the terminal result string; else the accumulated assistant text.
    return result_text or "\n".join(parts).strip()


def _int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _normalize_usage(
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> Optional[Dict[str, int]]:
    """One canonical usage shape across all backends (codex key names — the Web UI
    already maps these). Returns None when there's nothing meaningful to show."""
    if input_tokens + output_tokens + reasoning_output_tokens <= 0:
        return None
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
    }


def extract_usage_from_ndjson(text: str) -> Optional[Dict[str, Any]]:
    """Pull per-turn token usage out of any backend's NDJSON event stream, or None.

    Normalizes the THREE backend shapes into one canonical dict:
      • codex    ``turn.completed`` → ``usage: {input_tokens, cached_input_tokens,
                 output_tokens, reasoning_output_tokens}``
      • claude   ``result`` (subtype success) → ``usage: {input_tokens, output_tokens,
                 cache_read_input_tokens, cache_creation_input_tokens}``
      • opencode ``step_finish`` → ``part.tokens: {input, output, reasoning,
                 cache:{read,write}}``
    The last usage-bearing event in the stream wins (the final turn total)."""
    if not isinstance(text, str):
        return None
    result: Optional[Dict[str, Any]] = None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("{") or ("usage" not in ln and "tokens" not in ln):
            continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        if etype == "turn.completed":  # codex
            u = ev.get("usage") or {}
            if isinstance(u, dict):
                got = _normalize_usage(
                    input_tokens=_int(u.get("input_tokens")),
                    cached_input_tokens=_int(u.get("cached_input_tokens")),
                    output_tokens=_int(u.get("output_tokens")),
                    reasoning_output_tokens=_int(u.get("reasoning_output_tokens")),
                )
                if got:
                    result = got

        elif etype in ("result", "assistant"):  # claude
            u = ev.get("usage") or (ev.get("message") or {}).get("usage") or {}
            if isinstance(u, dict):
                got = _normalize_usage(
                    input_tokens=_int(u.get("input_tokens")),
                    cached_input_tokens=_int(u.get("cache_read_input_tokens"))
                    + _int(u.get("cache_creation_input_tokens")),
                    output_tokens=_int(u.get("output_tokens")),
                )
                if got:
                    result = got

        elif etype == "step_finish":  # opencode
            tok = (ev.get("part") or {}).get("tokens") or ev.get("tokens") or {}
            if isinstance(tok, dict):
                cache = tok.get("cache") or {}
                got = _normalize_usage(
                    input_tokens=_int(tok.get("input")),
                    cached_input_tokens=_int(cache.get("read")) + _int(cache.get("write"))
                    if isinstance(cache, dict)
                    else 0,
                    output_tokens=_int(tok.get("output")),
                    reasoning_output_tokens=_int(tok.get("reasoning")),
                )
                if got:
                    result = got

    return result


def extract_text_from_payload(payload: Any) -> str:
    """Best-effort extraction of a user-visible answer from structured payloads."""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        # NDJSON event stream (codex/opencode) — parse the agent_message events
        # rather than dumping the raw JSON lines into the conversation.
        ndjson = _extract_from_ndjson(text)
        if ndjson:
            return ndjson
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


def trim_reply_for_chat(text: str, max_chars: int) -> str:
    """Trim a long agent reply to ``max_chars`` characters for chat delivery.

    Strategy: keep the first ~30% (opening context) and the last ~70%
    (Claude's conclusion / summary), with a truncation notice between them.
    Both slices are aligned to the nearest paragraph boundary so the output
    doesn't begin or end mid-sentence.

    When ``max_chars`` is 0 or the text is already short enough, returns
    ``text`` unchanged.
    """
    if not max_chars or len(text) <= max_chars:
        return text

    head_budget = max(200, int(max_chars * 0.30))
    tail_budget = max_chars - head_budget

    # Snap head to the nearest paragraph end (double newline) or newline.
    head_raw = text[:head_budget]
    for sep in ("\n\n", "\n"):
        idx = head_raw.rfind(sep)
        if idx > head_budget // 2:  # at least halfway in — not degenerate
            head_raw = head_raw[:idx]
            break

    # Snap tail to start at the nearest paragraph/line start.
    tail_raw = text[-tail_budget:]
    for sep in ("\n\n", "\n"):
        idx = tail_raw.find(sep)
        if idx != -1 and idx < tail_budget // 4:  # within the first quarter
            tail_raw = tail_raw[idx + len(sep):]
            break

    omitted = len(text) - len(head_raw) - len(tail_raw)
    notice = f"\n\n[… {omitted:,} chars omitted — full reply in the web UI …]\n\n"
    return head_raw.rstrip() + notice + tail_raw.lstrip()


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
