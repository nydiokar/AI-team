"""
In-memory node registry for the task server.

Nodes register on startup, send heartbeats every 30s, and deregister on clean shutdown.
The registry marks nodes offline after `node_heartbeat_timeout_sec` seconds of silence
and persists state to the DB so the /nodes Telegram command works after restart.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class NodeCapabilities:
    backends: List[str] = field(default_factory=list)
    max_concurrent: int = 2
    projects_root: str = ""
    repos: List[dict] = field(default_factory=list)  # [{name, path}] snapshot from worker


@dataclass
class NodeInfo:
    node_id: str
    tailscale_ip: str
    api_port: int
    capabilities: NodeCapabilities
    status: str = "online"                  # online | offline
    last_heartbeat: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    live_state: Optional[dict] = None       # last heartbeat snapshot: slots, active_tasks

    @classmethod
    def from_dict(cls, d: dict) -> "NodeInfo":
        caps_raw = d.get("capabilities") or {}
        caps = NodeCapabilities(
            backends=list(caps_raw.get("backends") or []),
            max_concurrent=int(caps_raw.get("max_concurrent") or 2),
            projects_root=caps_raw.get("projects_root") or "",
            repos=list(caps_raw.get("repos") or []),
        )
        return cls(
            node_id=d["node_id"],
            tailscale_ip=d.get("tailscale_ip") or "",
            api_port=int(d.get("api_port") or 9001),
            capabilities=caps,
        )

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "tailscale_ip": self.tailscale_ip,
            "api_port": self.api_port,
            "capabilities": {
                "backends": self.capabilities.backends,
                "max_concurrent": self.capabilities.max_concurrent,
                "projects_root": self.capabilities.projects_root,
                "repos": self.capabilities.repos,
            },
            "status": self.status,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "registered_at": self.registered_at.isoformat() if self.registered_at else None,
            "live_state": self.live_state,
        }


class NodeRegistry:
    """Thread-safe in-memory registry of connected worker nodes.

    The background expiry loop runs inside the asyncio event loop that called
    `start()`. All public methods are safe to call from async context.
    """

    def __init__(self, heartbeat_timeout_sec: int = 90, notify_callback=None) -> None:
        self._nodes: Dict[str, NodeInfo] = {}
        self._timeout_sec = heartbeat_timeout_sec
        # Optional async callable(node_id, failed_tasks) for Telegram notification
        self._notify = notify_callback
        self._expiry_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start background heartbeat expiry loop."""
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._expiry_loop())

    def stop(self) -> None:
        if self._expiry_task and not self._expiry_task.done():
            self._expiry_task.cancel()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, info: NodeInfo) -> None:
        now = datetime.now(tz=timezone.utc)
        info.status = "online"
        info.last_heartbeat = now
        info.registered_at = now
        self._nodes[info.node_id] = info
        self._db_upsert(info)
        # Sweep any claims from the previous process incarnation back to pending.
        # A re-registering node means a new process started (e.g. PM2 restart);
        # its predecessor was hard-killed without releasing claims.
        released = self._db_release_node_claims(info.node_id)
        if released:
            logger.warning(
                "event=orphaned_claims_released node_id=%s count=%d task_ids=%s",
                info.node_id, len(released), released,
            )
        logger.info("event=node_registered node_id=%s ip=%s", info.node_id, info.tailscale_ip)

    def deregister(self, node_id: str) -> None:
        node = self._nodes.pop(node_id, None)
        if node:
            self._db_mark_offline(node_id)
            logger.info("event=node_deregistered node_id=%s", node_id)

    def heartbeat(self, node_id: str, live_state: Optional[dict] = None) -> bool:
        """Update last_heartbeat and optional live_state. Returns False if node is unknown."""
        node = self._nodes.get(node_id)
        if node is None:
            return False
        node.last_heartbeat = datetime.now(tz=timezone.utc)
        node.status = "online"
        if live_state is not None:
            node.live_state = live_state
        self._db_heartbeat(node_id, live_state)
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, node_id: str) -> Optional[NodeInfo]:
        return self._nodes.get(node_id)

    def list_all(self) -> List[NodeInfo]:
        return list(self._nodes.values())

    def is_empty(self) -> bool:
        return not bool(self._nodes)

    def pick_capable(self, backend: str) -> Optional[NodeInfo]:
        """Return the first online node that supports `backend`. Round-robin is future work."""
        for node in self._nodes.values():
            if node.status == "online" and backend in node.capabilities.backends:
                return node
        return None

    # ------------------------------------------------------------------
    # Expiry loop
    # ------------------------------------------------------------------

    async def _expiry_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await self._expire_stale_nodes()
        except asyncio.CancelledError:
            pass

    async def _expire_stale_nodes(self) -> None:
        now = datetime.now(tz=timezone.utc)
        for node in list(self._nodes.values()):
            if node.status != "online":
                continue
            if node.last_heartbeat is None:
                continue
            age = (now - node.last_heartbeat).total_seconds()
            if age > self._timeout_sec:
                node.status = "offline"
                self._db_mark_offline(node.node_id)
                logger.warning("event=node_offline node_id=%s age_s=%.0f", node.node_id, age)
                # Check for tasks that were claimed by this node and failed.
                # Run off-thread — these are blocking sqlite calls and would
                # otherwise stall the event loop (and FastAPI request handling).
                failed = await asyncio.to_thread(self._fail_offline_tasks, node.node_id)
                if self._notify and failed:
                    try:
                        await self._notify(node.node_id, failed)
                    except Exception as e:
                        logger.warning("event=notify_failed node_id=%s err=%s", node.node_id, e)

    def _fail_offline_tasks(self, node_id: str) -> List[str]:
        """Mark claimed tasks for an offline node as failed_node_offline. Returns task ids."""
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return []
            rows = db.list_tasks(status="claimed")
            failed = []
            for row in rows:
                if row.get("claimed_by") == node_id:
                    db.fail_task(row["id"], f"node {node_id} went offline", status="failed_node_offline")
                    failed.append(row["id"])
            return failed
        except Exception as e:
            logger.warning("event=fail_offline_tasks_err node_id=%s err=%s", node_id, e)
            return []

    # ------------------------------------------------------------------
    # DB helpers — best-effort, never raise
    # ------------------------------------------------------------------

    def _db_upsert(self, node: NodeInfo) -> None:
        try:
            from src.control.db import get_db
            db = get_db()
            if db:
                db.upsert_node(
                    node_id=node.node_id,
                    tailscale_ip=node.tailscale_ip,
                    api_port=node.api_port,
                    backends=node.capabilities.backends,
                    max_concurrent=node.capabilities.max_concurrent,
                    status="online",
                    projects_root=node.capabilities.projects_root,
                    repos=node.capabilities.repos,
                )
        except Exception as e:
            logger.debug("event=db_node_upsert_err node_id=%s err=%s", node.node_id, e)

    def _db_heartbeat(self, node_id: str, live_state: Optional[dict] = None) -> None:
        try:
            from src.control.db import get_db
            db = get_db()
            if db:
                db.heartbeat_node(
                    node_id,
                    live_state=json.dumps(live_state) if live_state is not None else None,
                )
        except Exception as e:
            logger.debug("event=db_heartbeat_err node_id=%s err=%s", node_id, e)

    def _db_mark_offline(self, node_id: str) -> None:
        try:
            from src.control.db import get_db
            db = get_db()
            if db:
                db.mark_node_offline(node_id)
        except Exception as e:
            logger.debug("event=db_mark_offline_err node_id=%s err=%s", node_id, e)

    def _db_release_node_claims(self, node_id: str) -> list:
        try:
            from src.control.db import get_db
            db = get_db()
            if db:
                return db.release_node_claims(node_id)
        except Exception as e:
            logger.debug("event=db_release_node_claims_err node_id=%s err=%s", node_id, e)
        return []


# ---------------------------------------------------------------------------
# Module-level singleton — imported by task_server and orchestrator
# ---------------------------------------------------------------------------

_registry: Optional[NodeRegistry] = None


def get_registry() -> NodeRegistry:
    """Return the singleton NodeRegistry, creating it on first call."""
    global _registry
    if _registry is None:
        try:
            from config import config as _cfg
            timeout = _cfg.mesh.node_heartbeat_timeout_sec
        except Exception:
            timeout = 90
        _registry = NodeRegistry(heartbeat_timeout_sec=timeout)
    return _registry
