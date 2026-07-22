"""Unit tests for scripts/mcp_manager.py (M3 Phase 3.0 tool surface).

HERMETIC: no network, no paid CLI, no gateway. The single HTTP choke point
(`_api_request`) is monkeypatched, and the .env bootstrap is neutralised by
pointing AI_TEAM_ENV_FILE at a nonexistent path BEFORE import.

Run: `pytest tests/test_mcp_manager.py` (plain pytest — respects the cost guard).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# Neutralise the .env bootstrap so importing the module reads no real secrets.
os.environ["AI_TEAM_ENV_FILE"] = "/nonexistent/mcp_manager_test.env"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

mcp_manager = importlib.import_module("mcp_manager")


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    # A token so _api_request (when not fully stubbed) doesn't short-circuit.
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    yield


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_classify_status_done():
    for s in ("closed", "completed", "done", "failed", "error", "cancelled", "CANCELED"):
        assert mcp_manager.classify_status(s) == "done"


def test_classify_status_attention():
    for s in ("blocked", "rework_requested", "needs_decision", "review", "in_review"):
        assert mcp_manager.classify_status(s) == "attention"


def test_classify_status_active_and_unknown():
    assert mcp_manager.classify_status("running") == "active"
    assert mcp_manager.classify_status("") == "unknown"
    assert mcp_manager.classify_status(None) == "unknown"


def test_bounded_text_required_and_limits():
    assert mcp_manager._bounded_text("  hi ", "x", 10) == "hi"
    with pytest.raises(ValueError):
        mcp_manager._bounded_text(None, "x", 10)
    with pytest.raises(ValueError):
        mcp_manager._bounded_text("   ", "x", 10)
    with pytest.raises(ValueError):
        mcp_manager._bounded_text("toolong", "x", 3)
    assert mcp_manager._bounded_text(None, "x", 10, required=False) is None


def test_bounded_files():
    assert mcp_manager._bounded_files(["a.py", " b.py "]) == ["a.py", "b.py"]
    assert mcp_manager._bounded_files(None) is None
    assert mcp_manager._bounded_files([]) is None
    with pytest.raises(ValueError):
        mcp_manager._bounded_files("notalist")
    with pytest.raises(ValueError):
        mcp_manager._bounded_files(["x"] * (mcp_manager._MAX_FILES + 1))


def test_clamp_float():
    assert mcp_manager._clamp_float(None, 5, 1, 10) == 5
    assert mcp_manager._clamp_float(100, 5, 1, 10) == 10
    assert mcp_manager._clamp_float(0, 5, 1, 10) == 1
    assert mcp_manager._clamp_float("garbage", 5, 1, 10) == 5


# --------------------------------------------------------------------------
# dispatch_worker
# --------------------------------------------------------------------------

def test_dispatch_worker_posts_and_reports(monkeypatch):
    calls = {}

    def fake_request(method, path, payload=None, timeout=20.0):
        calls["method"] = method
        calls["path"] = path
        calls["payload"] = payload
        return {"ok": True, "task_id": "task_abc", "session": {"session_id": "sess_1"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({
        "objective": "Fix the widget",
        "session_id": "sess_1",
        "cwd": "/repo",
        "files": ["a.py", "b.py"],
    })
    assert calls["method"] == "POST"
    assert calls["path"] == "/api/instructions"
    assert calls["payload"] == {
        "description": "Fix the widget",
        "session_id": "sess_1",
        "cwd": "/repo",
        "target_files": ["a.py", "b.py"],
    }
    assert "task_abc" in out
    assert "wait_for_worker" in out


def test_dispatch_worker_sends_parent_lineage(monkeypatch):
    """[A32/A33] Endpoint now accepts parent_flow_run_id, so dispatch_worker sends
    it as the Manager→worker lineage edge (persisted server-side when
    HARNESS_FLOW_DRIVE is ON)."""
    seen = {}

    def fake_request(method, path, payload=None, timeout=20.0):
        seen["payload"] = payload
        return {"task_id": "t1", "session": None}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({"objective": "do x", "parent_flow_run_id": "flow_parent"})
    assert seen["payload"]["parent_flow_run_id"] == "flow_parent"
    # Surfaced in the reply as a lineage edge (SHADOW record — not the old "not persisted" note).
    assert "flow_parent" in out
    assert "lineage edge" in out
    assert "NOT yet persisted" not in out


def test_dispatch_worker_omits_parent_lineage_when_absent(monkeypatch):
    """No parent_flow_run_id ⇒ the key is not in the payload (byte-identical to a
    plain dispatch; no null/empty field leaks)."""
    seen = {}

    def fake_request(method, path, payload=None, timeout=20.0):
        seen["payload"] = payload
        return {"task_id": "t1", "session": None}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    mcp_manager._dispatch_worker({"objective": "do x"})
    assert "parent_flow_run_id" not in seen["payload"]


def test_dispatch_worker_requires_objective(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_api_request", lambda *a, **k: {"task_id": "x"})
    with pytest.raises(ValueError):
        mcp_manager._dispatch_worker({})


def test_dispatch_worker_opens_observable_session_when_cwd_and_no_session(monkeypatch):
    """[DROP-2] No session_id + a cwd ⇒ open a REAL worker session first
    (POST /api/sessions), then submit the objective INTO it joined to the Case.
    Proves the worker is observable (a session row), not a sessionless one-off."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        if path == "/api/sessions":
            return {"ok": True, "session": {"session_id": "worker_sess_9"}}
        return {"ok": True, "task_id": "task_w", "session": {"session_id": "worker_sess_9"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({
        "objective": "Implement T1",
        "cwd": "/repo",
        "case_id": "case_1",
        "node_id": "kanebra-worker",
    })

    # First call opens the session (rooted at the repo, pinned to the node).
    # backend is MANDATORY — CreateSessionBody.backend has no default, so omitting
    # it 422s and silently drops back to a legacy one-off (the A44 live defect).
    assert calls[0][0] == "POST" and calls[0][1] == "/api/sessions"
    assert calls[0][2] == {"repo_path": "/repo", "backend": "claude", "node_id": "kanebra-worker"}
    # Second call submits INTO that session, joined to the Manager's Case.
    assert calls[1][1] == "/api/instructions"
    assert calls[1][2]["session_id"] == "worker_sess_9"
    assert calls[1][2]["case_id"] == "case_1"
    assert "observable worker session" in out
    assert "worker_sess_9" in out


def test_dispatch_worker_reuses_given_session_without_creating(monkeypatch):
    """A supplied session_id ⇒ NO session is created; single submit call
    (byte-identical to the reuse path)."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "task_id": "t", "session": {"session_id": "sess_existing"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({
        "objective": "reuse me", "session_id": "sess_existing", "cwd": "/repo",
    })
    assert [c[1] for c in calls] == ["/api/instructions"]
    assert "reused existing session" in out


def test_dispatch_worker_tiers_model_on_new_session(monkeypatch):
    """[Cockpit] model reaches the NEW worker session via CreateSessionBody.model
    (the create seam), NOT /api/instructions — the supported per-job tiering path
    that replaces `claude -p --model` via watch_job."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        if path == "/api/sessions":
            return {"ok": True, "session": {"session_id": "w_opus"}}
        return {"ok": True, "task_id": "t", "session": {"session_id": "w_opus"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({
        "objective": "hard design", "cwd": "/repo", "case_id": "c1", "model": "opus",
    })
    # model lands in the session-create body...
    assert calls[0][1] == "/api/sessions"
    assert calls[0][2]["model"] == "opus"
    # ...and NOT in the instruction body (that field would be dropped).
    assert "model" not in calls[1][2]
    assert "opus" in out and "boots on it" in out


def test_dispatch_worker_model_ignored_on_reused_session(monkeypatch):
    """A reused session_id keeps its boot model — model is NOT applied and the reply
    says so honestly (no silent no-op)."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "task_id": "t", "session": {"session_id": "sess_existing"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({
        "objective": "reuse", "session_id": "sess_existing", "model": "opus",
    })
    # No session created, so no model plumbing happened.
    assert [c[1] for c in calls] == ["/api/instructions"]
    assert "NOT applied" in out


def test_dispatch_worker_falls_back_to_oneoff_without_cwd(monkeypatch):
    """No session_id AND no cwd ⇒ cannot root a session; honest fallback to the
    legacy one-off (single /api/instructions call, no session_id). The reply says so."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "task_id": "t", "session": None}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({"objective": "no repo"})
    assert [c[1] for c in calls] == ["/api/instructions"]
    assert "session_id" not in calls[0][2]
    assert "one-off" in out


def test_dispatch_worker_warm_reuse_after_case_close(monkeypatch):
    """[A48] A worker whose Case has closed stays WARM (its affiliation is cleared,
    not its process). A follow-up dispatch by session_id reuses it — NO new session
    is opened (a single /api/instructions call). This proves warm re-dialogue is
    still available after Case close."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "task_id": "t2", "session": {"session_id": "warm_worker"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker({
        "objective": "follow-up turn", "session_id": "warm_worker", "cwd": "/repo",
    })
    # No POST /api/sessions — the warm session is reused, no cold re-open.
    assert [c[1] for c in calls] == ["/api/instructions"]
    assert calls[0][2]["session_id"] == "warm_worker"
    assert "reused existing session" in out


# --------------------------------------------------------------------------
# release_worker  (A48 — the Manager's explicit worker-close decision)
# --------------------------------------------------------------------------

def _affil(session_id="w1", role="worker", case_id="case_1"):
    """One affiliation index response with a single row for the target session."""
    return {"affiliations": [
        {"session_id": session_id, "flow_run_id": case_id, "role": role,
         "objective_lock": "obj", "case_status": None},
    ], "total": 1}


def test_release_worker_closes_verified_worker_of_own_case(monkeypatch):
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        if path == "/api/work/affiliations/sessions":
            return _affil(session_id="w1", role="worker", case_id="case_1")
        return {"ok": True, "reason": None, "session": {"session_id": "w1"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._release_worker({"session_id": "w1", "case_id": "case_1"})
    # First the ownership guard reads the affiliation index, THEN the close.
    assert calls == [
        ("GET", "/api/work/affiliations/sessions", None),
        ("POST", "/api/sessions/w1/close", None),
    ]
    assert "Released worker session w1" in out
    assert "CLOSED" in out


def test_release_worker_requires_session_id(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_api_request",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    with pytest.raises(ValueError):
        mcp_manager._release_worker({"case_id": "case_1"})


def test_release_worker_requires_case_id(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_api_request",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    with pytest.raises(ValueError):
        mcp_manager._release_worker({"session_id": "w1"})


def test_release_worker_refuses_unknown_session(monkeypatch):
    """No affiliation row for the target ⇒ structured refusal, NO close attempted."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append(path)
        if path == "/api/work/affiliations/sessions":
            return _affil(session_id="other", role="worker", case_id="case_1")
        raise AssertionError(f"must not call {path}")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._release_worker({"session_id": "ghost", "case_id": "case_1"})
    assert calls == ["/api/work/affiliations/sessions"]
    assert "REFUSED" in out
    assert "not an affiliated session" in out


def test_release_worker_refuses_non_worker_role(monkeypatch):
    """The target is affiliated but not a worker (e.g. a manager) ⇒ refusal, no close."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append(path)
        if path == "/api/work/affiliations/sessions":
            return _affil(session_id="m1", role="manager", case_id="case_1")
        raise AssertionError(f"must not call {path}")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._release_worker({"session_id": "m1", "case_id": "case_1"})
    assert calls == ["/api/work/affiliations/sessions"]
    assert "REFUSED" in out
    assert "'manager'" in out
    assert "not 'worker'" in out


def test_release_worker_refuses_worker_of_other_case(monkeypatch):
    """The target is a worker but joined to a DIFFERENT Case ⇒ refusal, no close."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append(path)
        if path == "/api/work/affiliations/sessions":
            return _affil(session_id="w1", role="worker", case_id="case_OTHER")
        raise AssertionError(f"must not call {path}")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._release_worker({"session_id": "w1", "case_id": "case_1"})
    assert calls == ["/api/work/affiliations/sessions"]
    assert "REFUSED" in out
    assert "case_OTHER" in out


def test_release_worker_reports_refusal_on_close_404(monkeypatch):
    """[Defect 3] The backend /close raises HTTPError 404 → _api_request raises
    RuntimeError for an already-closed/unknown session. That must return the SAME
    structured-refusal shape, not leak an exception (the old else-branch was dead)."""
    def fake_request(method, path, payload=None, timeout=20.0):
        if path == "/api/work/affiliations/sessions":
            return _affil(session_id="w1", role="worker", case_id="case_1")
        raise RuntimeError("HTTP 404 on POST /api/sessions/w1/close: session_not_found")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._release_worker({"session_id": "w1", "case_id": "case_1"})
    assert "did NOT close" in out
    assert "404" in out


# --------------------------------------------------------------------------
# wait_for_worker
# --------------------------------------------------------------------------

def test_wait_requires_an_id():
    with pytest.raises(ValueError):
        mcp_manager._wait_for_worker({})


def test_wait_resolves_task_to_flow_then_returns_on_done(monkeypatch):
    seq = []

    def fake_request(method, path, payload=None, timeout=20.0):
        seq.append(path)
        if path.startswith("/api/flows?task_id="):
            return {"flows": [{"flow_run_id": "flow_1"}]}
        if path == "/api/flows/flow_1":
            return {"flow": {"status": "completed", "current_stage": "close"}}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._wait_for_worker({"task_id": "task_abc", "timeout": 5})
    assert "DONE" in out
    assert "flow_1" in out
    assert any(p.startswith("/api/flows?task_id=") for p in seq)


def test_wait_returns_on_attention(monkeypatch):
    def fake_request(method, path, payload=None, timeout=20.0):
        return {"flow": {"status": "blocked", "current_stage": "impl"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._wait_for_worker({"flow_run_id": "flow_9", "timeout": 5})
    assert "ATTENTION" in out
    assert "Needs attention" in out


def test_wait_returns_on_task_finished_event(monkeypatch):
    """[A37] Honest closure: the worker flow's status never flips on task-end, so
    wait_for_worker must terminate on the authoritative `task.finished` event."""
    def fake_request(method, path, payload=None, timeout=20.0):
        if path.startswith("/api/flows/"):
            return {"flow": {"status": None, "current_stage": "execution"}}
        if path.startswith("/api/work/") and path.endswith("/timeline"):
            return {"events": [
                {"event_type": "task.attached", "entity_id": "task_abc"},
                {"event_type": "task.finished", "entity_id": "task_abc",
                 "payload_json": '{"outcome": "success"}'},
            ]}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._wait_for_worker(
        {"flow_run_id": "flow_1", "task_id": "task_abc", "timeout": 5})
    assert "DONE (task.finished)" in out
    assert "remains OPEN" in out


def test_wait_task_finished_failure_is_attention(monkeypatch):
    def fake_request(method, path, payload=None, timeout=20.0):
        if path.startswith("/api/flows/"):
            return {"flow": {"status": None, "current_stage": "execution"}}
        if path.endswith("/timeline"):
            return {"events": [
                {"event_type": "task.finished", "entity_id": "task_z",
                 "payload_json": '{"outcome": "failed", "error_class": "timeout"}'},
            ]}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._wait_for_worker({"flow_run_id": "flow_1", "timeout": 5})
    assert "ATTENTION (task.finished)" in out
    assert "FAILED" in out


def test_wait_times_out_without_busy_loop(monkeypatch):
    """Active-forever flow must hit the timeout branch and must sleep between
    polls (no CPU-pegging busy loop)."""
    sleeps = []
    monkeypatch.setattr(mcp_manager.time, "sleep", lambda s: sleeps.append(s))

    def fake_request(method, path, payload=None, timeout=20.0):
        return {"flow": {"status": "running"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._wait_for_worker({"flow_run_id": "flow_x", "timeout": 2, "poll_interval": 1})
    assert "TIMEOUT" in out
    assert sleeps, "wait loop must sleep between polls"


def test_wait_tolerates_transient_poll_errors(monkeypatch):
    """[A33] A transient gateway blip mid-poll must NOT abort the wait — the poll
    recovers and returns DONE once the gateway responds again."""
    monkeypatch.setattr(mcp_manager.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_request(method, path, payload=None, timeout=20.0):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("Could not reach control API: transient blip")
        return {"flow": {"status": "completed", "current_stage": "close"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._wait_for_worker({"flow_run_id": "flow_1", "timeout": 30, "poll_interval": 1})
    assert "DONE" in out
    assert calls["n"] >= 3  # recovered after the transient failures


def test_wait_gives_up_after_persistent_errors(monkeypatch):
    """[A33] Persistent poll failures give up after the consecutive-error cap with
    a clean ERROR (not a raised exception), well before a long timeout expires."""
    monkeypatch.setattr(mcp_manager.time, "sleep", lambda s: None)

    def always_fail(method, path, payload=None, timeout=20.0):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(mcp_manager, "_api_request", always_fail)
    out = mcp_manager._wait_for_worker({"flow_run_id": "flow_1", "timeout": 3600, "poll_interval": 1})
    assert "ERROR" in out
    assert "gave up" in out
    assert "gateway down" in out


def test_api_request_requires_token(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("WORKER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TOKEN not set"):
        mcp_manager._api_request("GET", "/api/flows")


# --------------------------------------------------------------------------
# MCP protocol surface
# --------------------------------------------------------------------------

def test_dispatch_tools_list(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_manager, "_send", lambda o: sent.append(o))
    mcp_manager._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = sent[0]["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"dispatch_worker", "wait_for_worker", "open_case", "get_case",
                     "read_session_history", "close_case", "record_review", "release_worker"}


def test_dispatch_tool_call_success(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_manager, "_send", lambda o: sent.append(o))
    monkeypatch.setattr(mcp_manager, "_api_request",
                        lambda *a, **k: {"task_id": "t9", "session": {"session_id": "s"}})
    mcp_manager._dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "dispatch_worker", "arguments": {"objective": "go"}},
    })
    result = sent[0]["result"]
    assert result["content"][0]["type"] == "text"
    assert "t9" in result["content"][0]["text"]
    assert not result.get("isError")


def test_open_case_tool_posts_to_cases(monkeypatch):
    """[M3.3] open_case posts objective+session_id to POST /api/cases and surfaces
    the new case_id — the seam that lets one session own many Cases."""
    calls = []

    def _fake_api(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "case_id": "case-777"}

    monkeypatch.setattr(mcp_manager, "_api_request", _fake_api)
    out = mcp_manager._open_case(
        {"objective": "ship the next thing", "session_id": "sess-1",
         "completion_criteria": "tests green"}
    )
    assert calls == [("POST", "/api/cases",
                      {"objective": "ship the next thing", "session_id": "sess-1",
                       "completion_criteria": "tests green"})]
    assert "case-777" in out


def test_open_case_tool_requires_session_id(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_api_request", lambda *a, **k: {"case_id": "x"})
    with pytest.raises(ValueError):
        mcp_manager._open_case({"objective": "no session"})


def test_dispatch_tool_call_error_is_soft(monkeypatch):
    """A tool raising must become an isError MCP reply, not a crash."""
    sent = []
    monkeypatch.setattr(mcp_manager, "_send", lambda o: sent.append(o))
    mcp_manager._dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "dispatch_worker", "arguments": {}},  # missing objective
    })
    result = sent[0]["result"]
    assert result.get("isError") is True
    assert "Error in dispatch_worker" in result["content"][0]["text"]


def test_dispatch_unknown_tool(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_manager, "_send", lambda o: sent.append(o))
    mcp_manager._dispatch({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert sent[0]["error"]["code"] == -32601


def test_dispatch_initialize():
    sent = []
    orig = mcp_manager._send
    try:
        mcp_manager._send = lambda o: sent.append(o)
        mcp_manager._dispatch({"jsonrpc": "2.0", "id": 0, "method": "initialize"})
    finally:
        mcp_manager._send = orig
    assert sent[0]["result"]["serverInfo"]["name"] == "manager"


def test_read_session_history_formats_turns(monkeypatch):
    calls = {}

    def fake_request(method, path, *a, **k):
        calls["method"] = method
        calls["path"] = path
        return {"messages": [
            {"timestamp": "2026-07-21T10:00:00Z", "instruction": "fix the loader",
             "result": "it double-frees on retry"},
            {"timestamp": "2026-07-21T10:05:00Z", "instruction": "ship the fix",
             "result": "done, tests green"},
        ]}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._read_session_history({"session_id": "sess-abc", "limit": 5})
    assert calls["method"] == "GET"
    assert "/api/sessions/sess-abc/messages?limit=5" in calls["path"]
    assert "You: fix the loader" in out
    assert "Agent: it double-frees on retry" in out
    assert "You: ship the fix" in out and "Agent: done, tests green" in out


def test_read_session_history_empty(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_api_request", lambda *a, **k: {"messages": []})
    out = mcp_manager._read_session_history({"session_id": "sess-x"})
    assert "no conversation turns" in out


def test_read_session_history_clamps_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_manager, "_api_request",
                        lambda m, p, *a, **k: seen.update(path=p) or {"messages": []})
    mcp_manager._read_session_history({"session_id": "s", "limit": 99999})
    assert f"limit={mcp_manager._HISTORY_TURNS_MAX}" in seen["path"]


def test_read_session_history_registered():
    assert "read_session_history" in mcp_manager._TOOL_IMPLS
    assert any(t["name"] == "read_session_history" for t in mcp_manager._TOOLS)
