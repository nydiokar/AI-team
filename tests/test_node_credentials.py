"""[A71] Per-node credential gate on the task-server node endpoints.

Covers the credential-vs-node binding (_authorize_node) and the flag-gated
behaviour of the node lifecycle endpoints. No network, no paid backend —
config + DB singletons are monkeypatched to a temp SQLite file.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from src.control import db as db_mod
import src.control.task_server as ts
import config as config_mod


TOKEN = "test-shared-token"
NODE_A = "node-a"
NODE_B = "node-b"


def _cred(node_id: str) -> str:
    return f"cred-{node_id}"


def _sha(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


@pytest.fixture
def meshdb(tmp_path, monkeypatch):
    db = db_mod.MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "_db_instance", db)
    monkeypatch.setattr(ts, "_worker_token", lambda: TOKEN)
    return db


@pytest.fixture
def creds_enabled(monkeypatch):
    monkeypatch.setattr(config_mod.config.mesh, "node_credentials_enabled", True)


@pytest.fixture
def creds_disabled(monkeypatch):
    monkeypatch.setattr(config_mod.config.mesh, "node_credentials_enabled", False)


@pytest.fixture
def fallback_on(monkeypatch):
    monkeypatch.setattr(config_mod.config.mesh, "node_credentials_allow_shared_fallback", True)


@pytest.fixture
def fallback_off(monkeypatch):
    monkeypatch.setattr(config_mod.config.mesh, "node_credentials_allow_shared_fallback", False)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _enroll(db, node_id: str):
    db.enroll_node_credential(node_id, _sha(_cred(node_id)))


def _register(client, token: str, node_id: str):
    return client.post("/nodes/register", headers=_auth(token),
                       json={"node_id": node_id})


def _heartbeat(client, token: str, node_id: str):
    return client.post("/nodes/heartbeat", headers=_auth(token),
                       json={"node_id": node_id})


def _claim(client, token: str, task_id: str, node_id: str):
    return client.post(f"/tasks/{task_id}/claim", headers=_auth(token),
                       json={"node_id": node_id})


def _enqueue(db, task_id: str, machine_id=None):
    db.enqueue_task(task_id, session_id=None, machine_id=machine_id,
                    backend="claude", action="run", payload={"x": 1})


# --- flag OFF: byte-identical shared-token behaviour -------------------------

def test_flag_off_shared_token_unchanged(meshdb, creds_disabled):
    _enroll(meshdb, NODE_A)
    client = TestClient(ts.app)
    assert _register(client, TOKEN, NODE_A).status_code == 200
    assert _heartbeat(client, TOKEN, NODE_A).status_code == 200


def test_flag_off_credential_alone_rejected(meshdb, creds_disabled):
    """Flag OFF = byte-identical shared-token model: a node credential alone is 401."""
    _enroll(meshdb, NODE_A)
    client = TestClient(ts.app)
    assert _register(client, _cred(NODE_A), NODE_A).status_code == 401


# --- flag ON: binding --------------------------------------------------------

def test_register_requires_credential_bound_to_node(meshdb, creds_enabled, fallback_off):
    _enroll(meshdb, NODE_A)
    _enroll(meshdb, NODE_B)
    client = TestClient(ts.app)
    assert _register(client, _cred(NODE_B), NODE_A).status_code == 403
    assert _register(client, _cred(NODE_A), NODE_A).status_code == 200
    assert _heartbeat(client, _cred(NODE_A), NODE_A).status_code == 200


def test_shared_fallback_acts_as_any_node(meshdb, creds_enabled, fallback_on):
    _enroll(meshdb, NODE_A)
    client = TestClient(ts.app)
    assert _register(client, TOKEN, NODE_A).status_code == 200


def test_unknown_credential_rejected(meshdb, creds_enabled, fallback_off):
    client = TestClient(ts.app)
    assert _register(client, "not-enrolled", NODE_A).status_code in (401, 403)


# --- flag ON: pinned-task claim binding --------------------------------------

def test_claim_pinned_requires_pinned_credential(meshdb, creds_enabled, fallback_off):
    _enroll(meshdb, NODE_A)
    _enroll(meshdb, NODE_B)
    _enqueue(meshdb, "t1", machine_id=NODE_A)
    client = TestClient(ts.app)
    # wrong node's cred cannot claim the pinned task
    assert _claim(client, _cred(NODE_B), "t1", NODE_B).status_code == 403
    # the pinned node's cred can
    assert _claim(client, _cred(NODE_A), "t1", NODE_A).status_code == 200


def test_claim_pinned_blocks_shared_token_when_fallback_off(meshdb, creds_enabled, fallback_off):
    _enroll(meshdb, NODE_A)
    _enqueue(meshdb, "t2", machine_id=NODE_A)
    client = TestClient(ts.app)
    assert _claim(client, TOKEN, "t2", NODE_A).status_code == 403


def test_claim_unpinned_any_enrolled_node(meshdb, creds_enabled, fallback_off):
    _enroll(meshdb, NODE_A)
    _enroll(meshdb, NODE_B)
    _enqueue(meshdb, "t3", machine_id=None)
    client = TestClient(ts.app)
    assert _claim(client, _cred(NODE_A), "t3", NODE_A).status_code == 200


def test_claim_unpinned_shared_fallback_allowed(meshdb, creds_enabled, fallback_on):
    _enroll(meshdb, NODE_A)
    _enqueue(meshdb, "t4", machine_id=None)
    client = TestClient(ts.app)
    assert _claim(client, TOKEN, "t4", NODE_A).status_code == 200


# --- DB credential methods ---------------------------------------------------

def test_db_credential_methods_roundtrip(meshdb):
    _enroll(meshdb, NODE_A)
    assert meshdb.get_node_credential_hash(NODE_A) == _sha(_cred(NODE_A))
    assert meshdb.get_node_credential_hash("ghost") is None
    assert NODE_A in meshdb.list_node_credential_hashes()
    assert meshdb.revoke_node_credential(NODE_A) is True
    assert meshdb.get_node_credential_hash(NODE_A) is None
    assert meshdb.revoke_node_credential(NODE_A) is False
