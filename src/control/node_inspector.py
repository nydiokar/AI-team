"""
Gateway-side router for repo inspection ops.

The problem this solves: read-only repo commands (/session_dirs, /git_status,
/commit, ...) used to run on the gateway's own filesystem. After the mesh split
the gateway runs on the VPS while the repo lives on a worker node, so those
commands silently reported "no directories" / "not a git repository" — they
were inspecting the wrong machine.

`NodeInspector` makes the gateway canonical: every inspection runs against the
node that *owns the session*, never against whatever box the gateway happens to
run on.

Routing rule
------------
A session is "remote" iff its ``machine_id`` matches an online node in the
registry. In that case the op is dispatched as an ``inspect`` mesh task pinned
to that node (the worker picks it up, runs it locally, posts the result) and we
poll the DB for the result. Otherwise — no mesh, a ``__local__`` session, or a
``machine_id`` that is simply this host — the op runs locally, exactly as
before. This is self-correcting across the VPS migration: it follows wherever
the owning worker actually is.

Honesty floor
-------------
If a session is pinned to a node that is *not* online, we do not fall back to
the local filesystem (that is what produced the misleading output in the first
place). We return an explicit error naming the offline node.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Inspection ops are fast and local on the worker. If a worker does not pick the
# task up within this window, something is wrong (offline mid-flight, overloaded
# poll loop) — surface it rather than hanging the Telegram command.
_INSPECT_TIMEOUT_SEC = 30
_POLL_INTERVAL_SEC = 1.0


class InspectError(Exception):
    """Raised when an inspection cannot be completed (offline node, timeout, DB down)."""


def nudge_node_direct(node_id: str, db: Any) -> bool:
    """POST /nudge to a worker node directly over Tailscale. Returns True on success.

    Canonical implementation — shared by :class:`NodeInspector` and the
    orchestrator so the HTTP nudge logic lives in exactly one place.  Callers
    that need an awaitable wrapper use :func:`_nudge_worker`; sync callers
    (e.g. ``asyncio.to_thread`` contexts in the orchestrator) call this directly.
    """
    import urllib.request

    try:
        row = db.get_node(node_id)
        if not row:
            return False
        tailscale_ip = row.get("tailscale_ip") or ""
        api_port = row.get("api_port") or 0
        if not tailscale_ip or not api_port:
            logger.debug("event=nudge_skipped node_id=%s reason=no_address", node_id)
            return False
        url = f"http://{tailscale_ip}:{api_port}/nudge"
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=2):
            pass
        logger.debug("event=nudge_sent node_id=%s", node_id)
        return True
    except Exception as e:
        logger.debug("event=nudge_failed node_id=%s err=%s", node_id, e)
        return False


async def _nudge_worker(node_id: str, db: Any) -> None:
    """Fire-and-forget async wrapper: wake the worker after an inspect enqueue.

    Eliminates the worker's adaptive backoff (up to 30 s on an idle node).
    Failures are swallowed — the poll loop handles the case where the nudge
    doesn't arrive.
    """
    await asyncio.to_thread(nudge_node_direct, node_id, db)


def session_node(session: Any) -> Optional[str]:
    """Return the node_id a session is pinned to, or None if it runs locally.

    Canonical "where does this session's repo live?" predicate, shared by every
    gateway code path that must decide local vs. remote (inspection, uploads).

    A session is remote iff mesh is enabled and its ``machine_id`` matches a
    *registered* node (online OR offline). A ``machine_id`` that is just this
    host's hostname is not a registered node → local. This is what makes the
    decision survive the VPS migration: it tracks the registry, not the gateway
    process's hostname.
    """
    machine_id = getattr(session, "machine_id", "") or ""
    if not machine_id:
        return None
    try:
        from config import config
        if not config.mesh.enabled:
            return None
        from src.control.node_registry import get_registry
        node = get_registry().get(machine_id)
    except Exception:
        return None
    return machine_id if node is not None else None


class NodeInspector:
    """Runs repo inspection ops against the node that owns a session."""

    def is_remote(self, session: Any) -> Optional[str]:
        """Return the owning node_id if the session lives on an online remote
        node, else None (meaning: run locally). Raises InspectError when the
        owning node is registered but offline — the honesty floor.
        """
        node_id = session_node(session)
        if node_id is None:
            return None
        from src.control.node_registry import get_registry
        node = get_registry().get(node_id)
        if node is None or node.status != "online":
            raise InspectError(
                f"Session lives on node '{node_id}', which is offline. "
                "Its filesystem can't be read until it reconnects."
            )
        return node_id

    async def run(self, session: Any, op: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute an inspection op for `session`. Returns the op result dict.

        Raises InspectError on offline node / dispatch failure / timeout.
        """
        params = params or {}
        repo_path = getattr(session, "repo_path", "") or ""

        node_id = self.is_remote(session)  # may raise InspectError for offline node
        if node_id is None:
            # Local path — byte-identical to the worker path via the shared module.
            from src.services.inspect_ops import run_inspect_op
            return await asyncio.to_thread(run_inspect_op, op, repo_path, params)

        return await self._run_remote(session, node_id, op, repo_path, params)

    async def _run_remote(
        self,
        session: Any,
        node_id: str,
        op: str,
        repo_path: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        from src.control.db import get_db

        db = get_db()
        if db is None:
            raise InspectError("Mesh DB unavailable; cannot reach the worker node.")

        # mesh_tasks.session_id is a FK into sessions; if the session isn't
        # mirrored yet the enqueue would be silently dropped. Ensure it exists
        # so remote inspection never depends on a prior shadow-write having run.
        try:
            if getattr(session, "session_id", "") and db.get_session(session.session_id) is None:
                db.upsert_session(session)
        except Exception:
            pass

        task_id = f"inspect_{uuid.uuid4().hex[:12]}"
        payload = {
            "task_id": task_id,
            "action": "inspect",
            "session": {
                "session_id": getattr(session, "session_id", ""),
                "repo_path": repo_path,
                "machine_id": node_id,
                "backend": getattr(session, "backend", "claude"),
            },
            "metadata": {"op": op, "repo_path": repo_path, "params": params},
        }
        await asyncio.to_thread(
            db.enqueue_task,
            task_id,
            getattr(session, "session_id", None),
            node_id,                                   # machine_id pin → only this node claims it
            getattr(session, "backend", "claude"),
            "inspect",
            payload,
        )

        # Nudge the worker so it polls immediately instead of waiting out its
        # backoff window (up to 30s on an idle node). Best-effort: failures are
        # logged at DEBUG and we fall through to the normal poll loop.
        asyncio.ensure_future(_nudge_worker(node_id, db))

        deadline = time.time() + _INSPECT_TIMEOUT_SEC
        first = True
        while True:
            row = await asyncio.to_thread(db.get_task, task_id)
            if row is None:
                if first:
                    raise InspectError("Inspect task vanished from the queue before dispatch.")
            else:
                status = row.get("status", "pending")
                if status == "completed":
                    return self._extract_result(row)
                if status in ("failed", "failed_node_offline"):
                    raise InspectError(row.get("error") or f"Inspection {status} on node '{node_id}'.")
            first = False
            if time.time() >= deadline:
                # Mark the task failed so the reaper doesn't loop on it forever.
                try:
                    db.fail_task(task_id, f"inspect timed out waiting for node '{node_id}'", status="failed")
                except Exception:
                    pass
                raise InspectError(
                    f"Node '{node_id}' did not answer the inspection within "
                    f"{_INSPECT_TIMEOUT_SEC}s (offline or busy)."
                )
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    @staticmethod
    def _extract_result(row: Dict[str, Any]) -> Dict[str, Any]:
        import json
        raw = row.get("result")
        try:
            result = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            result = {}
        inspect = result.get("inspect")
        if isinstance(inspect, dict):
            return inspect
        # Worker returned success but no inspect payload — treat as an error so
        # the caller never silently shows empty/stale data.
        return {"error": "Worker returned no inspection result."}


_inspector: Optional[NodeInspector] = None


def get_inspector() -> NodeInspector:
    global _inspector
    if _inspector is None:
        _inspector = NodeInspector()
    return _inspector
