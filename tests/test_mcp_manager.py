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
    # session_id supplied ⇒ sessionful (SDK) dispatch; also satisfies the ADR-0001 guard.
    out = mcp_manager._dispatch_worker(
        {"objective": "do x", "session_id": "s1", "parent_flow_run_id": "flow_parent"}
    )
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
    mcp_manager._dispatch_worker({"objective": "do x", "session_id": "s1"})
    assert "parent_flow_run_id" not in seen["payload"]


def test_dispatch_worker_refuses_sessionless_dispatch(monkeypatch):
    """[ADR-0001] Neither session_id nor cwd ⇒ REFUSE, because a sessionless task
    runs on the legacy `claude -p` CLI driver (run_oneoff → ClaudePrintResumeDriver),
    bypassing the persistent SDK client and its prompt cache. The guard fires before
    any API call, so no dispatch is emitted."""
    calls = []
    monkeypatch.setattr(
        mcp_manager, "_api_request",
        lambda *a, **k: calls.append(a) or {"task_id": "x"},
    )
    with pytest.raises(ValueError, match="sessionless"):
        mcp_manager._dispatch_worker({"objective": "do x"})
    assert calls == []  # refused before touching the control API


def test_dispatch_worker_requires_objective(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_api_request", lambda *a, **k: {"task_id": "x"})
    with pytest.raises(ValueError):
        mcp_manager._dispatch_worker({})


def test_dispatch_worker_refuses_new_session_without_model_decision(monkeypatch):
    """A new worker must declare its boot model before any control API call."""
    calls = []
    monkeypatch.setattr(
        mcp_manager, "_api_request",
        lambda *a, **k: calls.append(a) or {"task_id": "x"},
    )

    with pytest.raises(ValueError, match="model selection"):
        mcp_manager._dispatch_worker({"objective": "Implement T1", "cwd": "/repo"})

    assert calls == []


def test_dispatch_worker_refuses_unknown_claude_model_before_api_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_manager, "_api_request",
        lambda *a, **k: calls.append(a) or {"task_id": "x"},
    )

    with pytest.raises(ValueError, match="unknown model"):
        mcp_manager._dispatch_worker({
            "objective": "Implement T1", "cwd": "/repo", "model": "not-a-claude-model",
        })

    assert calls == []


def test_dispatch_worker_refuses_malformed_session_create_response(monkeypatch):
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "session": {}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)

    with pytest.raises(RuntimeError, match="session_id"):
        mcp_manager._dispatch_worker({"objective": "Implement T1", "cwd": "/repo", "model": "sonnet"})

    assert [call[1] for call in calls] == ["/api/sessions"]


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
        "node_id": "worker-node",
        "model": "sonnet",
    })

    # First call opens the session (rooted at the repo, pinned to the node).
    # backend is MANDATORY — CreateSessionBody.backend has no default, so omitting
    # it 422s and silently drops back to a legacy one-off (the A44 live defect).
    assert calls[0][0] == "POST" and calls[0][1] == "/api/sessions"
    assert calls[0][2] == {
        "repo_path": "/repo",
        "backend": "claude",
        "node_id": "worker-node",
        "model": "sonnet",
    }
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


def test_dispatch_worker_schema_requires_model_for_new_sessions():
    tool = next(tool for tool in mcp_manager._TOOLS if tool["name"] == "dispatch_worker")
    model = tool["inputSchema"]["properties"]["model"]

    assert "REQUIRED when opening a NEW worker session" in model["description"]
    assert "haiku" in model["description"]
    assert "sonnet" in model["description"]
    assert "opus" in model["description"]


def test_dispatch_worker_refuses_oneoff_without_cwd(monkeypatch):
    """[ADR-0001] No session_id AND no cwd ⇒ REFUSE. Previously this fell back to a
    legacy sessionless one-off, which the orchestrator runs on the `claude -p` CLI
    driver (run_oneoff → ClaudePrintResumeDriver), off the persistent SDK client and
    its prompt cache. The refusal fires before any API call is made."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "task_id": "t", "session": None}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    with pytest.raises(ValueError, match="sessionless"):
        mcp_manager._dispatch_worker({"objective": "no repo"})
    assert calls == []  # nothing dispatched — no CLI one-off emitted


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
    # Derive the expectation from the registry so the list never silently drifts
    # (it previously missed get_case_brief). Every advertised tool must be a
    # registered impl and vice-versa.
    assert names == set(mcp_manager._TOOL_IMPLS)
    assert {"dispatch_worker", "record_review", "publish_spec", "record_spec_review",
            "decompose_case", "publish_artifact"} <= names


def test_dispatch_tool_call_success(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_manager, "_send", lambda o: sent.append(o))
    monkeypatch.setattr(mcp_manager, "_api_request",
                        lambda *a, **k: {"task_id": "t9", "session": {"session_id": "s"}})
    mcp_manager._dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "dispatch_worker",
                   "arguments": {"objective": "go", "cwd": "/repo", "model": "sonnet"}},
    })
    result = sent[0]["result"]
    assert result["content"][0]["type"] == "text"
    assert "t9" in result["content"][0]["text"]
    assert not result.get("isError")


def test_dispatch_tool_call_missing_new_worker_model_is_structured_error(monkeypatch):
    sent = []
    calls = []
    monkeypatch.setattr(mcp_manager, "_send", lambda obj: sent.append(obj))
    monkeypatch.setattr(mcp_manager, "_api_request", lambda *args, **kwargs: calls.append(args))

    mcp_manager._dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "dispatch_worker", "arguments": {"objective": "go", "cwd": "/repo"}},
    })

    result = sent[0]["result"]
    assert result["isError"] is True
    assert "model selection" in result["content"][0]["text"]
    assert calls == []


def test_record_review_forwards_task_id(monkeypatch):
    """A supplied task_id is forwarded in the POST body so the gateway tags the
    verdict to that worker task (the Wake-Dispatcher consumption signal)."""
    calls = []

    def _fake_api(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "event_type": "review.accepted"}

    monkeypatch.setattr(mcp_manager, "_api_request", _fake_api)
    out = mcp_manager._record_review(
        {"case_id": "case-1", "verdict": "accepted", "reason": "ok", "task_id": "task_xyz"}
    )
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/cases/case-1/review"
    assert calls[0][2] == {"verdict": "accepted", "reason": "ok", "task_id": "task_xyz"}
    assert "accepted" in out


def test_record_review_omits_task_id_when_absent(monkeypatch):
    """Without task_id the body carries no task_id key — a Case-level review,
    byte-identical to the pre-tagging contract."""
    calls = []

    def _fake_api(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "event_type": "review.accepted"}

    monkeypatch.setattr(mcp_manager, "_api_request", _fake_api)
    mcp_manager._record_review({"case_id": "case-1", "verdict": "accepted"})
    assert "task_id" not in calls[0][2]


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


# ---------------------------------------------------------------------------
# [A46 / M3.3] durable worker-wait relay — client side
# ---------------------------------------------------------------------------

def test_dispatch_worker_records_durable_wait_when_case(monkeypatch):
    """A worker dispatched INTO a Case also records a durable pending-wait marker
    (POST /api/cases/{case}/waits) so a restart can reconcile it."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        if path == "/api/sessions":
            return {"session": {"session_id": "ws1"}}
        if path.endswith("/waits"):
            return {"ok": True, "event_id": 5}
        return {"task_id": "task_w", "session": {"session_id": "ws1"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker(
        {"objective": "do x", "cwd": "/repo", "case_id": "case_1", "model": "sonnet"}
    )
    waits = [c for c in calls if c[1] == "/api/cases/case_1/waits"]
    assert waits and waits[0][0] == "POST"
    assert waits[0][2] == {"task_id": "task_w"}
    assert "durable wait recorded" in out


def test_dispatch_worker_wait_relay_failure_is_nonfatal(monkeypatch):
    """A relay failure (e.g. the 404 when DURABLE_RELAY_ENABLED is OFF) must NOT
    break the dispatch — the worker is still dispatched, just without the note."""
    def fake_request(method, path, payload=None, timeout=20.0):
        if path.endswith("/waits"):
            raise RuntimeError("HTTP 404 on POST /api/cases/case_1/waits: not_found")
        if path == "/api/sessions":
            return {"session": {"session_id": "ws1"}}
        return {"task_id": "task_w", "session": {"session_id": "ws1"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._dispatch_worker(
        {"objective": "do x", "cwd": "/repo", "case_id": "case_1", "model": "sonnet"}
    )
    assert "task_w" in out                    # dispatch itself succeeded
    assert "durable wait recorded" not in out  # note suppressed on relay failure


def test_dispatch_worker_no_wait_relay_without_case(monkeypatch):
    """No case_id ⇒ no /waits call at all (a worker not joined to a Case has no
    Case ledger to record the wait on)."""
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append(path)
        return {"task_id": "t", "session": {"session_id": "s1"}}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    mcp_manager._dispatch_worker({"objective": "do x", "session_id": "s1"})
    assert not any(p.endswith("/waits") for p in calls)


def test_reconcile_waits_formats_resolved_and_pending(monkeypatch):
    """reconcile_waits summarizes resolved + still-open waits and tells the Manager
    to re-arm wait_for_worker for the open ones."""
    def fake_request(method, path, payload=None, timeout=20.0):
        assert method == "POST" and path == "/api/cases/case_1/waits/reconcile"
        return {
            "ok": True,
            "resolved": [{"task_id": "t_done", "outcome": "success"}],
            "pending": [{"task_id": "t_open", "timeout": 90.0}],
        }

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._reconcile_waits({"case_id": "case_1"})
    assert "t_done" in out and "success" in out
    assert "t_open" in out and "wait_for_worker(task_id='t_open'" in out


def test_reconcile_waits_reports_disabled(monkeypatch):
    """A disabled relay (ok:false) is surfaced, not silently swallowed."""
    monkeypatch.setattr(
        mcp_manager, "_api_request",
        lambda *a, **k: {"ok": False, "reason": "durable_relay_disabled"},
    )
    out = mcp_manager._reconcile_waits({"case_id": "case_1"})
    assert "durable_relay_disabled" in out


def test_reconcile_waits_registered():
    assert "reconcile_waits" in mcp_manager._TOOL_IMPLS
    assert any(t["name"] == "reconcile_waits" for t in mcp_manager._TOOLS)


# --------------------------------------------------------------------------
# Auth: token candidates + 401 retry with the alternate mesh secret
# --------------------------------------------------------------------------

def test_token_candidates_order_and_dedup(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "dash")
    monkeypatch.setenv("WORKER_TOKEN", "work")
    assert mcp_manager._token_candidates() == ["dash", "work"]
    # Identical values de-duplicate to one candidate.
    monkeypatch.setenv("WORKER_TOKEN", "dash")
    assert mcp_manager._token_candidates() == ["dash"]
    # Only the worker secret present (the node case).
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.setenv("WORKER_TOKEN", "work")
    assert mcp_manager._token_candidates() == ["work"]


class _FakeHTTPError(Exception):
    """Stand-in for urllib.error.HTTPError with the bits _api_request reads."""
    def __init__(self, code):
        self.code = code
    def read(self):
        return b'{"detail":"Invalid token"}'


class _FakeResp:
    def __init__(self, body):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_api_request_retries_401_with_alternate_token(monkeypatch):
    """A node whose DASHBOARD_TOKEN is stale/wrong but which holds the shared mesh
    WORKER_TOKEN must still authenticate: a 401 on the first token retries with the
    next candidate."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "stale-dashboard")
    monkeypatch.setenv("WORKER_TOKEN", "good-mesh-secret")
    monkeypatch.setattr(mcp_manager, "_base_url", lambda: "http://gw:9003")
    # Make HTTPError identity match what _api_request catches.
    monkeypatch.setattr(mcp_manager.urllib.error, "HTTPError", _FakeHTTPError)

    seen = []

    def fake_urlopen(req, timeout=0):
        bearer = req.headers.get("Authorization")
        seen.append(bearer)
        if bearer == "Bearer good-mesh-secret":
            return _FakeResp(b'{"ok": true}')
        raise _FakeHTTPError(401)

    monkeypatch.setattr(mcp_manager.urllib.request, "urlopen", fake_urlopen)
    out = mcp_manager._api_request("GET", "/api/flows?limit=1")
    assert out == {"ok": True}
    # Tried the stale dashboard token first, then the good mesh secret.
    assert seen == ["Bearer stale-dashboard", "Bearer good-mesh-secret"]


def test_api_request_raises_when_all_tokens_401(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "a")
    monkeypatch.setenv("WORKER_TOKEN", "b")
    monkeypatch.setattr(mcp_manager, "_base_url", lambda: "http://gw:9003")
    monkeypatch.setattr(mcp_manager.urllib.error, "HTTPError", _FakeHTTPError)
    monkeypatch.setattr(
        mcp_manager.urllib.request, "urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(_FakeHTTPError(401)),
    )
    with pytest.raises(RuntimeError) as ei:
        mcp_manager._api_request("GET", "/api/flows")
    assert "401" in str(ei.value)


# --------------------------------------------------------------------------- #
# [A52] open_case round_cap threading + get_case dual-shape display            #
# --------------------------------------------------------------------------- #

def test_open_case_threads_round_cap_into_body(monkeypatch):
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        return {"ok": True, "case_id": "case_9"}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._open_case({
        "objective": "Ship the thing",
        "session_id": "mgr_1",
        "completion_criteria": "tests green",
        "round_cap": 6,
    })
    assert calls[0][2]["round_cap"] == 6
    assert "round_cap: 6" in out


def test_open_case_rejects_non_positive_round_cap(monkeypatch):
    monkeypatch.setattr(
        mcp_manager, "_api_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not reach API")),
    )
    for bad in (0, -3):
        out = mcp_manager._open_case({
            "objective": "x", "session_id": "mgr_1", "round_cap": bad,
        })
        assert "positive integer" in out


def test_open_case_omits_round_cap_when_absent(monkeypatch):
    calls = []

    def fake_request(method, path, payload=None, timeout=20.0):
        calls.append(payload)
        return {"ok": True, "case_id": "case_9"}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    mcp_manager._open_case({"objective": "x", "session_id": "mgr_1"})
    assert "round_cap" not in calls[0]


def test_get_case_unpacks_dual_shape_criteria(monkeypatch):
    """A Case opened with round_cap stores the object shape; get_case must show the
    human criteria (not a JSON blob) and the cap on its own line."""
    def fake_request(method, path, payload=None, timeout=20.0):
        return {"flow": {
            "status": None,
            "completion_criteria": '{"round_cap": 5, "criteria": "tests green"}',
            "current_stage": "objective_lock",
            "objective_lock": "obj",
        }}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._get_case({"case_id": "case_9"})
    assert "completion_criteria: 'tests green'" in out
    assert "round_cap:" in out and "5" in out
    assert '"round_cap"' not in out  # the raw JSON blob is NOT surfaced


def test_get_case_plain_criteria_unchanged(monkeypatch):
    def fake_request(method, path, payload=None, timeout=20.0):
        return {"flow": {
            "status": None,
            "completion_criteria": "just plain text",
            "current_stage": "objective_lock",
            "objective_lock": "obj",
        }}

    monkeypatch.setattr(mcp_manager, "_api_request", fake_request)
    out = mcp_manager._get_case({"case_id": "case_9"})
    assert "completion_criteria: 'just plain text'" in out
    assert "round_cap:" not in out


# --------------------------------------------------------------------------
# Regression: _bootstrap must put the repo root on sys.path so the lazy
# `from config.models import ...` in dispatch_worker resolves regardless of the
# launching interpreter / cwd. The live MCP server is spawned by the session
# driver with a bare interpreter (no editable install) and a cwd outside the
# repo — that raised `No module named 'config'` and broke every new-worker
# dispatch. Assert the fix directly and deterministically (no interpreter
# assumptions): stripped from sys.path, _bootstrap re-inserts the repo root.
# --------------------------------------------------------------------------

def test_bootstrap_inserts_repo_root_on_sys_path():
    repo_root = str(Path(mcp_manager.__file__).resolve().parent.parent)
    saved = list(sys.path)
    try:
        # Simulate the live spawn: repo root absent from sys.path (bare
        # interpreter, foreign cwd). Without the fix, _bootstrap leaves it absent
        # and `from config.models import ...` in dispatch_worker would raise
        # `No module named 'config'` — this assertion fails.
        sys.path[:] = [p for p in sys.path if p != repo_root]
        assert repo_root not in sys.path
        mcp_manager._bootstrap()
        assert repo_root in sys.path, (
            "_bootstrap must put the repo root on sys.path so config/src resolve "
            "under any launching interpreter"
        )
    finally:
        sys.path[:] = saved


# --------------------------------------------------------------------------
# [O1] Skills library — reference-carried skills expanded on the wire, behind
# the SKILLS_LIBRARY_ENABLED flag (default OFF ⇒ byte-identical dispatch).
# --------------------------------------------------------------------------

def _fake_ok_request(seen):
    def fake_request(method, path, payload=None, timeout=20.0):
        seen.setdefault("calls", []).append((method, path, payload))
        return {"ok": True, "task_id": "task_o1", "session": {"session_id": "sess_1"}}
    return fake_request


def test_resolve_skills_none_and_empty_return_none():
    assert mcp_manager._resolve_skills(None) is None
    assert mcp_manager._resolve_skills([]) is None


def test_resolve_skills_resolves_real_seed_library():
    """The 3 seed skills shipped in skills/ resolve and inject as one block."""
    block = mcp_manager._resolve_skills(
        ["no-false-success", "reuse-before-build", "verify-claims-in-git"]
    )
    assert block is not None
    assert block.startswith("SKILLS —")
    # each seed skill's own H1 header is present in the injected block
    assert "# no-false-success" in block
    assert "# reuse-before-build" in block
    assert "# verify-claims-in-git" in block


def test_resolve_skills_unknown_id_is_structured_error_not_silent():
    with pytest.raises(ValueError, match="unknown skill id"):
        mcp_manager._resolve_skills(["no-such-skill"])


def test_resolve_skills_rejects_traversal_and_bad_charset():
    # traversal / separators / bad charset are refused before any file is touched
    for bad in ["../secrets", "a/b", "has_underscore", "trailing-", "-lead", "a..b"]:
        with pytest.raises(ValueError, match="invalid skill id|resolves outside"):
            mcp_manager._resolve_skills([bad])


def test_resolve_skills_bounds_id_list():
    with pytest.raises(ValueError, match="too many"):
        mcp_manager._resolve_skills(["no-false-success"] * (mcp_manager._MAX_SKILLS + 1))


def test_resolve_skills_rejects_non_list_and_non_string():
    with pytest.raises(ValueError, match="must be a list"):
        mcp_manager._resolve_skills("no-false-success")
    with pytest.raises(ValueError, match="must be a string"):
        mcp_manager._resolve_skills([123])


def test_resolve_skills_dedupes_repeated_id():
    block = mcp_manager._resolve_skills(["no-false-success", "no-false-success"])
    assert block.count("# no-false-success") == 1


def test_resolve_skills_rejects_oversized_and_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_manager, "_SKILLS_DIR", tmp_path)
    (tmp_path / "big.md").write_text("x" * (mcp_manager._MAX_SKILL_FILE_BYTES + 1))
    (tmp_path / "empty.md").write_text("   \n")
    with pytest.raises(ValueError, match="too large"):
        mcp_manager._resolve_skills(["big"])
    with pytest.raises(ValueError, match="empty/malformed"):
        mcp_manager._resolve_skills(["empty"])


def test_dispatch_worker_flag_off_is_byte_identical(monkeypatch):
    """PROOF: with SKILLS_LIBRARY_ENABLED OFF, passing `skills` yields a POST payload
    byte-identical to the same dispatch WITHOUT skills — the flag-off path is unchanged."""
    monkeypatch.delenv("SKILLS_LIBRARY_ENABLED", raising=False)

    seen_no_skills = {}
    monkeypatch.setattr(mcp_manager, "_api_request", _fake_ok_request(seen_no_skills))
    mcp_manager._dispatch_worker({
        "objective": "Fix the widget", "session_id": "sess_1", "cwd": "/repo",
    })

    seen_with_skills = {}
    monkeypatch.setattr(mcp_manager, "_api_request", _fake_ok_request(seen_with_skills))
    mcp_manager._dispatch_worker({
        "objective": "Fix the widget", "session_id": "sess_1", "cwd": "/repo",
        "skills": ["no-false-success", "verify-claims-in-git"],
    })

    # The /api/instructions POST body is identical with and without `skills` when OFF.
    instr_no = [c for c in seen_no_skills["calls"] if c[1] == "/api/instructions"][0]
    instr_yes = [c for c in seen_with_skills["calls"] if c[1] == "/api/instructions"][0]
    assert instr_no[2] == instr_yes[2]
    assert instr_yes[2]["description"] == "Fix the widget"


def test_dispatch_worker_flag_on_expands_skill_text_into_objective(monkeypatch):
    monkeypatch.setenv("SKILLS_LIBRARY_ENABLED", "1")
    seen = {}
    monkeypatch.setattr(mcp_manager, "_api_request", _fake_ok_request(seen))
    out = mcp_manager._dispatch_worker({
        "objective": "Fix the widget", "session_id": "sess_1", "cwd": "/repo",
        "skills": ["no-false-success"],
    })
    instr = [c for c in seen["calls"] if c[1] == "/api/instructions"][0]
    desc = instr[2]["description"]
    assert desc.startswith("SKILLS —")
    assert "# no-false-success" in desc
    assert desc.endswith("Fix the widget")
    assert "expanded into the objective on the wire" in out


def test_dispatch_worker_flag_on_unknown_skill_raises_before_dispatch(monkeypatch):
    monkeypatch.setenv("SKILLS_LIBRARY_ENABLED", "1")
    seen = {}
    monkeypatch.setattr(mcp_manager, "_api_request", _fake_ok_request(seen))
    with pytest.raises(ValueError, match="unknown skill id"):
        mcp_manager._dispatch_worker({
            "objective": "Fix the widget", "session_id": "sess_1", "cwd": "/repo",
            "skills": ["nope"],
        })
    # Nothing was dispatched — the error fired before any /api/instructions POST.
    assert not any(c[1] == "/api/instructions" for c in seen.get("calls", []))
