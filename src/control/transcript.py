"""Session transcript reader — the real conversation, full-text.

The conversation comes from ONE authoritative source: the session record's
``task_history`` (``state/sessions/<id>.json``). Each entry is one turn and holds
the COMPLETE, untruncated user message + the result summary + a real timestamp +
the task id. This is what the orchestrator records per turn and what the agent's
own memory is keyed to — so it reads top-to-bottom like a normal chat app.

We deliberately do NOT use:
  * ``results/task_*.json`` artifact ``task.title`` — that is a truncated display
    label (``f"Task: {description[:50]}..."``), an execution/debug record, never
    the conversation. Reading it clipped every historical message to ~50 chars.
  * ``state/summaries/<id>.md`` — a Telegram-era SINGLE-turn snapshot that could
    only describe the latest turn and corrupted earlier turns when overlaid.

The artifact is consulted ONLY as a degraded fallback for *old* turns whose
``task_history`` entry predates the rich schema (no ``user_message``) — purely so
ancient turns aren't blank. New turns never touch it.

SECURITY: ``session_id`` is an opaque hex slug — a ``..`` or path separator
resolves to None, never an arbitrary read (same as the SPA resolver).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_INDEX_NAME = "index.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("transcript_read_failed path=%s err=%s", path, e)
        return None


def _confined(base_dir: Path, name: str, suffix: str) -> Optional[Path]:
    """Resolve ``base_dir/<name><suffix>`` confined to ``base_dir`` (no traversal).

    A session id is an opaque token (hex slug) — it must never contain a path
    separator or ``..``. Reject those outright, then verify the resolved path is
    still under ``base_dir`` (defense in depth, same as the SPA resolver)."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    base = base_dir.resolve()
    candidate = (base_dir / f"{name}{suffix}").resolve()
    if base in candidate.parents:
        return candidate
    return None


def _clean_result(text: str, success: bool, errors: Optional[List[str]] = None) -> str:
    """Normalize a stored result string; be honest about empty/failed turns."""
    t = (text or "").strip()
    if t:
        return t
    if not success:
        if errors:
            return f"(no output — {errors[0]})"
        return "(task failed — no output)"
    return ""


# ── Artifact fallback (old turns only) ────────────────────────────────────────


def _result_text_from_artifact(art: Dict[str, Any]) -> str:
    """Clean, user-visible result text for a turn, reusing the same extractor the
    Telegram reply path uses."""
    try:
        from src.services.result_text import extract_text_from_payload
    except Exception:
        extract_text_from_payload = None  # type: ignore

    parsed = art.get("parsed_output")
    text = ""
    if extract_text_from_payload is not None:
        for src in (parsed, art.get("result"), art.get("raw_stdout")):
            if src:
                text = extract_text_from_payload(src) or ""
                if text:
                    break
    return _clean_result(text, bool(art.get("success")), art.get("errors") or [])


def _usage_from_artifact(art: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Token usage for the turn (codex turn.completed), from parsed_output or the
    raw NDJSON stdout. None when the backend didn't report usage."""
    parsed = art.get("parsed_output")
    if isinstance(parsed, dict) and parsed.get("type") == "turn.completed":
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            return usage
    try:
        from src.services.result_text import extract_usage_from_ndjson
        return extract_usage_from_ndjson(art.get("raw_stdout") or "")
    except Exception:
        return None


def _instruction_from_artifact(art: Dict[str, Any]) -> str:
    """Best-effort instruction text from an artifact (FALLBACK only).

    Prefers ``task.prompt`` (full, written for turns created after the fix); falls
    back to the truncated ``task.title`` so an ancient turn shows *something*
    rather than blank."""
    task = art.get("task") or {}
    prompt = task.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    title = (task.get("title") or "").strip()
    if title.lower().startswith("task:"):
        title = title[5:].strip()
    return title


def _load_artifact_index(results_dir: Path, session_id: str) -> Dict[str, Dict[str, Any]]:
    """Map task_id → artifact dict for this session (used only as a fallback for
    old turns). Read lazily/once; tolerant of a missing results dir."""
    index: Dict[str, Dict[str, Any]] = {}
    if not results_dir.is_dir():
        return index
    for p in results_dir.glob("*.json"):
        if p.name == _INDEX_NAME or not p.is_file():
            continue
        art = _read_json(p)
        if art is None:
            continue
        art_sid = (art.get("session") or {}).get("session_id") or art.get("session_id")
        if art_sid != session_id:
            continue
        tid = art.get("task_id") or p.stem
        index[tid] = art
    return index


# ── Primary source: session task_history ──────────────────────────────────────


def _turn_from_history(
    entry: Dict[str, Any],
    artifacts: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """One conversation turn from a session ``task_history`` entry.

    Full ``user_message`` and ``result_summary`` when present (the rich schema);
    otherwise fall back to the artifact for that ``task_id`` so old turns aren't
    blank."""
    task_id = entry.get("task_id") or ""
    success = bool(entry.get("success", True))
    instruction = (entry.get("user_message") or "").strip()
    result = (entry.get("result_summary") or "").strip()
    files = entry.get("files_modified") or []

    art = artifacts.get(task_id) if task_id else None
    if not instruction and art is not None:
        instruction = _instruction_from_artifact(art)
    result = _clean_result(result, success, None)
    if not result and art is not None:
        result = _result_text_from_artifact(art)

    return {
        "task_id": task_id,
        "timestamp": entry.get("timestamp") or "",
        "success": success,
        "instruction": instruction,
        "result": result,
        "file_count": len(files) if isinstance(files, (list, tuple)) else 0,
        "usage": _usage_from_artifact(art) if art is not None else None,
    }


def get_transcript(
    results_dir: Path,
    sessions_dir: Path,
    session_id: str,
    limit: int = 50,
) -> Optional[List[Dict[str, Any]]]:
    """Reconstruct a session's conversation, oldest→newest, full-text.

    Source of truth is ``sessions_dir/<id>.json`` → ``task_history``. ``results_dir``
    is used only to backfill text for old turns whose history entry predates the
    rich schema.

    Returns None on a path-traversal attempt (endpoint → 404). A session with no
    history yet returns ``[]``. Each turn:
    ``{task_id, timestamp, success, instruction, result, file_count, usage}``.
    """
    session_path = _confined(sessions_dir, session_id, ".json")
    if session_path is None:
        return None  # traversal attempt
    if not session_path.is_file():
        return []

    record = _read_json(session_path)
    if record is None:
        return []

    history = record.get("task_history") or []
    if not isinstance(history, list) or not history:
        return []

    # Artifact index is only needed if some entry lacks full text — load lazily.
    needs_artifacts = any(
        isinstance(e, dict) and not (e.get("user_message") or e.get("result_summary"))
        for e in history
    )
    artifacts = _load_artifact_index(results_dir, session_id) if needs_artifacts else {}

    turns: List[Dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        turn = _turn_from_history(entry, artifacts)
        # Skip a fully-empty turn (no instruction and no result) — it's noise.
        if not turn["instruction"] and not turn["result"]:
            continue
        turns.append(turn)

    # Oldest→newest (a conversation reads top-to-bottom). Stable on equal ts.
    turns.sort(key=lambda t: t.get("timestamp") or "")
    if limit and len(turns) > limit:
        turns = turns[-limit:]
    return turns
