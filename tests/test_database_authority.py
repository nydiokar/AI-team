"""[A88] Controller/worker database authority.

The worker must never open a mesh.db of its own; controller-owned flags and the
Manager boot reconcile reach the controller over the task-server API.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

import src.control.db as db_mod
import src.control.task_server as ts
from src.control import controller_state
from src.worker.controller_state_client import RemoteControllerState

TOKEN = "a88-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# Flags these tests assert on; a developer .env (loaded into os.environ by config)
# must not leak into the env → default fallback under test.
_ASSERTED_FLAGS = ("QUOTA_PREWARM_ENABLED", "MANAGER_ROLE_ENABLED", "MANAGER_TOOLS_ENABLED", "DURABLE_RELAY_ENABLED")


@pytest.fixture(autouse=True)
def _no_installed_client(monkeypatch: pytest.MonkeyPatch):
    for name in _ASSERTED_FLAGS:
        monkeypatch.delenv(name, raising=False)
    controller_state.uninstall()
    yield
    controller_state.uninstall()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(ts, "_worker_token", lambda: TOKEN)
    return TestClient(ts.app)


class _FakeHTTP:
    """Stands in for the worker's ``_HTTP`` (get/post returning decoded JSON)."""

    def __init__(self) -> None:
        self.get_responses: list[object] = []
        self.post_calls: list[tuple[str, int]] = []
        self.post_response: object = {"ok": True}

    def get(self, path: str, params: dict[str, str] | None = None, timeout: int = 10) -> object:
        assert path == "/control/runtime-flags"
        item = self.get_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, path: str, body: object = None, timeout: int = 10) -> object:
        self.post_calls.append((path, timeout))
        return self.post_response


def _snapshot(rev: str, **flags: str) -> dict[str, JsonValue]:
    return {
        "revision": rev,
        "flags": [{"flag_name": k, "value": v, "set_at": "2026-10-02T00:00:00Z"} for k, v in flags.items()],
    }


# --- seam -------------------------------------------------------------------


def test_installed_client_disables_local_mesh_db(tmp_path: Path) -> None:
    from config import config

    worker_db = tmp_path / "worker" / "mesh.db"
    config.mesh.db_path = str(worker_db)
    db_mod._db_instance = None
    controller_state.install(RemoteControllerState(_FakeHTTP()))

    assert db_mod.get_db() is None
    assert not worker_db.exists()
    assert not worker_db.parent.exists()


def test_flag_rows_come_from_client_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHTTP()
    http.get_responses.append(_snapshot("r1", MANAGER_ROLE_ENABLED="1"))
    remote = RemoteControllerState(http)
    assert remote.refresh() is True
    controller_state.install(remote)

    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "0")
    monkeypatch.setenv("QUOTA_PREWARM_ENABLED", "1")
    assert db_mod.runtime_flag_enabled("MANAGER_ROLE_ENABLED") is True  # controller row wins
    assert db_mod.runtime_flag_enabled("QUOTA_PREWARM_ENABLED") is True  # no row ⇒ env
    monkeypatch.delenv("QUOTA_PREWARM_ENABLED")
    assert db_mod.runtime_flag_enabled("QUOTA_PREWARM_ENABLED") is False  # ⇒ default


def test_controller_process_keeps_local_db_without_client() -> None:
    db = db_mod.get_db()
    assert db is not None
    db.set_runtime_flag("MANAGER_ROLE_ENABLED", True)
    assert db_mod.runtime_flag_enabled("MANAGER_ROLE_ENABLED") is True


# --- task-server routes ------------------------------------------------------


def test_runtime_flags_route_requires_auth(client: TestClient) -> None:
    assert client.get("/control/runtime-flags").status_code in (401, 403)
    assert client.get("/control/runtime-flags", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_runtime_flags_route_serves_registry_and_revision(client: TestClient) -> None:
    db = db_mod.get_db()
    assert db is not None
    first = client.get("/control/runtime-flags", headers=AUTH)
    assert first.status_code == 200
    assert first.json()["flags"] == []

    db.set_runtime_flag("QUOTA_PREWARM_ENABLED", True)
    second = client.get("/control/runtime-flags", headers=AUTH).json()
    assert [(f["flag_name"], f["value"]) for f in second["flags"]] == [("QUOTA_PREWARM_ENABLED", "1")]
    assert second["revision"] != first.json()["revision"]

    again = client.get("/control/runtime-flags", headers=AUTH).json()
    assert again["revision"] == second["revision"]


def test_runtime_flags_route_503_without_db(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "get_db", lambda: None)
    assert client.get("/control/runtime-flags", headers=AUTH).status_code == 503


def test_boot_reconcile_route_guards(client: TestClient) -> None:
    db = db_mod.get_db()
    assert db is not None
    unknown = client.post("/control/cases/case_unknown/boot-reconcile", headers=AUTH)
    assert unknown.status_code == 200 and unknown.json() == {"ok": False, "reason": "unknown_case"}

    case_id = db.open_case("objective", "sess_a88")
    off = client.post(f"/control/cases/{case_id}/boot-reconcile", headers=AUTH).json()
    assert off == {"ok": False, "reason": "durable_relay_disabled"}

    db.set_runtime_flag("DURABLE_RELAY_ENABLED", True)
    on = client.post(f"/control/cases/{case_id}/boot-reconcile", headers=AUTH).json()
    assert on["ok"] is True

    db.update_flow_run(case_id, status="closed")
    closed = client.post(f"/control/cases/{case_id}/boot-reconcile", headers=AUTH).json()
    assert closed == {"ok": False, "reason": "case_closed"}


def test_boot_reconcile_route_rejects_bad_ids(client: TestClient) -> None:
    assert client.post("/control/cases/bad%20id/boot-reconcile", headers=AUTH).status_code == 422
    assert client.post(f"/control/cases/{'x' * 200}/boot-reconcile", headers=AUTH).status_code == 422
    assert client.post("/control/cases/case_x/boot-reconcile").status_code in (401, 403)


# --- worker client -----------------------------------------------------------


def test_client_keeps_last_known_good_on_failure_and_malformed() -> None:
    http = _FakeHTTP()
    http.get_responses += [
        _snapshot("r1", MANAGER_TOOLS_ENABLED="1"),
        OSError("controller down"),
        {"revision": "r2", "flags": "not-a-list"},
        {"unexpected": True},
    ]
    remote = RemoteControllerState(http)
    assert remote.refresh() is True
    assert remote.refresh() is False
    assert remote.refresh() is False
    assert remote.refresh() is False
    row = remote.runtime_flag_row("manager_tools_enabled")
    assert row is not None and row["value"] == "1"
    assert remote.revision == "r1"


def test_client_rejects_oversized_snapshot() -> None:
    http = _FakeHTTP()
    http.get_responses.append({"revision": "r", "flags": [{"flag_name": f"F{i}", "value": "1"} for i in range(1000)]})
    remote = RemoteControllerState(http)
    assert remote.refresh() is False
    assert remote.revision is None


def test_client_never_fetched_returns_no_rows() -> None:
    remote = RemoteControllerState(_FakeHTTP())
    assert remote.runtime_flag_row("MANAGER_ROLE_ENABLED") is None
    assert remote.next_delay() == remote.retry_interval_sec


def test_client_logs_flag_changes_and_staleness(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]
    http = _FakeHTTP()
    http.get_responses += [
        _snapshot("r1", QUOTA_PREWARM_ENABLED="1"),
        _snapshot("r2", QUOTA_PREWARM_ENABLED="0"),
        OSError("down"),
        _snapshot("r2", QUOTA_PREWARM_ENABLED="0"),
    ]
    remote = RemoteControllerState(http, refresh_interval_sec=30, clock=lambda: now[0])
    caplog.set_level(logging.INFO, logger="src.worker.controller_state_client")
    remote.refresh()
    remote.refresh()
    assert "event=controller_flags_changed" in caplog.text and "QUOTA_PREWARM_ENABLED" in caplog.text
    assert remote.next_delay() == 30

    now[0] += 30 * 5 + 1
    remote.refresh()
    remote.check_stale()
    assert "event=controller_state_stale" in caplog.text
    remote.refresh()
    assert "event=controller_state_recovered" in caplog.text


def test_client_logs_missing_route_loudly(caplog: pytest.LogCaptureFixture) -> None:
    http = _FakeHTTP()
    http.get_responses.append(HTTPError("http://c/control/runtime-flags", 404, "Not Found", None, None))  # type: ignore[arg-type]
    remote = RemoteControllerState(http)
    http.get_responses.append(HTTPError("http://c/control/runtime-flags", 404, "Not Found", None, None))  # type: ignore[arg-type]
    caplog.set_level(logging.ERROR, logger="src.worker.controller_state_client")
    assert remote.refresh() is False
    assert remote.refresh() is False
    assert caplog.text.count("event=controller_state_route_missing") == 1  # once per episode


def test_client_boot_reconcile_posts_and_validates_id() -> None:
    http = _FakeHTTP()
    http.post_response = {"ok": True, "rearmed": []}
    remote = RemoteControllerState(http)
    assert remote.boot_reconcile_case("case_abc") == {"ok": True, "rearmed": []}
    assert http.post_calls[0][0] == "/control/cases/case_abc/boot-reconcile"
    assert remote.boot_reconcile_case("../etc") == {"ok": False, "reason": "invalid_case_id"}
    assert len(http.post_calls) == 1


def test_telemetry_sink_has_no_local_mirror_with_client(tmp_path: Path) -> None:
    from config import config
    from src.control.telemetry_sink import (
        DatabaseTelemetrySink,
        FanOutTelemetrySink,
        build_runtime_telemetry_sink,
    )

    config.mesh.db_path = str(tmp_path / "worker" / "mesh.db")
    db_mod._db_instance = None
    controller_state.install(RemoteControllerState(_FakeHTTP()))
    sink = build_runtime_telemetry_sink(
        node_id="w1", base_url="http://controller.example:9002", token=TOKEN,
        logs_dir=str(tmp_path / "logs"), is_gateway=False,
    )
    assert not isinstance(sink, (DatabaseTelemetrySink, FanOutTelemetrySink))
    assert not (tmp_path / "worker").exists()


# --- Manager boot reconcile routing -------------------------------------------


def _manager_session(case_id: str) -> object:
    from types import SimpleNamespace

    from src.core.roles import MANAGER_ROLE_ID

    return SimpleNamespace(case_role=MANAGER_ROLE_ID, current_case_id=case_id, session_id="s1")


def test_worker_hosted_manager_reconciles_via_controller(tmp_path: Path) -> None:
    from config import config
    from src.backends.claude_driver import ClaudeSDKClientDriver

    http = _FakeHTTP()
    http.get_responses.append(_snapshot("r1", DURABLE_RELAY_ENABLED="1"))
    http.post_response = {"ok": True, "reconciled": {"resolved": 0}, "rearmed": []}
    remote = RemoteControllerState(http)
    remote.refresh()
    config.mesh.db_path = str(tmp_path / "worker" / "mesh.db")
    db_mod._db_instance = None
    controller_state.install(remote)

    driver = ClaudeSDKClientDriver.__new__(ClaudeSDKClientDriver)
    driver._boot_reconcile_manager_case(_manager_session("case_abc"))  # type: ignore[arg-type]

    assert [c[0] for c in http.post_calls] == ["/control/cases/case_abc/boot-reconcile"]
    assert not (tmp_path / "worker").exists()


def test_controller_hosted_manager_reconciles_locally() -> None:
    from src.backends.claude_driver import ClaudeSDKClientDriver

    db = db_mod.get_db()
    assert db is not None
    db.set_runtime_flag("DURABLE_RELAY_ENABLED", True)
    case_id = db.open_case("objective", "sess_local")
    calls: list[str] = []
    original = type(db).boot_reconcile_case

    def _spy(self: object, cid: str, *, actor: str = "manager") -> dict[str, object]:
        calls.append(cid)
        return original(self, cid, actor=actor)  # type: ignore[arg-type]

    type(db).boot_reconcile_case = _spy  # type: ignore[method-assign]
    try:
        driver = ClaudeSDKClientDriver.__new__(ClaudeSDKClientDriver)
        driver._boot_reconcile_manager_case(_manager_session(case_id))  # type: ignore[arg-type]
    finally:
        type(db).boot_reconcile_case = original  # type: ignore[method-assign]
    assert calls == [case_id]


def test_worker_install_fetches_once_and_tolerates_outage(tmp_path: Path) -> None:
    from config import config
    from src.worker.agent import _install_controller_state

    config.mesh.db_path = str(tmp_path / "worker" / "mesh.db")
    db_mod._db_instance = None
    http = _FakeHTTP()
    http.get_responses.append(_snapshot("r1", MANAGER_ROLE_ENABLED="1"))
    _install_controller_state(http)  # type: ignore[arg-type]
    assert db_mod.runtime_flag_enabled("MANAGER_ROLE_ENABLED") is True
    assert db_mod.get_db() is None

    controller_state.uninstall()
    down = _FakeHTTP()
    down.get_responses.append(OSError("down"))
    _install_controller_state(down)  # type: ignore[arg-type]  # one attempt, no startup stall
    assert down.get_responses == []
    assert controller_state.active() is not None  # installed even when the controller is down
    assert db_mod.get_db() is None
    assert not (tmp_path / "worker").exists()


# --- reconciliation report -----------------------------------------------------


def test_authority_report_is_read_only_and_classifies(tmp_path: Path) -> None:
    import hashlib
    import sqlite3

    from scripts.db_authority_report import build_report
    from src.control.db import MeshDB

    ctl_path, wrk_path = tmp_path / "ctl.db", tmp_path / "wrk.db"
    ctl, wrk = MeshDB(str(ctl_path)), MeshDB(str(wrk_path))
    for db in (ctl, wrk):
        db.set_runtime_flag("MANAGER_ROLE_ENABLED", True)
    ctl.close()
    wrk.close()

    def _digest() -> str:
        return hashlib.sha256(ctl_path.read_bytes() + wrk_path.read_bytes()).hexdigest()

    before = _digest()
    clean = build_report(ctl_path, wrk_path)
    assert clean.exit_code == 0 and clean.flag_conflicts == [] and clean.tables == []
    assert clean.skipped == []
    assert _digest() == before

    with sqlite3.connect(wrk_path) as conn:
        conn.execute("UPDATE runtime_flags SET value='0' WHERE flag_name='MANAGER_ROLE_ENABLED'")
        conn.execute(
            "INSERT INTO push_subscriptions(endpoint,p256dh_key,auth_key,enabled,created_at,updated_at)"
            " VALUES ('https://push.example/x','k','a',1,'2026-10-02','2026-10-02')"
        )
    report = build_report(ctl_path, wrk_path)
    assert report.exit_code == 3  # flag conflict outranks row divergence
    assert [(c.flag_name, c.controller_value, c.worker_value) for c in report.flag_conflicts] == [
        ("MANAGER_ROLE_ENABLED", "1", "0")
    ]
    push = next(t for t in report.tables if t.table == "push_subscriptions")
    assert push.worker_only == 1
    assert all(not k.startswith("https://") for k in push.sample_keys)  # capability URLs are hashed


def test_worker_claims_no_work_before_flags_are_known() -> None:
    import asyncio

    from src.worker.agent import WorkerAgent

    http = _FakeHTTP()
    http.get_responses += [OSError("down"), _snapshot("r1")]
    remote = RemoteControllerState(http)
    controller_state.install(remote)
    agent = WorkerAgent.__new__(WorkerAgent)

    assert remote.refresh() is False                              # outage at startup
    assert asyncio.run(agent._controller_state_ready()) is False  # claim nothing
    assert len(http.get_responses) == 1                           # gate never fetches itself
    assert remote.refresh() is True                               # refresh loop recovers
    assert asyncio.run(agent._controller_state_ready()) is True


def test_worker_degrades_instead_of_stalling_on_old_controller() -> None:
    import asyncio

    from src.worker.agent import WorkerAgent

    http = _FakeHTTP()
    http.get_responses.append(HTTPError("http://c/control/runtime-flags", 404, "Not Found", None, None))  # type: ignore[arg-type]
    remote = RemoteControllerState(http)
    controller_state.install(remote)
    assert remote.refresh() is False
    agent = WorkerAgent.__new__(WorkerAgent)
    assert asyncio.run(agent._controller_state_ready()) is True
    assert remote.next_delay() == remote.refresh_interval_sec  # no 5 s retry storm


def test_controller_process_never_gates_claims() -> None:
    import asyncio

    from src.worker.agent import WorkerAgent

    agent = WorkerAgent.__new__(WorkerAgent)
    assert asyncio.run(agent._controller_state_ready()) is True


def test_authority_report_never_claims_clean_for_uncompared_tables(tmp_path: Path) -> None:
    import sqlite3

    from scripts.db_authority_report import build_report, main
    from src.control.db import MeshDB

    ctl_path, wrk_path = tmp_path / "ctl.db", tmp_path / "wrk.db"
    MeshDB(str(ctl_path)).close()
    MeshDB(str(wrk_path)).close()
    with sqlite3.connect(wrk_path) as conn:
        conn.execute("CREATE TABLE worker_only_tbl (x TEXT)")
    report = build_report(ctl_path, wrk_path)
    assert report.exit_code == 4
    assert [(s.table, s.reason) for s in report.skipped] == [("worker_only_tbl", "absent from controller")]

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    assert main(["--controller-db", str(garbage), "--worker-db", str(wrk_path)]) == 6


def test_deploy_preflight_installs_client_before_building_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canary preflight builds a WorkerAgent in a child process; it must install the
    controller-state client first, or it recreates the retired worker mesh.db."""
    import scripts.safe_worker_deploy as deploy

    captured: list[list[str]] = []
    monkeypatch.setattr(deploy, "_run", lambda cmd, *a, **k: captured.append(cmd))
    deploy._worker_startup_preflight()
    code = captured[0][-1]
    compile(code, "<preflight>", "exec")
    assert code.index("_install_controller_state(") < code.index("WorkerAgent()")
    assert "route_missing" in code
