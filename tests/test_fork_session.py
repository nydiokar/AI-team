"""
Tests for orchestrator.fork_session / _ensure_fork_case (feat/session-fork-case).

Fork = "continue a stalled session as a FRESH native session bound to one Case".
Covers:
- unknown source ⇒ structured refusal (no session created)
- rejected create ⇒ structured refusal surfaced
- happy path with NO source Case ⇒ births a carrier Case, links BOTH sessions as
  role 'session', stamps durable affiliation on both, records session.forked
- happy path WHEN the source already has an open Case ⇒ REUSES it (no new Case),
  links the new session into that same arc
- the carrier Case title defaults from the source when none is given

Real temp MeshDB wired via get_db(); a stub session store stands in for lifecycle.
No paid CLI, no gateway.
"""
from __future__ import annotations

import types

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator
from src.services.session_service import SessionService


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


class _StubStore:
    """Minimal in-memory session store: enough for create/get/save/bind."""

    def __init__(self):
        self._d = {}
        self._n = 0

    def create(self, *, backend, repo_path, telegram_chat_id=None,
               owner_user_id=None, machine_id=None):
        self._n += 1
        sid = f"sess{self._n}"
        s = types.SimpleNamespace(
            session_id=sid, backend=backend, repo_path=repo_path,
            machine_id=machine_id or "", model=None, origin=None,
            current_case_id=None, case_role=None, role_boot=None,
        )
        self._d[sid] = s
        return s

    def get(self, sid):
        return self._d.get(sid)

    def save(self, s):
        self._d[s.session_id] = s

    def bind(self, chat_id, sid):
        pass

    def add(self, s):
        self._d[s.session_id] = s


def _orch(store) -> TaskOrchestrator:
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.session_store = store
    # Permissive repo validator so create_session doesn't reach the real PathResolver.
    orch.session_service = SessionService(store, repo_path_validator=lambda p: None)
    return orch


def _seed_source(store, sid="src") -> str:
    s = types.SimpleNamespace(
        session_id=sid, backend="claude", repo_path="/repo",
        machine_id="", model=None, origin=None,
        current_case_id=None, case_role=None, role_boot=None,
    )
    store.add(s)
    return sid


def _patch_db(monkeypatch, db):
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


def test_unknown_source_is_refused(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    store = _StubStore()
    orch = _orch(store)

    res = orch.fork_session("ghost", backend="claude", repo_path="/repo")

    assert res == {"ok": False, "reason": "session_not_found"}
    # Nothing created.
    assert store._n == 0


def test_bad_backend_create_is_refused(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    store = _StubStore()
    orch = _orch(store)
    _seed_source(store)

    res = orch.fork_session("src", backend="not-a-backend", repo_path="/repo")

    assert res["ok"] is False
    assert res["reason"] == "unknown_backend"


def test_fork_births_carrier_case_and_links_both_sessions(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    store = _StubStore()
    orch = _orch(store)
    _seed_source(store)

    res = orch.fork_session("src", backend="claude", repo_path="/repo", title="hunt the leak")

    assert res["ok"] is True
    new_id = res["new_session_id"]
    case_id = res["case_id"]
    assert new_id and case_id

    # Exactly one carrier Case, objective = the title.
    runs = db.list_flow_runs()
    assert len(runs) == 1
    assert runs[0]["objective_lock"] == "hunt the leak"
    assert runs[0]["status"] is None  # open

    # Both sessions linked as role 'session'.
    links = db.list_flow_links(flow_run_id=case_id, entity_type="session")
    by_id = {l["entity_id"]: l["role"] for l in links}
    assert by_id == {"src": "session", new_id: "session"}

    # Durable affiliation stamped on both.
    assert store.get("src").current_case_id == case_id
    assert store.get(new_id).current_case_id == case_id

    # A session.forked event was recorded on the Case.
    forked = [e for e in db.list_flow_events(case_id) if e["event_type"] == "session.forked"]
    assert len(forked) == 1
    assert forked[0]["entity_id"] == new_id


def test_fork_reuses_existing_open_case(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    store = _StubStore()
    orch = _orch(store)
    _seed_source(store)

    # Source already belongs to an open Case.
    existing = db.open_case("original objective", "src", role="manager")

    res = orch.fork_session("src", backend="claude", repo_path="/repo", title="ignored")

    assert res["ok"] is True
    assert res["case_id"] == existing  # reused, not a new birth

    # Still exactly ONE Case.
    assert len(db.list_flow_runs()) == 1

    # The new session joined the SAME Case as a 'session' member.
    links = db.list_flow_links(flow_run_id=existing, entity_type="session")
    by_id = {l["entity_id"]: l["role"] for l in links}
    assert by_id["src"] == "manager"  # source's original role untouched
    assert by_id[res["new_session_id"]] == "session"


def test_fork_default_title_when_none(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    store = _StubStore()
    orch = _orch(store)
    _seed_source(store)

    res = orch.fork_session("src", backend="claude", repo_path="/repo")

    runs = db.list_flow_runs()
    assert runs[0]["objective_lock"] == "Continuation of session src"
