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


def _extract_result_from_ndjson(raw_stdout: str) -> str:
    """Extract full assistant reply text from backend NDJSON raw_stdout.

    Handles two backend formats:

    claude-code: emits ``{"type":"result","result":"<full text>"}`` as the last line.

    codex: emits multiple ``{"type":"item.completed","item":{"type":"agent_message",
    "text":"..."}}`` lines — each is one paragraph/thought; join them to get the
    full reply. The ``turn.completed`` line has usage but no text.
    """
    result_text = ""
    codex_parts: List[str] = []

    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        # claude-code: single result line wins immediately
        if t == "result":
            r = obj.get("result") or ""
            if r:
                result_text = r
        # codex: accumulate agent_message items
        elif t == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message":
                txt = item.get("text") or ""
                if txt:
                    codex_parts.append(txt)

    if result_text:
        return result_text
    if codex_parts:
        return "\n\n".join(codex_parts)
    return ""


def _result_text_from_artifact(art: Dict[str, Any]) -> str:
    """Full untruncated result text from an artifact file.

    Priority:
    1. NDJSON raw_stdout result line — always the complete claude-code reply.
    2. extract_text_from_payload on parsed_output — works for some backends.
    3. Empty string (caller decides fallback).
    """
    # 1. raw_stdout result line (claude-code backend — most sessions)
    raw_stdout = art.get("raw_stdout") or ""
    if raw_stdout:
        text = _extract_result_from_ndjson(raw_stdout)
        if text:
            return _clean_result(text, bool(art.get("success")), art.get("errors") or [])

    # 2. parsed_output via extract_text_from_payload (other backends / old schema)
    try:
        from src.services.result_text import extract_text_from_payload
    except Exception:
        extract_text_from_payload = None  # type: ignore

    text = ""
    if extract_text_from_payload is not None:
        parsed = art.get("parsed_output")
        for src in (parsed, art.get("result")):
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


# ── Primary source: mesh.db task ledger ───────────────────────────────────────


def _turns_from_db(session_id: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    """Project the session's task ledger into conversation turns.

    Returns a turn list when the DB can serve the conversation, or ``None`` to
    signal the caller to fall back to the file-stitching path (DB unavailable, or
    no task row for this session carries a backfilled ``reply_text`` yet).

    A row is "usable" when ``reply_text`` is populated — that's the marker that the
    artifact-complete enrichment (or backfill) has run for it. If NOT ONE row in
    the session has reply_text, we hand off to the file path so old un-backfilled
    sessions don't render blank.
    """
    try:
        from src.control.db import get_db
        db = get_db()
        if db is None:
            return None
        rows = db.get_session_turns(session_id, limit=limit)
    except Exception as e:
        logger.debug("transcript_db_read_failed session_id=%s err=%s", session_id, e)
        return None

    if not rows:
        return None
    if not any((r.get("reply_text") or "").strip() for r in rows):
        return None  # nothing enriched yet — let the file path handle it

    turns: List[Dict[str, Any]] = []
    for r in rows:
        success = (r.get("status") or "") not in ("failed", "failed_node_offline")
        instruction = (r.get("prompt") or "").strip()
        result = (r.get("reply_text") or "").strip()
        # Back-compat: a row enriched only via the legacy `result` JSON.
        if not result and r.get("result"):
            try:
                result = (json.loads(r["result"]) or {}).get("output") or ""
            except Exception:
                result = ""
        result = _clean_result(result, success, None)
        if not instruction and not result:
            continue
        # A failed task with no captured prompt is a ghost dispatch (e.g. a
        # dispatch-timeout that never ran) — task_history never recorded it, so the
        # chat never showed it. Skip it here too rather than render a blank user
        # turn with just a failure string. A FAILED turn that DOES have a prompt is
        # a real turn (user asked, it failed) and is kept.
        if not instruction and not success:
            continue

        files = []
        fc = r.get("files_modified_json")
        if fc:
            try:
                files = json.loads(fc) or []
            except Exception:
                files = []
        usage = None
        if r.get("usage_json"):
            try:
                usage = json.loads(r["usage_json"])
            except Exception:
                usage = None

        turns.append({
            "task_id": r.get("task_id") or "",
            "timestamp": r.get("completed_at") or r.get("created_at") or "",
            "success": success,
            "instruction": instruction,
            "result": result,
            "file_count": len(files) if isinstance(files, (list, tuple)) else 0,
            "usage": usage,
        })

    turns.sort(key=lambda t: t.get("timestamp") or "")
    if limit and len(turns) > limit:
        turns = turns[-limit:]
    return turns


# ── Fallback source: session task_history (files) ─────────────────────────────


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
    if art is not None:
        # Always try the artifact — it has the full untruncated text. Only keep
        # the task_history summary if it's longer (i.e. artifact fallback failed).
        artifact_result = _result_text_from_artifact(art)
        if artifact_result and len(artifact_result) > len(result):
            result = artifact_result
    if not result:
        result = _clean_result("", success, None)

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

    # Primary source: the task ledger in mesh.db. The conversation is a projection
    # of mesh_tasks — each task row carries the full prompt + untruncated reply_text
    # (written by orchestrator._mesh_complete_task / backfilled). No file I/O, no
    # NDJSON parsing. Falls through to the file-stitching path only when the DB has
    # no usable rows for this session (old sessions not yet backfilled, or DB off).
    db_turns = _turns_from_db(session_id, limit)
    if db_turns is not None:
        return db_turns

    if not session_path.is_file():
        return []

    record = _read_json(session_path)
    if record is None:
        return []

    history = record.get("task_history") or []
    if not isinstance(history, list) or not history:
        return []

    # Always load artifacts — they hold the full untruncated result text and are
    # needed both as a fallback for old turns (no user_message) and to override
    # any task_history result_summary that was written truncated (pre-fix turns).
    artifacts = _load_artifact_index(results_dir, session_id)

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
