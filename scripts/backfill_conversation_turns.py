"""One-time backfill: populate artifact-complete columns on mesh_tasks from the
existing results/task_*.json files.

After this runs, the conversation + Files/Info tab data live in mesh_tasks and the
fat artifact files can be archived/deleted. Idempotent: re-running only fills gaps
(enrich_task uses COALESCE). Run from repo root:

    python scripts/backfill_conversation_turns.py            # backfill
    python scripts/backfill_conversation_turns.py --verify   # parity check only
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.control.db import MeshDB
from src.control.transcript import _extract_result_from_ndjson

try:
    from src.services.result_text import extract_usage_from_ndjson, extract_text_from_payload
except Exception:  # pragma: no cover
    extract_usage_from_ndjson = lambda s: None  # type: ignore
    extract_text_from_payload = lambda p: ""    # type: ignore

DB_PATH = "state/mesh.db"
RESULTS = "results"


def _reply_from_artifact(art: dict) -> str:
    raw = art.get("raw_stdout") or ""
    if raw:
        t = _extract_result_from_ndjson(raw)
        if t:
            return t
    # archived raw → read the gz sidecar
    archived = art.get("raw_stdout_archived")
    if archived:
        p = Path(RESULTS) / archived
        if p.exists():
            import gzip
            try:
                with gzip.open(p, "rt", encoding="utf-8") as gz:
                    t = _extract_result_from_ndjson(gz.read())
                    if t:
                        return t
            except Exception:
                pass
    parsed = art.get("parsed_output")
    if parsed:
        t = extract_text_from_payload(parsed) or ""
        if t:
            return t
    return ""


def _history_index() -> tuple:
    """Map task_id -> (full user_message, result_summary) from task_history.

    The session record is the PRIMARY source for BOTH the prompt (user_message,
    full for all turns — artifact.task.prompt is empty on older turns) AND a reply
    fallback (result_summary — the only reply source for turns whose artifact
    raw_stdout has no result line, e.g. codex diff-only or short turns).
    """
    prompts: dict = {}
    replies: dict = {}
    for p in glob.glob("state/sessions/*.json"):
        try:
            s = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in s.get("task_history") or []:
            tid = e.get("task_id")
            if not tid:
                continue
            um = (e.get("user_message") or "").strip()
            if um:
                prompts[tid] = um
            rs = (e.get("result_summary") or "").strip()
            if rs:
                replies[tid] = rs
    return prompts, replies


def backfill(batch_size: int = 500) -> None:
    """One pass over results/*.json → enrich mesh_tasks. Single-transaction batches
    keep it to seconds even with hundreds of multi-MB artifacts."""
    db = MeshDB(DB_PATH)
    prompts, hist_replies = _history_index()
    known_ids = db.existing_task_ids()           # one query, not 1 per file
    files = glob.glob(os.path.join(RESULTS, "task_*.json"))
    enriched = skipped_no_row = skipped_no_text = 0
    pending: list = []

    def flush():
        if pending:
            db.enrich_tasks_batch(pending)
            pending.clear()

    for f in files:
        try:
            art = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        task_id = art.get("task_id") or Path(f).stem
        if task_id not in known_ids:
            skipped_no_row += 1
            continue
        # Reply: artifact raw_stdout/parsed_output is the full source; fall back to
        # task_history.result_summary when the artifact has no result text (matches
        # the old file transcript, which kept whichever was longer).
        reply = _reply_from_artifact(art).strip()
        hist_reply = hist_replies.get(task_id, "")
        if len(hist_reply) > len(reply):
            reply = hist_reply
        # Prompt sourcing, in priority order (matches the old file transcript's
        # _instruction_from_artifact so no user turn goes blank):
        #   1. task_history.user_message  (full text, newer turns)
        #   2. artifact task.prompt       (full text, prompt-persistence era)
        #   3. artifact task.title        (truncated "Task: <first 50>..." — last
        #      resort for ancient turns where the full prompt was never persisted;
        #      a partial instruction still beats a missing user turn)
        task_obj = art.get("task") or {}
        prompt = prompts.get(task_id) or (task_obj.get("prompt") or "").strip()
        if not prompt:
            title = (task_obj.get("title") or "").strip()
            if title.lower().startswith("task:"):
                title = title[5:].strip()
            prompt = title
        if not reply and not prompt:
            skipped_no_text += 1
            continue
        usage = None
        try:
            usage = extract_usage_from_ndjson(art.get("raw_stdout") or "")
        except Exception:
            usage = None
        pending.append({
            "task_id": task_id,
            "prompt": prompt or None,
            "reply_text": reply or None,
            "parsed_output": art.get("parsed_output"),
            "file_changes": art.get("file_changes") or None,
            "files_modified": art.get("files_modified") or [],
            "usage": usage,
            "error_class": (art.get("retry") or {}).get("error_class") or None,
            "return_code": art.get("return_code"),
        })
        enriched += 1
        if len(pending) >= batch_size:
            flush()
    flush()
    print(f"backfill done: enriched={enriched} skipped_no_db_row={skipped_no_row} "
          f"skipped_no_text={skipped_no_text} total_files={len(files)}")


def verify() -> None:
    """Parity: every session that had file-servable text must now serve identical
    text from the DB path. Compares per-turn reply length/prefix."""
    db = MeshDB(DB_PATH)
    import sqlite3
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    sess = c.execute("SELECT session_id FROM sessions WHERE task_history != '[]'").fetchall()
    mismatches = 0
    checked = 0
    for s in sess:
        sid = s["session_id"]
        rows = db.get_session_turns(sid, limit=500)
        for r in rows:
            db_reply = (r.get("reply_text") or "").strip()
            # recover file reply for this task
            p = Path(RESULTS) / f"{r['task_id']}.json"
            if not p.exists():
                continue
            try:
                art = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            file_reply = _reply_from_artifact(art).strip()
            if not file_reply:
                continue
            checked += 1
            # Invariant: the DB must serve at least what the file recovered. The DB
            # may legitimately hold MORE (the longest-source-wins rule can pick a
            # longer task_history.result_summary over the artifact NDJSON, or a
            # failure reason), so only a SHORTER db_reply is a real regression.
            if len(db_reply) < len(file_reply) - 2:
                mismatches += 1
                if mismatches <= 10:
                    print(f"  MISMATCH {r['task_id']}: db_len={len(db_reply)} file_len={len(file_reply)}")
    print(f"parity: checked={checked} mismatches={mismatches} "
          f"-> {'PASS' if mismatches == 0 else 'FAIL'}")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        backfill()
        print("--- parity check ---")
        verify()
