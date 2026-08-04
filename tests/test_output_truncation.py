"""T2 regression — long task output must not be silently truncated.

The bug: `src/worker/agent.py` capped backend output with a hard `[:4000]`
before it reached the DB, so the gateway never received the tail and its
Telegram splitter (`_split_message`, 4096-char chunks) had nothing to chunk.
The remainder of long results was lost.

These tests pin the fix end-to-end with a FAKE backend (test cost guard — no
paid Claude/Codex CLI is ever invoked):
  - the worker passes the FULL output through to the result dict,
  - the gateway's splitter then chunks it into multiple messages losslessly.
"""
import asyncio

import pytest

from src.core.interfaces import ExecutionResult
from src.telegram.interface import TelegramInterface
from src.worker import agent as worker_agent


# A payload comfortably larger than the old 4000 cap and Telegram's 4096 limit.
_LONG = "".join(f"line-{i:05d} the quick brown fox jumps over the lazy dog\n" for i in range(400))


class _FakeBackend:
    """Stands in for a CodingBackend — returns a fixed long ExecutionResult.

    Never spawns a real CLI, so the test cost guard is honoured.
    """

    def __init__(self, output: str):
        self._output = output

    def run_oneoff(self, cwd, prompt):
        return ExecutionResult(success=True, output=self._output, return_code=0)


class _FakeErrorBackend:
    """Stands in for a failing backend (e.g. the interrupt/salvage path).

    Returns an ExecutionResult with a full transcript + stderr, mirroring the
    99d997c3c8b6 shape where the agent was interrupted mid-response.
    """

    def __init__(self, output: str, raw_stdout: str):
        self._output = output
        self._raw_stdout = raw_stdout

    def run_oneoff(self, cwd, prompt):
        return ExecutionResult(
            success=False,
            output=self._output,
            raw_stdout=self._raw_stdout,
            raw_stderr="error_class=backend_error\nstderr_tail:\nfake stderr tail",
            error_class="backend_error",
            return_code=0,
        )


def _run_task(backends, task_row):
    return asyncio.run(worker_agent._execute_task(task_row, backends))


def test_long_output_loop():
    # sanity: the fixture is actually larger than both caps we care about
    assert len(_LONG) > 4096


def test_worker_does_not_truncate_long_output(monkeypatch):
    """The worker must hand the FULL backend output to the result dict."""
    # Default bound (500k) is far above our payload, so nothing should be cut.
    monkeypatch.delenv("WORKER_MAX_OUTPUT_CHARS", raising=False)
    backends = {"claude": _FakeBackend(_LONG)}
    task_row = {"action": "run_oneoff", "backend": "claude", "payload": {"prompt": "go"}}

    result = _run_task(backends, task_row)

    assert result["success"] is True
    assert result["output"] == _LONG  # nothing lost, no [:4000]
    assert len(result["output"]) > 4096


def test_worker_safety_bound_is_configurable_and_marks_truncation(monkeypatch):
    """A configured bound is a labelled safety cap, not a silent content cut."""
    monkeypatch.setenv("WORKER_MAX_OUTPUT_CHARS", "100")
    backends = {"claude": _FakeBackend(_LONG)}
    task_row = {"action": "run_oneoff", "backend": "claude", "payload": {"prompt": "go"}}

    result = _run_task(backends, task_row)

    out = result["output"]
    assert out.startswith(_LONG[:100])
    assert "output truncated at 100 chars" in out  # explicit, not silent


def test_worker_bound_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("WORKER_MAX_OUTPUT_CHARS", "0")
    backends = {"claude": _FakeBackend(_LONG)}
    task_row = {"action": "run_oneoff", "backend": "claude", "payload": {"prompt": "go"}}

    result = _run_task(backends, task_row)

    assert result["output"] == _LONG


def test_splitter_chunks_full_output_losslessly():
    """The gateway splitter turns the full output into multiple Telegram messages."""
    chunks = TelegramInterface._split_message(_LONG)

    assert len(chunks) > 1  # multiple sequential messages, not one truncated blob
    assert all(len(c) <= 4096 for c in chunks)
    # Nothing lost: rejoining the chunks reproduces the content. The splitter
    # strips leading newlines at chunk boundaries, so compare newline-free.
    assert "".join(chunks).replace("\n", "") == _LONG.replace("\n", "")


def test_worker_output_survives_split_end_to_end(monkeypatch):
    """Full path: worker output (no truncation) → splitter → lossless chunks."""
    monkeypatch.delenv("WORKER_MAX_OUTPUT_CHARS", raising=False)
    backends = {"claude": _FakeBackend(_LONG)}
    task_row = {"action": "run_oneoff", "backend": "claude", "payload": {"prompt": "go"}}

    result = _run_task(backends, task_row)
    chunks = TelegramInterface._split_message(result["output"])

    assert len(chunks) > 1
    assert "".join(chunks).replace("\n", "") == _LONG.replace("\n", "")


def test_worker_error_result_ships_full_payload(monkeypatch):
    """An error turn must carry the FULL backend transcript + stderr to the
    gateway, not just a bounded 4k diagnostic (the 99d997c3c8b6 truncation)."""
    monkeypatch.delenv("WORKER_MAX_OUTPUT_CHARS", raising=False)
    salvaged = "The agent's progress ... the oldest note is at line 419."
    raw_stdout = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"work"}]}}\n'
        '{"type":"result","subtype":"error_during_execution","is_error":true}'
    )
    backends = {"claude": _FakeErrorBackend(salvaged, raw_stdout)}
    task_row = {"action": "run_oneoff", "backend": "claude", "payload": {"prompt": "go"}}

    result = _run_task(backends, task_row)

    assert result["success"] is False
    assert result["output"] == salvaged  # the agent's reply is not eaten
    assert result["raw_stdout"] == raw_stdout  # full NDJSON survives the wire
    assert "error_during_execution" in result["raw_stdout"]
    assert "fake stderr tail" in result["raw_stderr"]
    assert result["error_class"] == "backend_error"
