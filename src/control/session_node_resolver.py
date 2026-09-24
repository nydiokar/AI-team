"""Resolve an unpinned session create onto a mesh node that owns the repo."""
from __future__ import annotations

import json
import logging
import posixpath
import sqlite3
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def _normalise_remote_path(path: str) -> str:
    return posixpath.normpath(path.strip())


def _repo_paths_match(repos: object, repo_path: str) -> bool:
    if not isinstance(repos, list):
        return False
    target: str = _normalise_remote_path(repo_path)
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        raw_path = repo.get("path")
        if isinstance(raw_path, str) and _normalise_remote_path(raw_path) == target:
            return True
    return False


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _db_row_matches(row: Mapping[str, object], *, backend: str, repo_path: str) -> bool:
    if row.get("status") != "online":
        return False
    backends = _json_list(row.get("backends"))
    if backend not in [b for b in backends if isinstance(b, str)]:
        return False
    return _repo_paths_match(_json_list(row.get("repos")), repo_path)


def resolve_unpinned_session_node(*, backend: str, repo_path: str) -> str | None:
    """Return an online capable node that advertises ``repo_path``, if any.

    This is deliberately narrower than generic task routing: session creation is
    where local repo-path validation happens, so an unpinned create may only skip
    local validation when the mesh has concrete evidence that a remote node owns
    the requested path.
    """
    backend_name: str = (backend or "").strip().lower()
    repo: str = (repo_path or "").strip()
    if not backend_name or not repo:
        return None

    try:
        from config import config
        if not config.mesh.enabled:
            return None
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.warning("event=session_node_resolve_config_failed err=%s", exc)
        return None

    try:
        from src.control.node_registry import get_registry

        registry = get_registry()
        if not registry.is_empty():
            fresh_available: list[tuple[float, int, str]] = []
            unknown: list[tuple[int, str]] = []
            for index, node in enumerate(registry.list_capable(backend_name)):
                if not _repo_paths_match(node.capabilities.repos, repo):
                    continue
                slots = registry._fresh_slot_snapshot(
                    node,
                    getattr(config.mesh, "routing_live_state_max_age_sec", 90),
                )
                if slots is None:
                    unknown.append((index, node.node_id))
                    continue
                used, total = slots
                if used < total:
                    fresh_available.append((used / total, index, node.node_id))
            if fresh_available:
                fresh_available.sort(key=lambda item: (item[0], item[1]))
                return fresh_available[0][2]
            if unknown:
                return unknown[0][1]
            return None
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.warning("event=session_node_resolve_registry_failed err=%s", exc)

    try:
        from src.control.db import get_db

        db = get_db()
        if db is None:
            return None
        for row in db.list_nodes(status="online"):
            if _db_row_matches(row, backend=backend_name, repo_path=repo):
                node_id = row.get("node_id")
                return node_id if isinstance(node_id, str) and node_id else None
    except (AttributeError, ImportError, sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("event=session_node_resolve_db_failed err=%s", exc)
    return None
