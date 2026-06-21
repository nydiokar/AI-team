"""
Repo inspection operations — the canonical, machine-local implementations.

These are the read-only (and commit) operations that a Telegram command needs
to run *against a session's repo*. The repo lives on whichever node owns the
session, so the gateway must never assume it can touch the filesystem directly.

This module is the single source of truth for *what* those operations do and
*how their results are shaped*. It is deliberately transport-agnostic:

  - On a worker node, `run_inspect_op` is called locally by the worker daemon
    when it picks up an `action == "inspect"` mesh task.
  - On the gateway, the same function is called directly for sessions that live
    on this host (no mesh, or a `__local__` session), so local and remote paths
    produce byte-identical result shapes.

The allowed op vocabulary is closed (mirrors the CodingBackend action-allowlist
discipline in AGENT_MESH_SPEC §4.3): no raw shell, no arbitrary paths beyond the
repo the caller already owns.

Result contract
---------------
Every op returns a JSON-serialisable dict. On failure the dict carries an
``error`` string instead of raising, so the same value survives the DB round
trip and reaches the Telegram handler unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Closed set of inspection operations. Adding one requires adding it here first.
INSPECT_OPS = ("list_dirs", "git_status", "commit", "commit_all")


def run_inspect_op(op: str, repo_path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Execute one inspection op against `repo_path` on the local filesystem.

    Returns a JSON-serialisable dict. Always returns — never raises — so the
    result can travel over the mesh DB and be rendered the same way whether it
    ran here or on a remote worker.
    """
    params = params or {}
    if op not in INSPECT_OPS:
        return {"error": f"Unknown inspect op: {op!r}"}

    try:
        if op == "list_dirs":
            return _list_dirs(repo_path, params)
        if op == "git_status":
            return _git_status(repo_path)
        if op in ("commit", "commit_all"):
            return _commit(op, repo_path, params)
    except Exception as e:  # defensive — ops below already guard, this is the floor
        return {"error": f"{type(e).__name__}: {e}"}
    return {"error": f"Unhandled inspect op: {op!r}"}


def _list_dirs(repo_path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import os
    from src.services.path_resolver import PathResolver

    limit = int(params.get("limit", 12))
    include_hidden = bool(params.get("include_hidden", False))
    sort_by_recent = bool(params.get("sort_by_recent", True))

    # Optional sub-path, constrained to the repo the caller already owns so a
    # crafted param can't walk the worker's filesystem outside the session repo.
    target = params.get("path") or repo_path
    if repo_path:
        try:
            real_repo = os.path.realpath(repo_path)
            real_target = os.path.realpath(target)
            if real_target != real_repo and not real_target.startswith(real_repo + os.sep):
                return {"error": "Path is outside the session repository."}
            target = real_target
        except Exception:
            target = repo_path

    # A bare PathResolver (no roots) is fine here: the path is already inside the
    # repo the caller owns, and list_child_directories does its own checks.
    dirs = PathResolver().list_child_directories(
        target,
        limit=limit,
        include_hidden=include_hidden,
        sort_by_recent=sort_by_recent,
    )
    return {"dirs": dirs, "path": target}


def _git_status(repo_path: str) -> Dict[str, Any]:
    from src.services.git_automation import GitAutomationService

    return GitAutomationService(repo_path).get_git_status_summary()


def _commit(op: str, repo_path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    from src.services.git_automation import GitAutomationService

    git_service = GitAutomationService(repo_path)
    kwargs = dict(
        task_id=params.get("task_id", ""),
        task_description=params.get("task_description", ""),
        create_branch=bool(params.get("create_branch", False)),
        push_branch=bool(params.get("push_branch", False)),
    )
    if op == "commit":
        return git_service.safe_commit_task(**kwargs)
    return git_service.commit_all_staged(**kwargs)
