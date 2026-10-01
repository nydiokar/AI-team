"""A82 Stage 5 — scoped agent sender (packet §9, design §9; AUTH01–07 + INT11).

Real pieces: a file-backed SQLite ``MeshDB``; the REAL task-server app (carrier
``/claim-managed`` = the authenticated provisioning request that mints the
capability) and the REAL control-API app (``POST /api/sessions/{id}/turn-requests``
with the scoped auth handler) over a REAL ``TaskOrchestrator`` admission path;
the REAL ``WorkerAgent`` claim/provision bookkeeping; the REAL Claude driver
sender slot + in-process SDK MCP server and the REAL Codex ``_thread_config``.
Only the paid backend execution is stubbed, and the tool's socket is routed
into the in-process control API (headers/body unchanged), so every request
crosses the real auth boundary. No CLI is ever spawned.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

import src.control.db as db_mod
import src.control.node_registry as nr_mod
import src.control.task_server as ts
import src.worker.agent as agent_mod
from src.control import agent_sender, control_api
from src.control.db import MeshDB
from src.control.turn_queue import InvalidCredentialError
from src.core.interfaces import Session, SessionStatus
from src.orchestrator import TaskOrchestrator
from tests.test_turn_queue_carrier_integration import _ClientHTTP
from tests.test_turn_queue_producer1 import _no_cli_spawn  # noqa: F401 — autouse guard

REPO = Path(__file__).resolve().parent.parent
WORKER = "worker-shared-token"
ADMIN = "admin-dashboard-token"
WH = {"Authorization": f"Bearer {WORKER}"}
NOW = "2026-10-02T00:00:00+00:00"
MGR, WRK, WRK2, OUT, SOLO = "mgr-1", "wrk-1", "wrk-2", "out-1", "solo-1"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class World(SimpleNamespace):
    db: MeshDB
    task: TestClient
    api: TestClient
    orch: Any
    node: str
    case_a: str
    case_b: str


def _register(task: TestClient, node: str, inc: str) -> None:
    r = task.post("/nodes/register", json={
        "node_id": node, "tailscale_ip": "127.0.0.1", "api_port": 0, "incarnation_id": inc,
        "capabilities": {"backends": ["claude", "codex"], "queue_protocols": [0, 1],
                         "managed_backends": ["claude"]},
    }, headers=WH)
    assert r.status_code == 200, r.text


def _mk_orch(db: MeshDB) -> Any:
    from src.core.session_task_queue import SessionTaskQueue
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore

    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.session_service = SessionService(o.session_store, repo_path_validator=lambda _p: None)
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o._backends = {"claude": object()}
    o.events = []
    o._emit_event = lambda name, task, data=None: o.events.append((name, task.id, data))
    o._emit_turn_telemetry = lambda name, task, data=None, **k: o.events.append((name, task.id, data))
    return o


def _mk_world(tmp_path, monkeypatch, *, node: str = "Horse", pinned: bool = True,
              register: bool = True) -> World:
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.delenv("HARNESS_LEVEL3_GUARD", raising=False)
    from src.control import turn_admission as ta

    # A fresh gateway process starts with a fresh shared waiting allowance.
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(ts, "get_db", lambda: db)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts, "_worker_token", lambda: WORKER)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: ADMIN)
    monkeypatch.setattr(control_api, "_worker_token", lambda: WORKER)
    from config import config

    monkeypatch.setattr(config.mesh, "local_carrier_node_id", "" if pinned else node, raising=False)
    task = TestClient(ts.app)
    if register:
        _register(task, node, "inc-1")
    machine = node if pinned else ""
    for sid in (MGR, WRK, WRK2, OUT, SOLO):
        db.upsert_session(Session(
            session_id=sid, backend="claude", repo_path="/tmp/repo",
            status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=machine,
        ))
        db.enroll_session(sid)
    case_a = db.open_case(objective="objective A", session_id=MGR, role="manager")
    db.set_session_case(MGR, case_a, "manager")
    db.set_session_case(WRK, case_a, "worker")
    db.set_session_case(WRK2, case_a, "worker")
    case_b = db.open_case(objective="objective B", session_id=OUT, role="manager")
    db.set_session_case(OUT, case_b, "manager")
    orch = _mk_orch(db)
    api = TestClient(control_api.build_control_api(orch))
    return World(db=db, task=task, api=api, orch=orch, node=node, case_a=case_a, case_b=case_b)


def _turn(db: MeshDB, tid: str, sid: str, node: str, prompt: str = "frozen prompt") -> None:
    db.enqueue_turn(
        task_id=tid, session_id=sid, backend="claude", action="resume_session",
        payload={"task_id": tid, "prompt": prompt}, turn_source="human",
        turn_kind="instruction", machine_id=node,
    )
    assert db.activate_turn(tid)


def _claim(w: World, tid: str, inc: str = "inc-1", **extra: Any):
    return w.task.post(f"/tasks/{tid}/claim-managed", json={
        "node_id": w.node, "carrier_kind": "worker_daemon", "incarnation_id": inc,
        "queue_protocols": [1], **extra,
    }, headers=WH)


def _cap(w: World, tid: str, sid: str, **extra: Any) -> str:
    _turn(w.db, tid, sid, w.node)
    r = _claim(w, tid, **extra)
    assert r.status_code == 200, r.text
    grant = r.json()["sender_capability"]
    assert grant and grant["token"], r.json()
    return grant["token"]


def _send(w: World, cap: str, target: str, body: str = "hello", op: str = "op-1",
          scheme: str = "AITeamSender", extra: Optional[Dict[str, Any]] = None,
          headers: Optional[Dict[str, str]] = None):
    return w.api.post(
        f"/api/sessions/{target}/turn-requests",
        json={"body": body, "operation_id": op, **(extra or {})},
        headers={"Authorization": f"{scheme} {cap}", "Idempotency-Key": op, **(headers or {})},
    )


def _agent_rows(db: MeshDB, target: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE queue_protocol = 1 AND session_id = ? "
        "AND turn_source = 'agent' ORDER BY queue_sequence", (target,)).fetchall()]


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _route_tool_to(monkeypatch, api: TestClient, base: str, seen: Optional[List[str]] = None) -> None:
    """Route the tool's single HTTP choke point into the in-process control API
    (URL/headers/body unchanged), asserting the resolved gateway address."""

    def _post(url: str, body: bytes, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
        assert url.startswith(base + "/api/sessions/"), url
        if seen is not None:
            seen.append(url)
        resp = api.post(url[len(base):], content=body, headers=headers)
        return resp.status_code, resp.content

    monkeypatch.setattr(agent_sender, "_http_post", _post)


async def _call_tool(server: Dict[str, Any], args: Dict[str, Any]) -> Tuple[str, bool]:
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = server["instance"].request_handlers[CallToolRequest]
    res = await handler(CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=agent_sender.SEND_TOOL_NAME, arguments=args),
    ))
    return res.root.content[0].text, bool(res.root.isError)


# --------------------------------------------------------------------------- #
# AUTH01 — issuance: current-owner claim only, canonical binding, hash only
# --------------------------------------------------------------------------- #
def test_AUTH01_capability_minted_from_current_carrier_claim_hash_only(tmp_path, monkeypatch):
    w = _mk_world(tmp_path, monkeypatch)
    _turn(w.db, "w-t1", WRK, w.node)
    # The request tries to CHOOSE its bindings: ignored — binding is canonical.
    r = _claim(w, "w-t1", case_id=w.case_b, role="manager", sender_session_id=OUT)
    assert r.status_code == 200, r.text
    body = r.json()
    grant = body["sender_capability"]
    assert (grant["session_id"], grant["case_id"], grant["role"]) == (WRK, w.case_a, "worker")
    assert grant["generation"] >= 1 and grant["operations"] == ["send_instruction"]
    tok = grant["token"]
    assert isinstance(tok, str) and len(tok) >= 40
    assert "sender_capability" not in json.dumps(body["task"]), "frozen payload must not carry it"
    rows = [dict(x) for x in w.db._conn().execute("SELECT * FROM mesh_sender_capabilities")]
    assert [x["token_hash"] for x in rows] == [_sha(tok)]
    assert tok not in json.dumps(rows)
    # The carrier presents the generation it holds ⇒ no new secret is minted.
    r2 = _claim(w, "w-t1", sender_capability_generation=grant["generation"])
    g2 = r2.json()["sender_capability"]
    assert g2["token"] is None and g2["generation"] == grant["generation"]
    assert _send(w, tok, MGR, op="still-valid").status_code == 202
    # A carrier that lost the secret (lost claim response) gets a rotation;
    # the superseded secret is dead.
    r3 = _claim(w, "w-t1")
    g3 = r3.json()["sender_capability"]
    assert g3["token"] and g3["token"] != tok and g3["generation"] > grant["generation"]
    assert _send(w, tok, MGR, op="old").status_code == 401
    assert _send(w, g3["token"], MGR, op="new").status_code == 202
    # A non-owner (zombie incarnation) cannot claim, so cannot mint.
    n_before = w.db._conn().execute("SELECT COUNT(*) FROM mesh_sender_capabilities").fetchone()[0]
    assert _claim(w, "w-t1", inc="inc-zombie").status_code == 409
    with pytest.raises(InvalidCredentialError):
        w.db.issue_sender_capability("w-t1", "forged-claim-token", w.node, "inc-1")
    assert w.db._conn().execute("SELECT COUNT(*) FROM mesh_sender_capabilities").fetchone()[0] == n_before
    # A standalone (no Case) enrolled session gets no capability at all.
    _turn(w.db, "s-t1", SOLO, w.node)
    assert _claim(w, "s-t1").json()["sender_capability"] is None
    # Minting requires the carrier credential.
    _turn(w.db, "w2-t1", WRK2, w.node)
    assert w.task.post("/tasks/w2-t1/claim-managed", json={
        "node_id": w.node, "carrier_kind": "worker_daemon", "incarnation_id": "inc-1",
    }).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# AUTH02 — validation on every send + revocation + reissue via ownership only
# --------------------------------------------------------------------------- #
def test_AUTH02_validation_revocation_and_reissue(tmp_path, monkeypatch):
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "w-t1", WRK)
    assert _send(w, cap, MGR, op="a").status_code == 202
    assert _send(w, "f" * 43, MGR, op="forged").status_code == 401
    assert _send(w, "", MGR, op="empty").status_code == 401
    assert _send(w, "x" * 5000, MGR, op="huge").status_code == 401
    # Case-binding change revokes; moving back does NOT resurrect it.
    w.db.set_session_case(WRK, w.case_b, "worker")
    assert _send(w, cap, OUT, op="b").status_code == 401
    w.db.set_session_case(WRK, w.case_a, "worker")
    assert _send(w, cap, MGR, op="c").status_code == 401
    revoked = w.db._conn().execute(
        "SELECT revoked_at FROM mesh_sender_capabilities WHERE token_hash = ?", (_sha(cap),),
    ).fetchone()
    assert revoked is not None and revoked[0]
    # Reissue only through the current owner's claim.
    r = _claim(w, "w-t1")
    cap2 = r.json()["sender_capability"]["token"]
    assert cap2 and _send(w, cap2, MGR, op="d").status_code == 202
    # Role change in the same Case also revokes.
    w.db.set_session_case(WRK, w.case_a, "manager")
    assert _send(w, cap2, WRK2, op="e").status_code == 401
    w.db.set_session_case(WRK, w.case_a, "worker")
    cap3 = _claim(w, "w-t1").json()["sender_capability"]["token"]
    assert _send(w, cap3, MGR, op="f").status_code == 202
    # Carrier replacement (a new registered incarnation) revokes.
    _register(w.task, w.node, "inc-2")
    assert _send(w, cap3, MGR, op="g").status_code == 401
    # Session close revokes.
    cap_w2 = None
    _register(w.task, w.node, "inc-1")
    _turn(w.db, "w2-t1", WRK2, w.node)
    cap_w2 = _claim(w, "w2-t1").json()["sender_capability"]["token"]
    assert _send(w, cap_w2, MGR, op="h").status_code == 202
    w.db.close_session_turns(WRK2)
    assert _send(w, cap_w2, MGR, op="i").status_code == 401
    assert w.db._conn().execute(
        "SELECT revoked_at FROM mesh_sender_capabilities WHERE token_hash = ?", (_sha(cap_w2),),
    ).fetchone()[0]
    # Case close (sender Case no longer open) ⇒ refused as revoked.
    capm = _cap(w, "m-t1", MGR)
    w.db._conn().execute("UPDATE flow_runs SET status = 'closed' WHERE flow_run_id = ?", (w.case_a,))
    w.db._conn().commit()
    assert _send(w, capm, WRK, op="j").status_code == 401


# --------------------------------------------------------------------------- #
# AUTH03 — sender binding, same-Case scope, no impersonation, fanout bound
# --------------------------------------------------------------------------- #
def test_AUTH03_sender_binding_and_same_case_scope(tmp_path, monkeypatch):
    w = _mk_world(tmp_path, monkeypatch)
    cap_w = _cap(w, "w-t1", WRK)
    cap_m = _cap(w, "m-t1", MGR)
    r = _send(w, cap_w, MGR, body="worker to manager", op="wm")
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "queued" and out["turn_id"] and out["source"] == "agent"
    assert out["sender_session_id"] == WRK
    row = w.db.get_task(out["turn_id"])
    assert row["turn_source"] == "agent" and row["sender_session_id"] == WRK
    assert row["flow_run_id"] == w.case_a and row["prompt"] == "worker to manager"
    # Sending never re-affiliates the recipient (the Manager stays Manager).
    assert w.db.get_session(MGR)["case_role"] == "manager"
    assert w.db.get_session(MGR)["current_case_id"] == w.case_a
    assert _send(w, cap_w, WRK2, op="ww").status_code == 202  # worker → worker
    assert _send(w, cap_m, WRK, op="mw").status_code == 202  # manager → worker
    # Refused: cross-Case, self-send, broadcast-ish / unknown targets.
    for target in (OUT, WRK, "*", "all", "nope-session"):
        assert _send(w, cap_w, target, op=f"x-{target}").status_code == 403, target
    assert _send(w, cap_m, MGR, op="self").status_code == 403
    # Source / sender cannot be chosen by the request body.
    for extra in ({"source": "system"}, {"sender_session_id": OUT}, {"turn_source": "human"}):
        assert _send(w, cap_w, MGR, op="imp", extra=extra).status_code == 422
    # The operation-id header must match the body's.
    assert _send(w, cap_w, MGR, op="hdr", headers={"Idempotency-Key": "other"}).status_code == 422
    assert len(_agent_rows(w.db, MGR)) == 1
    # Role pairs: Manager→Manager and non-member roles (e.g. reviewer) refused.
    w.db.set_session_case(WRK2, w.case_a, "manager")
    assert _send(w, cap_m, WRK2, op="mm").status_code == 403
    w.db.set_session_case(WRK2, w.case_a, "reviewer")
    assert _send(w, cap_w, WRK2, op="rev").status_code == 403
    w.db.set_session_case(WRK2, w.case_a, "worker")
    assert len(_agent_rows(w.db, WRK2)) == 1
    # Unenrolled same-Case recipient: not a managed target (409, nothing admitted).
    w.db._conn().execute("UPDATE sessions SET turn_queue_enrolled = 0 WHERE session_id = ?", (WRK2,))
    w.db._conn().commit()
    assert _send(w, cap_w, WRK2, op="unenrolled").status_code == 409


def test_AUTH03b_agent_fanout_rate_bound(tmp_path, monkeypatch):
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "w-t1", WRK)
    for i in range(30):
        target = MGR if i % 2 == 0 else WRK2
        r = _send(w, cap, target, body=f"n{i}", op=f"fan-{i}")
        assert r.status_code == 202, (i, r.text)
    r = _send(w, cap, MGR, body="one too many", op="fan-30")
    assert r.status_code == 429 and r.headers.get("Retry-After") == "60"
    # A replay of an accepted operation still resolves (idempotency first).
    assert _send(w, cap, MGR, body="n0", op="fan-0").status_code == 202


# --------------------------------------------------------------------------- #
# AUTH04 — shared credentials are not agent identity; capability is send-only
# --------------------------------------------------------------------------- #
def test_AUTH04_shared_credential_not_scoped_sender_and_capability_is_send_only(tmp_path, monkeypatch):
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "w-t1", WRK)
    assert _send(w, ADMIN, MGR, op="admin-as-agent").status_code == 401
    assert _send(w, WORKER, MGR, op="worker-as-agent").status_code == 401
    assert _send(w, cap, MGR, op="cap-as-bearer", scheme="Bearer").status_code == 401
    assert _agent_rows(w.db, MGR) == []
    ok = _send(w, cap, MGR, op="real")
    assert ok.status_code == 202
    tid = ok.json()["turn_id"]
    forbidden = [
        ("PATCH", f"/api/turn-requests/{tid}", {"body": "edited"}),
        ("POST", f"/api/turn-requests/{tid}/withdraw", {}),
        ("GET", f"/api/turn-requests/{tid}", None),
        ("GET", f"/api/sessions/{MGR}/turn-requests", None),
        ("POST", f"/api/sessions/{MGR}/turn-requests/pause", None),
        ("POST", f"/api/turn-requests/{tid}/resolve-recovery", {}),
        ("POST", "/api/instructions", {"description": "x", "session_id": MGR}),
        ("POST", "/api/sessions", {"backend": "claude", "repo_path": "/tmp/repo"}),
        ("POST", f"/api/cases/{w.case_a}/close", {}),
    ]
    for scheme in ("Bearer", "AITeamSender"):
        for method, path, body in forbidden:
            r = w.api.request(method, path, json=body, headers={"Authorization": f"{scheme} {cap}",
                                                                 "If-Match": "1"})
            assert r.status_code == 401, (scheme, method, path, r.status_code)
        # Carrier routes (claims, results, staging, minting) take the worker token only.
        for path in ("/tasks/w-t1/claim-managed", "/tasks/pending-managed", "/tasks/w-t1/start-managed"):
            r = w.task.request("GET" if "pending" in path else "POST", path,
                               json=None if "pending" in path else {"node_id": w.node, "incarnation_id": "inc-1"},
                               headers={"Authorization": f"{scheme} {cap}"})
            assert r.status_code in (401, 403), (scheme, path, r.status_code)
    # Operator authority on the same resource is unchanged.
    r = w.api.post(f"/api/sessions/{MGR}/turn-requests", json={"body": "human", "operation_id": "h1"},
                   headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 202 and w.db.get_task(r.json()["turn_id"])["turn_source"] == "human"


# --------------------------------------------------------------------------- #
# AUTH05 — per-backend-instance provisioning; concurrent sessions separated
# --------------------------------------------------------------------------- #
def test_AUTH05_claude_and_codex_mcp_provisioning_is_per_session(tmp_path, monkeypatch):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from src.backends.claude_driver import ClaudeSDKClientDriver, _SDKSession
    from src.backends.codex_native import CodexBackend

    env_before = dict(os.environ)
    monkeypatch.setenv("CONTROLLER_URL", "http://gw.example:9002")
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    monkeypatch.setenv("DASHBOARD_PORT", "9003")
    drv = ClaudeSDKClientDriver()
    assert drv.provision_sender_capability("sess-a", "tok-A" * 9) is True
    assert drv.provision_sender_capability("sess-b", "tok-B" * 9) is True
    sa = _SDKSession("sess-a", str(tmp_path), None, {"SESSION_ID": "sess-a"},
                     sender_slot=drv._sender_slots["sess-a"])
    sb = _SDKSession("sess-b", str(tmp_path), None, {"SESSION_ID": "sess-b"},
                     sender_slot=drv._sender_slots["sess-b"])
    plain = _SDKSession("sess-c", str(tmp_path), None, {})
    oa, ob, oc = sa._sdk_options(), sb._sdk_options(), plain._sdk_options()
    name = agent_sender.SENDER_SERVER_NAME
    assert oa.mcp_servers[name]["type"] == "sdk" and ob.mcp_servers[name]["type"] == "sdk"
    assert oa.mcp_servers[name]["instance"] is not ob.mcp_servers[name]["instance"]
    assert name not in (oc.mcp_servers or {})
    assert agent_sender.SENDER_TOOL_FQN in oa.allowed_tools
    assert agent_sender.SENDER_TOOL_FQN not in oc.allowed_tools
    # User/project MCP settings preserved: no strict replacement, settings untouched.
    assert oa.strict_mcp_config is False and oa.setting_sources == oc.setting_sources
    # The secret is in neither the CLI env nor its argv nor the global env.
    for opts, tok in ((oa, "tok-A" * 9), (ob, "tok-B" * 9)):
        assert tok not in json.dumps(opts.env)
        tr = SubprocessCLITransport(prompt="", options=opts)
        tr._cli_path = "claude"
        assert tok not in " ".join(tr._build_command())
    assert "tok-A" not in json.dumps(dict(os.environ)) and "tok-B" not in json.dumps(dict(os.environ))
    # Each session's tool presents ITS OWN capability to the resolved gateway.
    seen: List[Tuple[str, str]] = []

    def _post(url, body, headers, timeout):
        seen.append((url, headers["Authorization"]))
        return 202, json.dumps({"turn_id": "t", "status": "queued", "revision": 1,
                                "queue_sequence": 1}).encode()

    monkeypatch.setattr(agent_sender, "_http_post", _post)
    args = {"target_session_id": "mgr", "body": "hi", "operation_id": "op"}
    asyncio.run(_call_tool(oa.mcp_servers[name], args))
    asyncio.run(_call_tool(ob.mcp_servers[name], args))
    assert seen == [("http://gw.example:9003/api/sessions/mgr/turn-requests", f"AITeamSender {'tok-A' * 9}"),
                    ("http://gw.example:9003/api/sessions/mgr/turn-requests", f"AITeamSender {'tok-B' * 9}")]
    # Rotation reaches the LIVE instance without a respawn; revocation empties it.
    drv.provision_sender_capability("sess-a", "tok-A2" * 8)
    asyncio.run(_call_tool(oa.mcp_servers[name], args))
    assert seen[-1][1] == f"AITeamSender {'tok-A2' * 8}"
    drv.provision_sender_capability("sess-a", None)
    text, is_error = asyncio.run(_call_tool(oa.mcp_servers[name], args))
    assert is_error and "not provisioned" in text and len(seen) == 3

    # Codex native seam: per-thread config, user servers + identity preserved.
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[mcp_servers.jobs]\ncommand = "python"\nargs = ["jobs.py"]\nenv = { "KEEP" = "1" }\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    cx = CodexBackend()
    assert cx.provision_sender_capability("sess-a", "cdx-A" * 9) is True
    assert cx.provision_sender_capability("sess-b", "cdx-B" * 9) is True
    ca, cb, cc = cx._thread_config("sess-a"), cx._thread_config("sess-b"), cx._thread_config("sess-c")
    for cfg, sid in ((ca, "sess-a"), (cb, "sess-b"), (cc, "sess-c")):
        assert cfg["mcp_servers.jobs.env"] == {"KEEP": "1", "SESSION_ID": sid, "AI_TEAM_SESSION_ID": sid}
        assert cfg["shell_environment_policy.set"] == {"SESSION_ID": sid, "AI_TEAM_SESSION_ID": sid}
    assert ca[f"mcp_servers.{name}.env"][agent_sender.CAPABILITY_ENV] == "cdx-A" * 9
    assert cb[f"mcp_servers.{name}.env"][agent_sender.CAPABILITY_ENV] == "cdx-B" * 9
    assert ca[f"mcp_servers.{name}.env"][agent_sender.SENDER_URL_ENV] == "http://gw.example:9003"
    assert ca[f"mcp_servers.{name}.args"] == [str(REPO / "scripts" / "mcp_sender.py")]
    assert f"mcp_servers.{name}.command" not in cc and f"mcp_servers.{name}.env" not in cc
    # The capability never leaks into the agent's shell environment.
    assert "cdx-A" not in json.dumps(ca["shell_environment_policy.set"])
    assert "cdx-A" not in json.dumps(dict(os.environ))
    assert {k: v for k, v in os.environ.items() if k not in ("CONTROLLER_URL", "DASHBOARD_PORT", "CODEX_HOME")} == {
        k: v for k, v in env_before.items() if k not in ("CONTROLLER_URL", "DASHBOARD_PORT", "CODEX_HOME", "DASHBOARD_URL")}


def test_AUTH05b_codex_rotation_reattaches_loaded_thread(tmp_path, monkeypatch):
    from tests.test_codex_native import Runtime, run

    from src.backends.codex_native import CodexBackend

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("src.core.test_guard.assert_live_calls_allowed", lambda _: None)
    runtime = Runtime()
    backend = CodexBackend()
    monkeypatch.setattr(backend, "_runtime", lambda: runtime)
    name = agent_sender.SENDER_SERVER_NAME
    backend.provision_sender_capability("k", "first-capability-0123456789abcdef")
    first = run(backend, str(tmp_path), key="k")
    run(backend, str(tmp_path), key="k", native_id=first.backend_session_id, task="t2")
    assert [m for m, _ in runtime.calls] == ["thread/start", "turn/start", "turn/start"]
    backend.provision_sender_capability("k", "second-capability-0123456789abcdef")
    run(backend, str(tmp_path), key="k", native_id=first.backend_session_id, task="t3")
    methods = [m for m, _ in runtime.calls]
    assert methods[3:] == ["thread/unsubscribe", "thread/resume", "turn/start"]
    resumed = [p for m, p in runtime.calls if m == "thread/resume"][0]
    assert resumed["config"][f"mcp_servers.{name}.env"][agent_sender.CAPABILITY_ENV] == (
        "second-capability-0123456789abcdef")


# --------------------------------------------------------------------------- #
# AUTH06 — tool retry (incl. after a tool restart) reuses the key; conflicts
# --------------------------------------------------------------------------- #
def _load_stdio_script():
    spec = importlib.util.spec_from_file_location("mcp_sender_under_test", REPO / "scripts" / "mcp_sender.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_AUTH06_same_operation_retry_after_tool_restart(tmp_path, monkeypatch, capsys):
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "w-t1", WRK)
    base = "http://gw.example:9003"
    _route_tool_to(monkeypatch, w.api, base)
    monkeypatch.setenv(agent_sender.CAPABILITY_ENV, cap)
    monkeypatch.setenv(agent_sender.SENDER_URL_ENV, base)
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "send_instruction",
        "arguments": {"target_session_id": MGR, "body": "please review", "operation_id": "op-r"}}}
    replies = []
    for _restart in range(2):  # a fresh stdio process each time
        mod = _load_stdio_script()
        mod._dispatch(call)
        replies.append(json.loads(capsys.readouterr().out.strip().splitlines()[-1]))
    texts = [r["result"]["content"][0]["text"] for r in replies]
    assert not any(r["result"].get("isError") for r in replies), texts
    rows = _agent_rows(w.db, MGR)
    assert len(rows) == 1 and all(rows[0]["id"] in t for t in texts)
    assert "queued" in texts[0] and "not" in texts[0].lower() and "read" in texts[0].lower()
    assert "replay" in texts[1].lower()
    # Same key, different body ⇒ 409 conflict surfaced as a tool error.
    call["params"]["arguments"]["body"] = "something else"
    _load_stdio_script()._dispatch(call)
    reply = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert reply["result"]["isError"] and "409" in reply["result"]["content"][0]["text"]
    assert len(_agent_rows(w.db, MGR)) == 1
    # Shared-module path is equally stateless: same op ⇒ same id.
    out = agent_sender.send_instruction(cap, base, {"target_session_id": MGR, "body": "please review",
                                                    "operation_id": "op-r"})
    assert out.ok and out.turn_id == rows[0]["id"] and out.idempotent_replay


def test_AUTH06b_stdio_tool_never_falls_back_to_shared_tokens(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DASHBOARD_TOKEN={ADMIN}\nWORKER_TOKEN={WORKER}\n"
                        f"{agent_sender.CAPABILITY_ENV}=from-dotenv-should-not-load\n")
    env = {k: v for k, v in os.environ.items() if k not in (agent_sender.CAPABILITY_ENV,)}
    env.update({"AI_TEAM_ENV_FILE": str(env_file), "DASHBOARD_TOKEN": ADMIN, "WORKER_TOKEN": WORKER,
                agent_sender.SENDER_URL_ENV: "http://127.0.0.1:9"})
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "send_instruction",
         "arguments": {"target_session_id": MGR, "body": "x", "operation_id": "o"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "send_instruction",
         "arguments": {"target_session_id": MGR, "body": "x" * (17 * 1024), "operation_id": "o"}}},
    ]
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "mcp_sender.py")],
                          input="\n".join(json.dumps(r) for r in reqs) + "\n",
                          capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path))
    out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [o["id"] for o in out] == [1, 2, 3, 4]
    tools = out[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["send_instruction"]
    assert set(tools[0]["inputSchema"]["required"]) == {"target_session_id", "body", "operation_id"}
    no_cap = out[2]["result"]
    assert no_cap["isError"] and "not provisioned" in no_cap["content"][0]["text"]
    assert out[3]["result"]["isError"] and "16 KiB" in out[3]["result"]["content"][0]["text"]
    assert ADMIN not in proc.stdout + proc.stderr and WORKER not in proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# INT11 — credentialed worker sends two instructions to a busy same-Case
# Manager via the real claim → provision → tool → endpoint → admission path,
# on the gateway-host (local) carrier and a remote node carrier.
# --------------------------------------------------------------------------- #
class _ClaudeCarrierBackend:
    """Managed-capable carrier backend whose sender provisioning is the REAL
    Claude SDK driver (slot + in-process MCP server); execution is stubbed."""

    def __init__(self) -> None:
        from src.backends.claude_driver import ClaudeSDKClientDriver

        self.driver = ClaudeSDKClientDriver()

    def supports_managed_turns(self) -> bool:
        return True

    def is_quiescent(self, session) -> bool:
        return True

    def provision_sender_capability(self, session_id: str, token: Optional[str]) -> bool:
        return self.driver.provision_sender_capability(session_id, token)


def _mk_worker(tmp_path, http, node: str, backend: Any):
    from tests.test_turn_queue_carrier_integration import _worker

    w = _worker(tmp_path, http, managed=False)
    w.cfg.node_id = node
    w._backends = {"claude": backend}
    w.cfg.managed_turns = True
    w._register()
    return w


@pytest.mark.parametrize("carrier", ["local", "remote"])
def test_INT11_credentialed_worker_sends_to_busy_same_case_manager(tmp_path, monkeypatch, caplog, carrier):
    caplog.set_level("DEBUG")
    local = carrier == "local"
    node = "gw-local" if local else "Horse"
    controller = "http://127.0.0.1:9002" if local else "http://gw.tailnet.example:9002"
    base = "http://127.0.0.1:9003" if local else "http://gw.tailnet.example:9003"
    monkeypatch.setenv("CONTROLLER_URL", controller)
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    monkeypatch.setenv("DASHBOARD_PORT", "9003")
    w = _mk_world(tmp_path, monkeypatch, node=node, pinned=not local, register=False)
    import tests.test_turn_queue_carrier_integration as ci

    monkeypatch.setattr(ci, "TOKEN", WORKER)
    backend = _ClaudeCarrierBackend()
    http = _ClientHTTP(w.task)
    worker = _mk_worker(tmp_path, http, node, backend)
    sessions_before = w.db._conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    # The Manager is BUSY: its own turn is claimed + started (running).
    _turn(w.db, "m-t1", MGR, node, prompt="manager work")
    tok_m = _claim(w, "m-t1").json()["claim_token"]
    assert w.task.post("/tasks/m-t1/start-managed", json={
        "node_id": node, "claim_token": tok_m, "incarnation_id": "inc-1"}, headers=WH).status_code == 200
    _turn(w.db, "w-t1", WRK, node, prompt="worker work")
    seen: List[str] = []
    _route_tool_to(monkeypatch, w.api, base, seen)
    outcomes: List[Tuple[str, bool]] = []

    async def fake_execute(task_row, backends, http=None, telemetry_sink=None, node_id="", ownership=None,
                           on_process=None):
        # The agent inside the worker session calls the provisioned tool twice.
        slot = backends["claude"].driver._sender_slots[ownership.session_id]
        server = agent_sender.build_claude_sender_server(slot)
        for op, body in (("op-1", "first finding"), ("op-2", "second finding")):
            outcomes.append(await _call_tool(server, {"target_session_id": MGR, "body": body,
                                                      "operation_id": op}))
        return {"success": True, "output": "worker done", "errors": [], "files_modified": [],
                "execution_time": 0.01, "timestamp": NOW, "return_code": 0,
                "backend_session_id": "native-w"}

    monkeypatch.setattr(agent_mod, "_execute_task", fake_execute)

    async def scenario():
        rows = await worker._fetch_pending()
        row = [r for r in rows if r["id"] == "w-t1"][0]
        await worker._handle_task(row)

    asyncio.run(scenario())
    assert [e for _t, e in outcomes] == [False, False], outcomes
    assert len(seen) == 2
    rows = _agent_rows(w.db, MGR)
    assert [r["prompt"] for r in rows] == ["first finding", "second finding"]
    assert len({r["id"] for r in rows}) == 2 and rows[0]["queue_sequence"] < rows[1]["queue_sequence"]
    for r, (text, _e) in zip(rows, outcomes):
        assert r["id"] in text and "queued" in text
        assert r["status"] == "queued" and r["sender_session_id"] == WRK and r["flow_run_id"] == w.case_a
    # No interruption of the busy Manager; it stays Manager of the Case.
    m = w.db.get_task("m-t1")
    assert m["status"] == "running" and not m.get("cancel_requested_at")
    assert w.db.get_active_turn(MGR)["id"] == "m-t1"
    assert w.db.get_session(MGR)["case_role"] == "manager"
    assert w.db.get_task("w-t1")["status"] == "completed"
    assert w.db._conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions_before
    # A human send uses the same ledger, after the agent's turns.
    h = w.api.post(f"/api/sessions/{MGR}/turn-requests", json={"body": "human note", "operation_id": "h"},
                   headers={"Authorization": f"Bearer {ADMIN}"})
    assert h.status_code == 202 and h.json()["queue_sequence"] > rows[1]["queue_sequence"]
    # Forged / cross-Case / revoked sender fail.
    cap = backend.driver._sender_slots[WRK].token
    assert cap and _send(w, "f" * 43, MGR, op="forged").status_code == 401
    assert _send(w, cap, OUT, op="cross").status_code == 403
    # Leak sweep while the secret is still live: nowhere but carrier memory.
    surfaces: List[str] = [caplog.text, json.dumps(w.orch.events, default=str),
                           json.dumps(http.calls[:1], default=str)]
    for path in (f"/api/sessions/{MGR}/turn-requests", f"/api/turn-requests/{rows[0]['id']}",
                 "/api/tasks", f"/api/sessions/{WRK}/messages", f"/api/sessions/{WRK}/timeline"):
        r = w.api.get(path, headers={"Authorization": f"Bearer {ADMIN}"})
        surfaces.append(r.text)
    surfaces.append(json.dumps([dict(x) for x in w.db._conn().execute("SELECT * FROM mesh_tasks")],
                               default=str))
    blob = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert cap.encode() not in blob, "raw capability persisted to disk (DB/WAL/spool/claim store)"
    for s in surfaces:
        assert cap not in s
    w.db.close_session_turns(WRK)
    assert _send(w, cap, MGR, op="revoked").status_code == 401
    assert len(_agent_rows(w.db, MGR)) == 2


# --------------------------------------------------------------------------- #
# Stage 5 rework (review round 1) — inverted reviewer probes P1/P2/P3 + F3/F5
# --------------------------------------------------------------------------- #
NEW_MGR = "mgr-new"


def _new_manager_session(w: World, sid: str = NEW_MGR) -> None:
    w.db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=w.node,
    ))
    w.db.enroll_session(sid)


def _revoked(w: World, cap: str) -> bool:
    row = w.db._conn().execute(
        "SELECT revoked_at FROM mesh_sender_capabilities WHERE token_hash = ?", (_sha(cap),),
    ).fetchone()
    return row is None or bool(row[0])


def _raw_sql(w: World, sql: str, *params: Any) -> None:
    conn = w.db._conn()
    conn.execute(sql, params)
    conn.commit()


def _after_validate(w: World, monkeypatch, mutate) -> None:
    """Race a state change into the window between the route's capability
    validation and the admission transaction."""
    orig = w.db.validate_sender_capability

    def racing(raw: str, target: str) -> Any:
        ident = orig(raw, target)
        mutate()
        return ident

    monkeypatch.setattr(w.db, "validate_sender_capability", racing)


def test_AUTH07_respawned_manager_loses_sender_authority(tmp_path, monkeypatch):
    """F1 (P1 inverted): a respawn rebinding the Case's Manager revokes the
    dead Manager's capability in the same txn; the dead Manager is no longer a
    permitted recipient; the new Manager mints and sends normally."""
    from src.control.db import RESPAWN_ACTION

    w = _mk_world(tmp_path, monkeypatch)
    cap_m = _cap(w, "m-t1", MGR)
    cap_w = _cap(w, "w-t1", WRK)
    _new_manager_session(w)
    tok = f"respawn:{w.case_a}:1"
    w.db.enqueue_task(tok, None, None, "claude", RESPAWN_ACTION,
                      {"case_id": w.case_a, "generation": 1, "dead_session_id": MGR})
    w.db.record_respawn_link(tok, case_id=w.case_a, new_session_id=NEW_MGR,
                             dead_session_id=MGR, generation=1)
    assert w.db.case_manager_session_id(w.case_a) == NEW_MGR
    assert _revoked(w, cap_m)
    r = _send(w, cap_m, WRK, op="old-mgr")
    assert r.status_code == 401 and r.headers.get("WWW-Authenticate") == "AITeamSender"
    assert _send(w, cap_w, MGR, op="to-dead").status_code == 403
    cap_n = _cap(w, "n-t1", NEW_MGR)
    assert _send(w, cap_n, WRK, op="new-mgr").status_code == 202
    assert _send(w, cap_w, NEW_MGR, op="to-new").status_code == 202
    assert [r["sender_session_id"] for r in _agent_rows(w.db, WRK)] == [NEW_MGR]
    assert _agent_rows(w.db, MGR) == []


def test_AUTH07b_manager_link_rebind_revokes_old_manager(tmp_path, monkeypatch):
    """F1: the generic ``create_flow_link(..., 'manager')`` rebind path (legacy
    respawn) revokes the superseded Manager's capability too."""
    w = _mk_world(tmp_path, monkeypatch)
    cap_m = _cap(w, "m-t1", MGR)
    cap_w = _cap(w, "w-t1", WRK)
    _new_manager_session(w)
    w.db.create_flow_link(w.case_a, "session", NEW_MGR, "manager", created_by="system")
    w.db.set_session_case(NEW_MGR, w.case_a, "manager")
    assert _revoked(w, cap_m)
    assert _send(w, cap_m, WRK, op="old-mgr").status_code == 401
    assert _send(w, cap_w, MGR, op="to-old").status_code == 403
    cap_n = _cap(w, "n-t1", NEW_MGR)
    assert _send(w, cap_n, WRK, op="new-mgr").status_code == 202
    # Re-linking the CURRENT Manager (idempotent) never revokes it.
    w.db.create_flow_link(w.case_a, "session", NEW_MGR, "manager", created_by="system")
    assert not _revoked(w, cap_n)


def test_AUTH07c_superseded_manager_rule_holds_without_revocation(tmp_path, monkeypatch):
    """F1 (b): even if some rebind path forgot to revoke, validation refuses a
    Manager sender / Manager recipient that is not the Case's latest Manager."""
    w = _mk_world(tmp_path, monkeypatch)
    cap_m = _cap(w, "m-t1", MGR)
    cap_w = _cap(w, "w-t1", WRK)
    _new_manager_session(w)
    _raw_sql(w, "UPDATE sessions SET current_case_id = ?, case_role = 'manager' WHERE session_id = ?",
             w.case_a, NEW_MGR)
    _raw_sql(w, "INSERT INTO flow_links (flow_run_id, entity_type, entity_id, role, created_at) "
                "VALUES (?, 'session', ?, 'manager', ?)", w.case_a, NEW_MGR, NOW)
    assert not _revoked(w, cap_m)
    assert _send(w, cap_m, WRK, op="old-mgr").status_code == 401
    assert _send(w, cap_w, MGR, op="to-old").status_code == 403
    assert _agent_rows(w.db, WRK) == [] and _agent_rows(w.db, MGR) == []


@pytest.mark.parametrize("who", ["sender", "target"])
def test_AUTH07d_admission_recheck_refuses_superseded_manager(tmp_path, monkeypatch, who):
    """F1 (b) in the admission txn: a Manager rebind landing between validation
    and admission ⇒ superseded Manager sender 401 / recipient 403."""
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "m-t1", MGR) if who == "sender" else _cap(w, "w-t1", WRK)
    target = WRK if who == "sender" else MGR
    _new_manager_session(w)

    def rebind() -> None:
        _raw_sql(w, "UPDATE sessions SET current_case_id = ?, case_role = 'manager' WHERE session_id = ?",
                 w.case_a, NEW_MGR)
        _raw_sql(w, "INSERT INTO flow_links (flow_run_id, entity_type, entity_id, role, created_at) "
                    "VALUES (?, 'session', ?, 'manager', ?)", w.case_a, NEW_MGR, NOW)

    _after_validate(w, monkeypatch, rebind)
    r = _send(w, cap, target, op="race")
    assert r.status_code == (401 if who == "sender" else 403), r.text
    assert _agent_rows(w.db, target) == []


@pytest.mark.parametrize("change", ["carrier_replaced", "revoked", "rotated"])
def test_AUTH08_admission_rechecks_capability_row(tmp_path, monkeypatch, change):
    """F2 (P2 inverted): carrier replacement / revocation / rotation between
    validation and admission ⇒ 401 and no agent row."""
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "w-t1", WRK)
    mutate = {
        "carrier_replaced": lambda: _register(w.task, w.node, "inc-2"),
        "revoked": lambda: w.db.revoke_sender_capabilities(WRK),
        "rotated": lambda: _claim(w, "w-t1").json()["sender_capability"]["token"],
    }[change]
    _after_validate(w, monkeypatch, mutate)
    r = _send(w, cap, MGR, op="race")
    assert r.status_code == 401, r.text
    assert r.headers.get("WWW-Authenticate") == "AITeamSender"
    assert _agent_rows(w.db, MGR) == []


def test_AUTH08b_admission_request_carries_hash_not_secret():
    """F2: the admission request carries only the capability hash, repr-safe."""
    from src.control.turn_admission import AdmissionRequest
    from src.control.turn_queue import SenderIdentity

    ident = SenderIdentity(session_id=WRK, case_id="c", role="worker", capability_hash="h" * 64)
    assert "h" * 64 not in repr(ident)
    req = AdmissionRequest(
        session_id=MGR, body="b", payload={}, turn_source="agent", operation_id="o",
        idempotency_scope="s", admission_hash="a", sender_session_id=WRK,
        sender_capability_hash="h" * 64,
    )
    assert "h" * 64 not in repr(req)


@pytest.mark.parametrize("sql,expected", [
    ("UPDATE sessions SET status = 'closed' WHERE session_id = 'wrk-1'", 401),
    ("UPDATE sessions SET current_case_id = NULL WHERE session_id = 'wrk-1'", 401),
    ("UPDATE sessions SET turn_queue_enrolled = 0 WHERE session_id = 'wrk-1'", 401),
    ("UPDATE flow_runs SET status = 'closed'", 401),
    # A recipient closed inside the window hits the generic managed-admission
    # closed-recipient refusal (409, every source) before the sender recheck.
    ("UPDATE sessions SET status = 'closed' WHERE session_id = 'mgr-1'", 409),
    ("UPDATE sessions SET current_case_id = NULL WHERE session_id = 'mgr-1'", 403),
    ("UPDATE sessions SET case_role = 'reviewer' WHERE session_id = 'mgr-1'", 403),
])
def test_AUTH09_admission_recheck_status_codes(tmp_path, monkeypatch, sql, expected):
    """F3: a sender-side change (closed / left Case / unenrolled / Case closed)
    inside the admission window is a stale credential (401); a target-side or
    role-pair change stays 403. Raw SQL so no revocation hides the recheck."""
    w = _mk_world(tmp_path, monkeypatch)
    cap = _cap(w, "w-t1", WRK)
    _after_validate(w, monkeypatch, lambda: _raw_sql(w, sql))
    r = _send(w, cap, MGR, op="race")
    assert r.status_code == expected, r.text
    if expected == 401:
        assert r.headers.get("WWW-Authenticate") == "AITeamSender"
    assert _agent_rows(w.db, MGR) == []


def test_AUTH10_unauthenticated_garbage_body_is_401_not_422(tmp_path, monkeypatch):
    """F4 (P3 inverted): auth is enforced before the body is read/decoded."""
    w = _mk_world(tmp_path, monkeypatch)
    url = f"/api/sessions/{MGR}/turn-requests"
    jh = {"Content-Type": "application/json"}
    assert w.api.post(url, content=b"{not json", headers=jh).status_code == 401
    r = w.api.post(url, content=b"{not json", headers={**jh, "Authorization": "AITeamSender bogus"})
    assert r.status_code == 401 and r.headers.get("WWW-Authenticate") == "AITeamSender"
    assert w.api.post(url, content=b"{not json",
                      headers={**jh, "Authorization": "Bearer wrong"}).status_code == 401
    # Authenticated callers still get the validation error for a bad body.
    assert w.api.post(url, content=b"{not json",
                      headers={**jh, "Authorization": f"Bearer {ADMIN}"}).status_code == 422
    cap = _cap(w, "w-t1", WRK)
    assert w.api.post(url, content=b"{not json",
                      headers={**jh, "Authorization": f"AITeamSender {cap}"}).status_code == 422
    assert w.api.post(url, json={"body": "x", "operation_id": "o", "source": "system"},
                      headers={"Authorization": f"AITeamSender {cap}", "Idempotency-Key": "o"}
                      ).status_code == 422
    assert _agent_rows(w.db, MGR) == []


def test_AUTH11_sender_http_refuses_redirects():
    """F5: the tool's HTTP choke point never follows a redirect (which urllib
    would re-send as GET with the Authorization header to any host)."""
    import http.server
    import threading

    hits: List[Dict[str, str]] = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *a: Any) -> None:
            return None

    sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    sink_url = f"http://127.0.0.1:{sink.server_address[1]}/stolen"

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(int(self.path.rsplit("/", 1)[-1]))
            self.send_header("Location", sink_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            return None

    red = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for srv in (sink, red):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{red.server_address[1]}"
        for code in (301, 302, 303, 307, 308):
            status, _ = agent_sender._http_post(
                f"{base}/r/{code}", b"{}", {"Authorization": "AITeamSender s3cret",
                                            "Content-Type": "application/json"}, 5.0)
            assert status == code
        assert hits == []
    finally:
        for srv in (sink, red):
            srv.shutdown()
            srv.server_close()
