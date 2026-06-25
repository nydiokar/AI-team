"""Artifact reader — the file/diff source for UI-4 (Files & artifacts).

Pure helpers over the on-disk ``results/<task_id>.json`` artifacts, with NO
FastAPI dependency so they are unit-testable in isolation. ``control_api`` wraps
these in two authenticated GET endpoints.

What's modeled is exactly what the artifacts actually carry (verified against disk
2026-06-24): the universal ``files_modified`` string array, the timestamp/success
header, and the richer optional ``file_changes`` array (per-file change_type +
line counts, present in a minority of artifacts). Nothing is invented — there is no
stored unified-diff hunk or file content, so neither is surfaced (raw stdout/stderr
belong to UI-5 logs, not here).

SECURITY: the single-artifact read confines ``task_id`` to ``results_dir`` exactly
like the SPA file resolver (``control_api._mount_web_ui``) — an ``id`` containing
``..`` or an absolute path that would escape the directory resolves to None rather
than reading an arbitrary file.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The orchestrator's task_id → path map; not an artifact itself, so it is skipped
# by the directory scan.
_INDEX_NAME = "index.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # malformed / partial write — skip, never crash the list
        logger.debug("artifact_read_failed path=%s err=%s", path, e)
        return None


def _artifact_path(results_dir: Path, task_id: str) -> Optional[Path]:
    """Resolve ``results_dir/<task_id>.json`` confined to ``results_dir``.

    Returns None on any path-traversal / absolute-path escape. Mirrors the
    confinement check in control_api._mount_web_ui's SPA file resolver.
    """
    base = results_dir.resolve()
    candidate = (results_dir / f"{task_id}.json").resolve()
    if candidate == base or base in candidate.parents:
        return candidate
    return None


def _session_id_of(artifact: Dict[str, Any]) -> Optional[str]:
    """Artifacts carry the session id nested under ``session`` (verified against
    disk: none store it top-level). Accept the top-level too, in case the schema
    ever flattens."""
    return (artifact.get("session") or {}).get("session_id") or artifact.get("session_id")


def _summary(artifact: Dict[str, Any], artifact_path: Path) -> Dict[str, Any]:
    files = artifact.get("files_modified") or []
    file_changes = artifact.get("file_changes") or []
    return {
        "task_id": artifact.get("task_id") or artifact_path.stem,
        "artifact_path": str(artifact_path),
        "success": bool(artifact.get("success")),
        "timestamp": artifact.get("timestamp") or "",
        "file_count": len(file_changes) if file_changes else len(files),
        "files_modified": list(files),
        "has_changes": bool(file_changes) or bool(files),
        "session_id": _session_id_of(artifact),
        "parent_task_id": artifact.get("parent_task_id"),
    }


def list_artifacts(results_dir: Path, limit: int = 50) -> List[Dict[str, Any]]:
    """Newest-first summaries of ``results/*.json`` (excluding ``index.json``).

    Each entry is the shallow header the artifact list renders; full per-file
    detail comes from :func:`get_artifact`. Bounded by ``limit`` AFTER the
    mtime sort so the newest are kept. Unreadable files are skipped, not fatal.
    """
    if not results_dir.is_dir():
        return []
    files = [
        p for p in results_dir.glob("*.json")
        if p.name != _INDEX_NAME and p.is_file()
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    out: List[Dict[str, Any]] = []
    for p in files[: max(0, limit)]:
        artifact = _read_json(p)
        if artifact is None:
            continue
        out.append(_summary(artifact, p))
    return out


def get_artifact(results_dir: Path, task_id: str) -> Optional[Dict[str, Any]]:
    """Full per-file detail for one artifact, or None (escape / missing / bad).

    Surfaces the header + the change detail the card/diff view needs; raw
    stdout/stderr are intentionally omitted (UI-5).
    """
    path = _artifact_path(results_dir, task_id)
    if path is None or not path.is_file():
        return None
    artifact = _read_json(path)
    if artifact is None:
        return None
    return {
        "task_id": artifact.get("task_id") or path.stem,
        "success": bool(artifact.get("success")),
        "timestamp": artifact.get("timestamp") or "",
        "execution_time": artifact.get("execution_time"),
        "errors": list(artifact.get("errors") or []),
        "files_modified": list(artifact.get("files_modified") or []),
        "file_changes": artifact.get("file_changes") or None,
        "session_id": _session_id_of(artifact),
        "parent_task_id": artifact.get("parent_task_id"),
    }


def to_remote_files(artifact: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize an artifact's changes to ``RemoteFile`` rows.

    Prefers the rich ``file_changes`` (carries change_type + line counts); falls
    back to the flat ``files_modified`` string array as ``modified`` with unknown
    line counts (we do NOT fabricate added/deleted without data).
    """
    file_changes = artifact.get("file_changes") or []
    if file_changes:
        return [
            {
                "path": fc.get("path", ""),
                "change": _normalize_change(fc.get("change_type"), fc.get("git_status")),
                "added": fc.get("added_lines"),
                "deleted": fc.get("deleted_lines"),
            }
            for fc in file_changes
            if isinstance(fc, dict)
        ]
    return [
        {"path": p, "change": "modified", "added": None, "deleted": None}
        for p in (artifact.get("files_modified") or [])
    ]


def _normalize_change(change_type: Optional[str], git_status: Optional[str]) -> str:
    """Map an artifact change_type / git porcelain status to added|modified|deleted."""
    ct = (change_type or "").lower()
    if ct in ("added", "untracked", "new"):
        return "added"
    if ct in ("deleted", "removed"):
        return "deleted"
    if ct == "modified":
        return "modified"
    gs = (git_status or "").strip()
    if gs in ("??", "A"):
        return "added"
    if gs == "D":
        return "deleted"
    return "modified"
