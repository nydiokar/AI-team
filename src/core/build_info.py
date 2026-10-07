"""Build identity of the running process: package version + git commit.

The Docker image bakes ``AI_TEAM_GIT_SHA`` at build time (the image has no
``.git``); a worker running from a checkout falls back to ``git rev-parse``.
Cached for the process lifetime: it describes the code that was loaded, not
whatever the checkout was moved to afterwards.
"""
from __future__ import annotations

import os
import subprocess
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel

_DIST_NAME: str = "ai-task-orchestrator"
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
UNKNOWN: str = "unknown"


class BuildInfo(BaseModel):
    version: str
    git_sha: str


def _git_head_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _package_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN


@cache
def build_info() -> BuildInfo:
    env_sha: str = os.environ.get("AI_TEAM_GIT_SHA", "").strip()
    sha: str = env_sha if env_sha and env_sha != UNKNOWN else (_git_head_sha() or UNKNOWN)
    return BuildInfo(version=_package_version(), git_sha=sha)


def node_build_skew(nodes: Iterable[object], expected_sha: str) -> dict[str, int]:
    """Count online nodes whose heartbeat reports a different build (mismatch)
    or none at all (unknown: pre-build-identity worker, or the in-process node)."""
    mismatch: int = 0
    unknown: int = 0
    for node in nodes:
        if getattr(node, "status", None) != "online":
            continue
        sha = (getattr(node, "live_state", None) or {}).get("build_sha")
        if not sha or sha == UNKNOWN:
            unknown += 1
        elif not (sha.startswith(expected_sha) or expected_sha.startswith(sha)):
            mismatch += 1  # prefix match: short SHAs may differ in length
    return {"nodes_build_mismatch": mismatch, "nodes_build_unknown": unknown}
