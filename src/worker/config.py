"""
Worker daemon configuration — read from environment variables.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass
class WorkerConfig:
    node_id: str
    worker_token: str
    tailscale_ip: str
    controller_url: str
    backends: List[str]
    api_port: int = 9001
    max_concurrent: int = 2
    projects_root: str = ""
    accept_unpinned: bool = True
    # EXPLICIT transport semantics (never inferred from network identity):
    # True only for a legacy single-host deployment where this worker writes
    # DIRECTLY into the controller's events.ndjson (same process / shared
    # filesystem). Under Docker the worker and controller are separate
    # containers with separate volumes even on the same host, so this stays
    # False and activity is always forwarded over the explicit HTTP interface.
    shares_controller_fs: bool = False
    # Harness-side quota observation: the controller container has no Claude
    # binary/credentials, so quota telemetry is read here (where the harness
    # lives) and shipped to the controller. Free control request, not a turn.
    quota_observe_enabled: bool = False
    quota_observe_interval_sec: int = 300
    # Window warming (prewarm) MUST run where Claude executes — the worker —
    # because opening a 5h window costs a real model turn the controller
    # container cannot spend. Gated by QUOTA_PREWARM_ENABLED and a claude backend.
    quota_prewarm_enabled: bool = False

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        node_id = os.environ["WORKER_NODE_ID"]
        token = os.environ["WORKER_TOKEN"]
        tailscale_ip = os.environ["WORKER_TAILSCALE_IP"]
        controller_url = os.environ["CONTROLLER_URL"].rstrip("/")
        raw_backends = os.environ["WORKER_BACKENDS"]
        backends = [b.strip() for b in raw_backends.split(",") if b.strip()]
        api_port = int(os.getenv("WORKER_API_PORT") or 9001)
        max_concurrent = int(os.getenv("WORKER_MAX_CONCURRENT") or 2)
        projects_root = os.getenv("WORKER_PROJECTS_ROOT", "")
        accept_unpinned = (
            os.getenv("WORKER_ACCEPT_UNPINNED", "true").strip().lower()
            not in _FALSE
        )
        shares_controller_fs = (
            os.getenv("WORKER_SHARES_CONTROLLER_FS", "").strip().lower() in _TRUE
        )

        # Quota observation defaults ON for a claude-capable worker whenever the
        # controller's coordinator is enabled; WORKER_QUOTA_OBSERVE forces it
        # either way for per-node control.
        coordinator_on = (
            os.getenv("QUOTA_COORDINATOR_ENABLED", "").strip().lower() in _TRUE
        )
        observe_override = os.getenv("WORKER_QUOTA_OBSERVE", "").strip().lower()
        if observe_override in _TRUE:
            quota_observe_enabled = True
        elif observe_override in _FALSE:
            quota_observe_enabled = False
        else:
            quota_observe_enabled = coordinator_on and ("claude" in backends)
        quota_observe_interval_sec = int(os.getenv("QUOTA_OBSERVE_INTERVAL_SEC") or 300)

        # Warming can only fire from a claude-capable harness. Enable it here
        # (execution side) when the flag is on, regardless of the ingest_only
        # controller — this is the fix for warming going inert under Docker.
        prewarm_flag = os.getenv("QUOTA_PREWARM_ENABLED", "").strip().lower() in _TRUE
        quota_prewarm_enabled = prewarm_flag and ("claude" in backends)

        return cls(
            node_id=node_id,
            worker_token=token,
            tailscale_ip=tailscale_ip,
            controller_url=controller_url,
            backends=backends,
            api_port=api_port,
            max_concurrent=max_concurrent,
            projects_root=projects_root,
            accept_unpinned=accept_unpinned,
            shares_controller_fs=shares_controller_fs,
            quota_observe_enabled=quota_observe_enabled,
            quota_observe_interval_sec=quota_observe_interval_sec,
            quota_prewarm_enabled=quota_prewarm_enabled,
        )

    def list_repos(self) -> List[dict]:
        """Scan projects_root and return [{name, path}] for each subdirectory."""
        if not self.projects_root:
            return []
        try:
            root = Path(self.projects_root).resolve()
            children = sorted(
                (c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".")),
                key=lambda c: c.stat().st_mtime,
                reverse=True,
            )
            return [{"name": c.name, "path": str(c)} for c in children[:20]]
        except Exception:
            return []
