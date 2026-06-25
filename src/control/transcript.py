"""Session transcript reader — the conversation source the Web UI was missing.

The original UI-2 timeline had no source for the real conversation: it only showed
(a) messages optimistically typed in the web app this load, (b) live SSE
operational events, and (c) one ``lastSummary`` line. So a session opened from
Telegram looked empty. This module reconstructs the real per-turn conversation
from what is actually on disk, with NO FastAPI dependency (pure, unit-testable),
mirroring ``artifacts.py``.

Sources, in order of richness:
  1. ``results/task_*.json`` artifacts whose ``session_id`` matches — each is ONE
     turn: the user instruction (``task.title`` / source) → the assistant result
     text (via the existing ``result_text.extract_text_from_payload``), timestamped.
  2. ``state/summaries/<id>.md`` — the clean "Last instruction / Last result (tail)"
     Telegram shows; used as the authoritative latest turn (artifact ``content`` is
     often a raw backend stream, the summary is the human-readable distillation).

SECURITY: ``session_id`` is confined exactly like ``artifacts._artifact_path`` /
the SPA resolver — a ``..`` or absolute path resolves to None, never an arbitrary
read.
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


def _instruction_from_artifact(art: Dict[str, Any]) -> str:
    """The user-side text of a turn. ``task.title`` is the instruction Telegram set
    (often prefixed 'Task: '); fall back to a few known fields."""
    task = art.get("task") or {}
    title = (task.get("title") or "").strip()
    if title.lower().startswith("task:"):
        title = title[5:].strip()
    if title:
        return title
    for k in ("instruction", "objective", "prompt"):
        v = (art.get(k) or "").strip() if isinstance(art.get(k), str) else ""
        if v:
            return v
    return ""


def _result_text_from_artifact(art: Dict[str, Any]) -> str:
    """Clean, user-visible result text for a turn, reusing the same extractor the
    Telegram reply path uses. Falls back to a short failure note on error turns."""
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
    if text:
        return text.strip()
    # No clean text (timeout / killed / empty) — be HONEST about it, don't fabricate.
    if not art.get("success", False):
        errs = art.get("errors") or []
        if errs:
            return f"(no output — {errs[0]})"
        return "(task failed — no output)"
    return ""


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


def _turn_from_artifact(art: Dict[str, Any], path: Path) -> Dict[str, Any]:
    task_id = art.get("task_id") or path.stem
    return {
        "task_id": task_id,
        "timestamp": art.get("timestamp") or "",
        "success": bool(art.get("success")),
        "instruction": _instruction_from_artifact(art),
        "result": _result_text_from_artifact(art),
        "file_count": len(art.get("file_changes") or art.get("files_modified") or []),
        "usage": _usage_from_artifact(art),
    }


def _parse_summary_md(text: str) -> Dict[str, str]:
    """Pull the clean ``## Last instruction`` + ``## Last result (tail)`` sections
    out of a ``state/summaries/<id>.md`` (orchestrator._write_session_summary).

    This is the SAME clean text Telegram shows — the backend already ran the raw
    payload through result_text extraction before writing it here (so opencode's
    JSON-lines stream is already collapsed to prose, and the user input is the
    FULL message, not the truncated artifact task.title). We prefer this over the
    artifact for the latest turn."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        if raw.startswith("## "):
            current = raw[3:].strip().lower()
            sections[current] = []
        elif current is not None:
            sections[current].append(raw)

    def _val(key: str) -> str:
        body = "\n".join(sections.get(key, [])).strip()
        return "" if body == "(none)" else body

    return {
        "instruction": _val("last instruction"),
        "result": _val("last result (tail)"),
    }


def get_transcript(
    results_dir: Path,
    summaries_dir: Path,
    session_id: str,
    limit: int = 50,
) -> Optional[List[Dict[str, Any]]]:
    """Reconstruct a session's conversation as an oldest→newest list of turns.

    Returns None only on a path-traversal attempt (so the endpoint can 404).
    A session with no artifacts yet returns ``[]`` (a real, empty conversation).
    Each turn: ``{task_id, timestamp, success, instruction, result, file_count}``.
    """
    # Confinement check on the session id itself (used to match, and to read the .md).
    if _confined(summaries_dir, session_id, ".md") is None:
        return None
    if not results_dir.is_dir():
        return []

    turns: List[Dict[str, Any]] = []
    for p in results_dir.glob("*.json"):
        if p.name == _INDEX_NAME or not p.is_file():
            continue
        art = _read_json(p)
        if art is None:
            continue
        # session_id lives nested under "session" in the artifact schema
        # (verified against disk: 481/481 artifacts use session.session_id, none
        # top-level). Accept the top-level too in case the schema ever flattens.
        art_sid = (art.get("session") or {}).get("session_id") or art.get("session_id")
        if art_sid != session_id:
            continue
        turns.append(_turn_from_artifact(art, p))

    # Oldest→newest by timestamp (a conversation reads top-to-bottom). Stable.
    turns.sort(key=lambda t: t.get("timestamp") or "")
    if limit and len(turns) > limit:
        turns = turns[-limit:]  # keep the most recent N

    # The summary .md holds the FULL user message (artifacts truncate the
    # instruction to task.title), so it's authoritative for the latest turn's
    # *instruction*. Its result, however, is only the TAIL ("## Last result
    # (tail)" — session.last_result_summary), so it must NEVER overwrite a full
    # artifact result: doing so showed the user only the end of the model's reply.
    # Rule:
    #   - instruction: always prefer the summary (it's the full text).
    #   - result: only use the summary tail as a FALLBACK when the artifact turn
    #     produced no clean result text (e.g. an in-flight first turn with no
    #     artifact yet, or an artifact whose result extraction came back empty).
    # And if there are no artifact turns at all, synthesize one from the summary.
    summary_path = _confined(summaries_dir, session_id, ".md")
    if summary_path is not None and summary_path.is_file():
        try:
            summ = _parse_summary_md(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summ = {"instruction": "", "result": ""}
        if summ.get("instruction") or summ.get("result"):
            if turns:
                latest = turns[-1]
                if summ.get("instruction"):
                    latest["instruction"] = summ["instruction"]
                # Only fall back to the tail when the artifact had no full result.
                if summ.get("result") and not latest.get("result"):
                    latest["result"] = summ["result"]
            else:
                turns.append({
                    "task_id": "summary",
                    "timestamp": "",
                    "success": True,
                    "instruction": summ.get("instruction", ""),
                    "result": summ.get("result", ""),
                    "file_count": 0,
                })

    return turns
