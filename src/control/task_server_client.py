"""
HTTP client the gateway uses to talk to a standalone mesh task server.

State Separation Phase 2: when the task server runs as its own process
(server_main.py) instead of embedded (embedded_server.py), the gateway can no
longer reach the in-process get_registry() singleton. It talks to the server
over HTTP instead, via this client.

Design notes:
  - stdlib urllib only (mirrors src/worker/agent.py `_HTTP`) — no new deps.
  - Bearer auth with WORKER_TOKEN, same as every task-server endpoint.
  - Legacy methods return None / [] on failure rather than raising, because the gateway
    must tolerate the server being unreachable: that condition is precisely what
    triggers fallback mode (Phase 4). Callers distinguish "server said no" from
    "server unreachable" via is_healthy() / the None sentinel.
  - list_nodes() is cached with a short TTL so per-task dispatch doesn't hammer
    the server; this restores cheap node discovery lost by un-embedding.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from pydantic import JsonValue

from src.control.mesh_health import MeshHealth, get_mesh_health

logger = logging.getLogger(__name__)


class TaskServerClient:
    """Synchronous HTTP client for the mesh task server.

    Failures are swallowed and surfaced as None/[]; the gateway treats an
    unreachable server as "mesh unhealthy", not as a crash. The J2 completion
    protocol is explicitly fail-closed: failure must not look like an empty page.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int = 10,
        node_cache_ttl: float = 5.0,
        mesh_health: Optional[MeshHealth] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._node_cache_ttl = node_cache_ttl
        self._node_cache: Optional[List[Dict[str, Any]]] = None
        self._node_cache_at: float = 0.0
        self._mesh_health: MeshHealth = mesh_health or MeshHealth()

    # ------------------------------------------------------------------
    # Low-level transport
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: Any = None, timeout: Optional[int] = None) -> Optional[Any]:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            f"{self._base}{path}", data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.debug("event=task_server_post_failed path=%s err=%s", path, e)
            return None

    def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None
    ) -> Optional[Any]:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.debug("event=task_server_get_failed path=%s err=%s", path, e)
            return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def get_health(self, timeout: int = 5) -> Optional[Dict[str, Any]]:
        """Return the /health body, or None if the server is unreachable."""
        return self._get("/health", timeout=timeout)

    def is_healthy(self, timeout: int = 5) -> bool:
        """True iff the mesh has been consistently healthy (sliding window).

        Each call performs a fresh probe against ``/health`` and feeds the
        result into the internal *MeshHealth* sliding window.  The return
        value is the *smoothed* state — it only flips to ``False`` after
        ``failure_threshold`` consecutive failures, preventing false
        positives from transient network blips.
        """
        body = self.get_health(timeout=timeout)
        healthy = bool(body and body.get("status") == "ok")
        self._mesh_health.record_check(healthy)
        return self._mesh_health.is_healthy()

    def mesh_health_stats(self) -> Dict[str, Any]:
        """Return raw MeshHealth diagnostics (for embedding in /health etc.)."""
        return self._mesh_health.stats()

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def list_nodes(self, *, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Return all registered nodes. Cached for `node_cache_ttl` seconds.

        Returns [] on failure. On a cache miss that fails, the stale cache (if
        any) is returned rather than [] so a brief server blip doesn't make the
        whole mesh look empty.
        """
        now = time.monotonic()
        if use_cache and self._node_cache is not None and (now - self._node_cache_at) < self._node_cache_ttl:
            return self._node_cache

        body = self._get("/nodes")
        if body is None:
            return self._node_cache if self._node_cache is not None else []
        if isinstance(body, list):
            self._node_cache = body
            self._node_cache_at = now
            return body
        return []

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        for n in self.list_nodes():
            if n.get("node_id") == node_id:
                return n
        return None

    def invalidate_node_cache(self) -> None:
        self._node_cache = None
        self._node_cache_at = 0.0

    def nudge(self, node_id: str) -> bool:
        """Ask the server to nudge a worker. True if the server accepted it."""
        resp = self._post(f"/nodes/{node_id}/nudge")
        return bool(resp and resp.get("status") == "nudged")

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def register_job(
        self,
        node_id: str,
        label: str,
        *,
        session_id: Optional[str] = None,
        command: Optional[str] = None,
        cwd: Optional[str] = None,
        attach_pid: Optional[int] = None,
        log_path: Optional[str] = None,
        notify: bool = True,
        notify_agent: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Register a watched job. Returns the job dict on success, None on failure.

        Pass `command` to have the worker spawn it (output captured to log), or
        `attach_pid` to attach to an already-running process (output: log_path only).
        """
        return self._post("/jobs", {
            "node_id": node_id,
            "label": label,
            "session_id": session_id,
            "command": command,
            "cwd": cwd,
            "attach_pid": attach_pid,
            "log_path": log_path,
            "notify": notify,
            "notify_agent": notify_agent,
        })

    def list_jobs(
        self,
        node_id: Optional[str] = None,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        ownership: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit}
        if node_id:
            params["node_id"] = node_id
        if status:
            params["status"] = status
        if session_id:
            params["session_id"] = session_id
        elif ownership:
            params["ownership"] = ownership
        return self._get("/jobs", params) or []

    def list_job_completions(self, after_id: str = "") -> dict[str, JsonValue]:
        """J2 completion protocol: bounded and fail-closed, unlike legacy lists.

        A 404/timeout/malformed body MUST NOT become an empty successful sweep.
        New controller protocol is required before a federated gateway upgrades.
        """
        from .job_completion import JOB_COMPLETION_BATCH, JobCompletionPage

        query = urllib.parse.urlencode({"after_id": after_id, "limit": JOB_COMPLETION_BATCH})
        req = urllib.request.Request(f"{self._base}/jobs/completions?{query}", headers=self._headers())
        with urllib.request.urlopen(req, timeout=self._timeout) as response:
            body = response.read(1048577)
        if len(body) > 1048576:
            raise ValueError("job completion response exceeds 1 MiB")
        page = JobCompletionPage.model_validate_json(body)
        ids = [j.id for j in page.jobs]
        if ids != sorted(set(ids)):
            raise ValueError("job completion page is not strictly ordered")
        if page.jobs and any(j.id <= after_id for j in page.jobs):
            raise ValueError("job completion cursor did not advance")
        expected_cursor = page.jobs[-1].id if len(page.jobs) == JOB_COMPLETION_BATCH else None
        if page.next_cursor != expected_cursor:
            raise ValueError("invalid job completion cursor")
        return page.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return the task row (status + result), or None if unreachable.

        Note: the task server has no dedicated GET /tasks/{id}; the gateway reads
        task status straight from the shared DB today. This method is provided
        for the future where the server fronts all task reads. Until then callers
        should prefer the DB path; see orchestrator recovery.
        """
        return self._get(f"/tasks/{task_id}")
