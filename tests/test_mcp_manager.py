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
    assert names == {"dispatch_worker", "wait_for_worker"}


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
