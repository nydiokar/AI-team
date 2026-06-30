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
    live_state_updated_at: Optional[datetime] = None
    incarnation_id: Optional[str] = None   # minted by DB on every register; used by reaper

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
            "live_state_updated_at": self.live_state_updated_at.isoformat() if self.live_state_updated_at else None,
            "incarnation_id": self.incarnation_id,
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
        old_incarnation, new_incarnation = self._db_upsert(info)
        # Sweep claims only when the worker process identity changed. A controller
        # restart also forces workers to re-register, but the worker process is
        # still alive and may still be executing its claimed task.
        if old_incarnation and new_incarnation and old_incarnation != new_incarnation:
            released = self._db_release_node_claims(info.node_id)
            if released:
                logger.warning(
                    "event=orphaned_claims_released node_id=%s old_incarnation=%s new_incarnation=%s count=%d task_ids=%s",
                    info.node_id, old_incarnation, new_incarnation, len(released), released,
                )
            lost_count = self._db_mark_driver_sessions_lost(info.node_id)
            if lost_count:
                logger.warning(
                    "event=driver_sessions_marked_lost node_id=%s old_incarnation=%s new_incarnation=%s count=%d",
                    info.node_id, old_incarnation, new_incarnation, lost_count,
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
            node.live_state_updated_at = node.last_heartbeat
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

    def list_capable(self, backend: str) -> List[NodeInfo]:
        """Return online nodes that support `backend`."""
        return [
            node
            for node in self._nodes.values()
            if node.status == "online" and backend in node.capabilities.backends
        ]

    @staticmethod
    def _live_state_age_sec(node: NodeInfo) -> Optional[float]:
        if node.live_state_updated_at is None:
            return None
        updated = node.live_state_updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - updated).total_seconds()

    @classmethod
    def _fresh_slot_snapshot(
        cls,
        node: NodeInfo,
        max_live_state_age_sec: int,
    ) -> Optional[tuple[int, int]]:
        """Return (used, total) for fresh live_state, or None when unknown/stale."""
        if not isinstance(node.live_state, dict):
            return None
        age = cls._live_state_age_sec(node)
        if age is None or age > max_live_state_age_sec:
            return None
        try:
            total = int(node.live_state.get("slots_total") or node.capabilities.max_concurrent or 0)
            used = int(node.live_state.get("slots_used") or 0)
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        return used, total

    def pick_capable(
        self,
        backend: str,
        *,
        max_live_state_age_sec: int = 90,
    ) -> Optional[NodeInfo]:
        """Pick an online capable node, preferring fresh live_state with free slots."""
        candidates = self.list_capable(backend)
        if not candidates:
            return None

        fresh_available: List[tuple[float, int, NodeInfo]] = []
        unknown: List[NodeInfo] = []
        for index, node in enumerate(candidates):
            slots = self._fresh_slot_snapshot(node, max_live_state_age_sec)
            if slots is None:
                unknown.append(node)
                continue
            used, total = slots
            if used < total:
                fresh_available.append((used / total, index, node))

        if fresh_available:
            fresh_available.sort(key=lambda item: (item[0], item[1]))
            return fresh_available[0][2]
        if unknown:
            return unknown[0]
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

    def _db_upsert(self, node: NodeInfo) -> tuple[Optional[str], Optional[str]]:
        try:
            from src.control.db import get_db
            db = get_db()
            if db:
                old = db.get_node(node.node_id)
                old_incarnation = old.get("incarnation_id") if old else None
                incarnation_id = db.upsert_node(
                    node_id=node.node_id,
                    tailscale_ip=node.tailscale_ip,
                    api_port=node.api_port,
                    backends=node.capabilities.backends,
                    max_concurrent=node.capabilities.max_concurrent,
                    status="online",
                    projects_root=node.capabilities.projects_root,
                    repos=node.capabilities.repos,
                    incarnation_id=node.incarnation_id,
                )
                node.incarnation_id = incarnation_id
                return old_incarnation, incarnation_id
        except Exception as e:
            logger.debug("event=db_node_upsert_err node_id=%s err=%s", node.node_id, e)
        return None, node.incarnation_id

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

    def _db_mark_driver_sessions_lost(self, node_id: str) -> int:
        try:
            from src.control.db import get_db
            db = get_db()
            if db is not None:
                return db.mark_driver_sessions_lost_for_node(node_id)
        except Exception:
            logger.debug("event=node_registry_mark_driver_sessions_lost_failed node_id=%s", node_id, exc_info=True)
        return 0

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
