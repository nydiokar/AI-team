"""Tests for M1 — Enriched heartbeats (live_state).

Workers now send active_tasks, slots_used, and slots_total with every heartbeat.
The gateway stores this as live_state JSON on the nodes row and exposes it via
NodeInfo.to_dict() and GET /nodes.
"""

import json
import uuid

import pytest

from src.control.db import MeshDB
from src.control.node_registry import NodeInfo, NodeCapabilities, NodeRegistry


def _node_id() -> str:
    return f"node_{uuid.uuid4().hex[:8]}"


def _make_info(node_id: str) -> NodeInfo:
    return NodeInfo(
        node_id=node_id,
        tailscale_ip="127.0.0.1",
        api_port=9001,
        capabilities=NodeCapabilities(backends=["claude"], max_concurrent=2),
    )


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_heartbeat_node_stores_live_state(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2)

    live = json.dumps({"active_tasks": ["task_abc"], "slots_used": 1, "slots_total": 2})
    db.heartbeat_node("node-a", live_state=live)

    rows = db.list_nodes()
    node = next(r for r in rows if r["node_id"] == "node-a")
    stored = json.loads(node["live_state"])
    assert stored["slots_used"] == 1
    assert stored["slots_total"] == 2
    assert stored["active_tasks"] == ["task_abc"]


def test_heartbeat_node_without_live_state_preserves_existing(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2)

    live = json.dumps({"active_tasks": ["task_abc"], "slots_used": 1, "slots_total": 2})
    db.heartbeat_node("node-a", live_state=live)
    # Second heartbeat with no live_state — should not wipe the existing value
    db.heartbeat_node("node-a", live_state=None)

    rows = db.list_nodes()
    node = next(r for r in rows if r["node_id"] == "node-a")
    assert node["live_state"] is not None
    stored = json.loads(node["live_state"])
    assert stored["slots_used"] == 1


# ---------------------------------------------------------------------------
# Registry layer
# ---------------------------------------------------------------------------


def test_registry_heartbeat_updates_live_state_in_memory():
    registry = NodeRegistry()
    nid = _node_id()
    registry.register(_make_info(nid))

    live = {"active_tasks": ["task_xyz"], "slots_used": 1, "slots_total": 2}
    registry.heartbeat(nid, live_state=live)

    node = registry.get(nid)
    assert node.live_state == live


def test_registry_heartbeat_without_live_state_does_not_wipe():
    registry = NodeRegistry()
    nid = _node_id()
    registry.register(_make_info(nid))

    live = {"active_tasks": ["task_xyz"], "slots_used": 1, "slots_total": 2}
    registry.heartbeat(nid, live_state=live)
    registry.heartbeat(nid, live_state=None)  # no state this time

    node = registry.get(nid)
    assert node.live_state == live  # still intact


def test_node_info_to_dict_includes_live_state():
    registry = NodeRegistry()
    nid = _node_id()
    registry.register(_make_info(nid))

    live = {"active_tasks": [], "slots_used": 0, "slots_total": 2}
    registry.heartbeat(nid, live_state=live)

    d = registry.get(nid).to_dict()
    assert d["live_state"] == live


# ---------------------------------------------------------------------------
# FastAPI endpoint
# ---------------------------------------------------------------------------


def test_heartbeat_endpoint_accepts_live_state(tmp_path):
    from fastapi.testclient import TestClient
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "hb_live.db")
    shared_token = "test-hb-live"
    cfg.mesh.worker_token = shared_token
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old:
        old.close()

    try:
        from src.control.task_server import app
        from src.control.node_registry import _registry
        import src.control.node_registry as reg_mod
        reg_mod._registry = None  # fresh registry for this test

        client = TestClient(app)
        headers = {"Authorization": f"Bearer {shared_token}"}

        nid = _node_id()
        client.post("/nodes/register", json={
            "node_id": nid,
            "tailscale_ip": "127.0.0.1",
            "api_port": 9001,
            "capabilities": {"backends": ["claude"], "max_concurrent": 2},
        }, headers=headers)

        resp = client.post("/nodes/heartbeat", json={
            "node_id": nid,
            "live_state": {
                "active_tasks": ["task_abc"],
                "slots_used": 1,
                "slots_total": 2,
            },
        }, headers=headers)
        assert resp.status_code == 200

        node = reg_mod.get_registry().get(nid)
        assert node.live_state["slots_used"] == 1
        assert node.live_state["active_tasks"] == ["task_abc"]

    finally:
        db_mod._db_instance = None
        if old:
            old.close()
        db_mod._db_instance = old
        reg_mod._registry = None


def test_heartbeat_endpoint_backward_compatible(tmp_path):
    """Old workers sending only node_id still work — live_state defaults to None."""
    from fastapi.testclient import TestClient
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "hb_compat.db")
    shared_token = "test-hb-compat"
    cfg.mesh.worker_token = shared_token
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old:
        old.close()

    try:
        from src.control.task_server import app
        import src.control.node_registry as reg_mod
        reg_mod._registry = None

        client = TestClient(app)
        headers = {"Authorization": f"Bearer {shared_token}"}

        nid = _node_id()
        client.post("/nodes/register", json={
            "node_id": nid,
            "tailscale_ip": "127.0.0.1",
            "api_port": 9001,
            "capabilities": {"backends": ["claude"], "max_concurrent": 2},
        }, headers=headers)

        # Old-style heartbeat with only node_id
        resp = client.post("/nodes/heartbeat", json={"node_id": nid}, headers=headers)
        assert resp.status_code == 200

    finally:
        db_mod._db_instance = None
        if old:
            old.close()
        db_mod._db_instance = old
        reg_mod._registry = None
