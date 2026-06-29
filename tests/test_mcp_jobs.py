from __future__ import annotations

from typing import Any


def test_watch_job_defaults_to_agent_followup(monkeypatch, tmp_path):
    from scripts import mcp_jobs

    payloads: list[dict[str, Any]] = []

    def _post_job(payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return {"job_id": "job_abc123"}

    monkeypatch.setenv("WORKER_NODE_ID", "Horse")
    monkeypatch.setenv("SESSION_ID", "sess_123")
    monkeypatch.setattr(mcp_jobs, "_post_job", _post_job)
    monkeypatch.chdir(tmp_path)

    text = mcp_jobs._watch_job({"command": "echo ok", "label": "noop"})

    assert payloads == [{
        "node_id": "Horse",
        "label": "noop",
        "command": "echo ok",
        "cwd": str(tmp_path),
        "session_id": "sess_123",
        "notify": True,
        "notify_agent": True,
    }]
    assert "Agent follow-up: yes" in text
    assert "System > Jobs" in text
    assert "Telegram" not in text


def test_windows_sleep_command_is_normalized(monkeypatch):
    from scripts import mcp_jobs

    monkeypatch.setattr(mcp_jobs.os, "name", "nt")

    command = mcp_jobs._normalize_worker_command(
        "echo Test job started && sleep 10 && echo done"
    )

    assert "sleep 10" not in command
    assert 'powershell -NoProfile -Command "Start-Sleep -Seconds 10"' in command


def test_watch_job_rejects_oversized_command(monkeypatch):
    from scripts import mcp_jobs

    def _post_job(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("invalid payload must not be posted")

    monkeypatch.setattr(mcp_jobs, "_post_job", _post_job)

    try:
        mcp_jobs._watch_job({"command": "x" * 8001, "label": "too big"})
    except ValueError as exc:
        assert "command is too long" in str(exc)
    else:
        raise AssertionError("expected ValueError")
