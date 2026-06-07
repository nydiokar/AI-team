"""
Worker daemon configuration — read from environment variables.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


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
        return cls(
            node_id=node_id,
            worker_token=token,
            tailscale_ip=tailscale_ip,
            controller_url=controller_url,
            backends=backends,
            api_port=api_port,
            max_concurrent=max_concurrent,
            projects_root=projects_root,
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
