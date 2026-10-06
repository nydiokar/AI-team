"""A97 + A78 §3: a failed turn's error class comes from what actually failed.

1. Text in which the agent merely WROTE about timeouts / "503" / usage limits
   (its reply: ``output``, assistant events in ``raw_stdout``, ``assistant_text``
   in ``parsed_output``) never changes the class.
2. A genuine provider refusal on the default SDK path (a rejected
   ``RateLimitEvent`` and/or ``api_error_status == 429``) still classifies
   ``usage_limit`` and its ``resetsAt`` reaches the quota store
   (``_usage_limit_class`` -> ``record_refusal_snapshot``).
3. One failure-label marker set.

No paid backend is touched: the SDK stream is fed real wire dicts through the
SDK's own ``parse_message``.
"""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_agent_sdk._internal.message_parser import parse_message

from src.backends import claude_driver
from src.backends.claude_driver import ClaudeSDKClientDriver, _SDKSession, classify_error_text
from src.core.failure_markers import (
    QUOTA_FAILURE_LABEL_MARKERS,
    mark_reply_mirror,
    RATE_LIMIT_MARKERS,
    USAGE_LIMIT_MARKERS,
)
from src.core.interfaces import ExecutionResult, TaskResult
from src.orchestrator import TaskOrchestrator
from src.services import result_text

RESETS_AT = 1791300000  # 2026-10-06T14:00:00Z


def _result(**kw) -> TaskResult:
    base = dict(
        task_id="t", success=False, output="", errors=[], files_modified=[],
        execution_time=1.0, timestamp="2026-10-06T12:00:00+00:00",
    )
    base.update(kw)
    return TaskResult(**base)


def _orch(store=None) -> TaskOrchestrator:
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.quota_coordinator = SimpleNamespace(store=store) if store is not None else None
    return orch


def _classify(result: TaskResult, store=None) -> str:
    return TaskOrchestrator._classify_error(_orch(store), result)


class _FakeStore:
    def __init__(self, raises: bool = False):
        self.calls = []
        self.raises = raises

    def record_refusal_snapshot(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise RuntimeError("telemetry db is locked")
        return True


# --------------------------------------------------------------------------- #
# 1. The agent's own words never change the class                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reply, wrong_class", [
    ("I added a retry with a 30s timeout around the HTTP client.", "timeout"),
    ("The upstream returns 503 when the pool is drained; I handle it now.", "network"),
    ("I documented the usage limit and the session limit in the README.", "usage_limit"),
    ("I implemented the rate limit middleware.", "rate_limit"),
])
def test_reply_text_does_not_change_the_class(reply, wrong_class):
    """The real error is a plain fatal; the reply only TALKS about something
    else — in ``output``, in the stream's assistant events, and in a parsed
    result's ``assistant_text``. Every one of these used to be classified by
    the reply."""
    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply}),
    ])
    for result in (
        _result(output=reply, errors=["Tool process crashed: segfault"]),
        _result(output=reply, errors=["Claude exited with code 1"], raw_stdout=stream),
        _result(errors=["Claude exited with code 1"],
                parsed_output={"type": "result", "is_error": False, "assistant_text": reply}),
    ):
        assert _classify(result) == "fatal", (reply, wrong_class)
        # ...and the user-facing label agrees with the class.
        assert "usage limit" not in TaskOrchestrator._short_failure_reason(result).lower()
        assert "usage limit" not in result_text.short_failure_reason(result).lower()


def test_reply_mentioning_usage_limit_does_not_pause_a_case():
    """The worst case in the issue: a reply that mentions "usage limit" landed
    in QUOTA_PAUSE_ERROR_CLASSES and paused a whole Manager Case."""
    from src.orchestrator import QUOTA_PAUSE_ERROR_CLASSES
    result = _result(
        output="Next step: raise the usage limit in config, then rerun.",
        errors=["permission denied: /etc/shadow"],
    )
    assert _classify(result) not in QUOTA_PAUSE_ERROR_CLASSES
    assert _classify(result) == "auth"


def test_error_only_in_output_keeps_todays_class():
    """No error-bearing field at all (no errors, no stderr, no error event):
    the reply is the only evidence and is still read (A97 TASK 2 fallback)."""
    assert _classify(_result(output="Request timed out after 600s")) == "timeout"
    assert _classify(_result(output="You've hit your limit · resets 5pm")) == "usage_limit"
    assert _classify(_result(raw_stdout="connection reset by peer")) == "network"


def test_error_events_in_the_stream_are_still_read():
    """The print_resume CLI path: errors only says the exit code, the reply
    talks about timeouts, and the stream's is_error result carries the real
    refusal text — that is evidence, the assistant prose is not."""
    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "fixing the timeout"}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": True,
                    "result": "You've hit your session limit · resets 4:40pm"}),
    ])
    result = _result(errors=["Claude exited with code 1"], raw_stdout=stream, output="fixing the timeout")
    assert _classify(result) == "usage_limit"
    synthetic = _result(
        errors=["Claude exited with code 1"],
        raw_stdout='{"type":"assistant","message":{"content":[{"type":"text","text":"API Error"}]},"error":"rate_limit"}',
    )
    assert _classify(synthetic) == "rate_limit"


@pytest.mark.parametrize("text, expected", [
    ("upstream said HTTP 503 Service Unavailable", "network"),
    ("gateway error (504)", "network"),
    ("error 503: try again", "network"),
    ("checksum mismatch in abc5031", "fatal"),
    ("failed to open src/503/handler.py", "fatal"),
    ("unsupported schema v1.504", "fatal"),
])
def test_bare_status_codes_are_word_bounded(text, expected):
    assert _classify(_result(errors=[text])) == expected


def test_allowed_warning_rate_limit_event_is_not_a_refusal():
    """An ``allowed_warning`` event (approaching the limit) carries
    ``overageStatus`` but is not a refusal: it must not classify the failure as
    a quota pause, nor reach the quota store."""
    store = _FakeStore()
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "five_hour",
        "resetsAt": RESETS_AT, "overageStatus": "allowed"}})
    result = _result(errors=["Claude exited with code 1"], raw_stdout=line)
    assert _classify(result, store) == "fatal"
    assert store.calls == []


# --------------------------------------------------------------------------- #
# 2. Structured backend class is preferred and survives the result path        #
# --------------------------------------------------------------------------- #

def test_structured_backend_class_is_preferred_over_text():
    """The mesh path: raw_stdout mirrors the reply, so the backend's own class
    is the only structure left. It outranks the text."""
    assert _classify(_result(error_class="upstream_error", errors=["request timed out"])) == "upstream_error"
    assert _classify(_result(error_class="max_turns", errors=["rate limit"])) == "max_turns"
    assert _classify(_result(error_class="sdk_stream_closed", errors=["Cannot write to terminated process"])) == "sdk_stream_closed"
    assert _classify(_result(error_class="usage_limit", errors=["backend error result"])) == "usage_limit"


def test_unknown_backend_class_is_still_rederived_from_text():
    """Classes outside the classifier's vocabulary have no retry-policy entry;
    they keep today's text re-derivation (no policy change in this fix)."""
    assert _classify(_result(error_class="session_lost", errors=["request timed out"])) == "timeout"
    assert _classify(_result(error_class="backend_error", errors=["boom"])) == "fatal"


def test_run_backend_local_keeps_the_backend_error_class():
    """``_run_backend_local`` used to rebuild the TaskResult without
    ``error_class``, so the structured class never reached ``_classify_error``."""
    raw = ExecutionResult(success=False, output="", errors=["x"], error_class="upstream_error")
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._backends = {"claude": SimpleNamespace(run_oneoff=lambda cwd, prompt: raw)}
    orch._task_cancel_events = {}
    orch._running_exec_tasks = {}
    task = SimpleNamespace(id="t-local", prompt="p", metadata={"cwd": "/repo"})
    result = asyncio.run(orch._run_backend_local(task, None, "claude"))
    assert result.error_class == "upstream_error"


# --------------------------------------------------------------------------- #
# 3. A real SDK refusal: RateLimitEvent mirrored, usage_limit, store learns it #
# --------------------------------------------------------------------------- #

def _sdk_turn(*wire_messages):
    """Run the driver's real reader loop over SDK messages parsed from the
    CLI's wire dicts; return the TurnOutcome it dispatched."""
    session = _SDKSession("gw-session", "/repo", None, {})
    outcomes = []
    session._dispatch = outcomes.append  # type: ignore[assignment]

    class _Client:
        async def receive_messages(self):
            for wire in wire_messages:
                msg = parse_message(wire)
                if msg is not None:
                    yield msg

    session._client = _Client()
    asyncio.run(session._reader_loop())
    assert len(outcomes) == 1
    return outcomes[0]


def _rate_limit_wire(status: str) -> dict:
    return {"type": "rate_limit_event", "uuid": "u-rl", "session_id": "sid-1",
            "rate_limit_info": {"status": status, "rateLimitType": "five_hour",
                                "resetsAt": RESETS_AT, "overageStatus": "rejected"}}


def _result_wire(**kw) -> dict:
    base = {"type": "result", "subtype": "success", "duration_ms": 10,
            "duration_api_ms": 5, "is_error": True, "num_turns": 1, "session_id": "sid-1"}
    base.update(kw)
    return base


def _through_driver_and_orchestrator(outcome) -> TaskResult:
    """ExecutionResult from the driver's real error branch, then the
    orchestrator's real ``_run_backend_local`` TaskResult construction."""
    with patch.object(_SDKSession, "send", lambda self_inner, message, **_kw: outcome), \
            patch.object(_SDKSession, "start", lambda self_inner: None), \
            patch("src.core.test_guard.assert_live_calls_allowed", lambda *a, **k: None):
        raw = ClaudeSDKClientDriver().run_oneoff("/repo", "do it")
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._backends = {"claude": SimpleNamespace(run_oneoff=lambda cwd, prompt: raw)}
    orch._task_cancel_events = {}
    orch._running_exec_tasks = {}
    task = SimpleNamespace(id="t-sdk", prompt="p", metadata={"cwd": "/repo"})
    return asyncio.run(orch._run_backend_local(task, None, "claude"))


def test_sdk_429_with_rate_limit_event_records_refusal_snapshot():
    """The default ``driver_type="sdk"`` path: the CLI emits a rejected
    rate_limit_event, the agent narrated about timeouts, and the terminal
    result is a 429 whose text is unrelated. Class: usage_limit; the provider's
    resetsAt reaches the quota store."""
    outcome = _sdk_turn(
        {"type": "assistant", "message": {"model": "m", "content": [
            {"type": "text", "text": "Working on the timeout handling now."}]},
         "session_id": "sid-1"},
        _rate_limit_wire("rejected"),
        _result_wire(api_error_status=429, result=""),
    )
    assert outcome.error_class == "usage_limit"
    mirrored = [json.loads(l) for l in outcome.raw_ndjson.splitlines()
                if json.loads(l).get("type") == "rate_limit_event"]
    assert mirrored and mirrored[0]["rate_limit_info"]["resetsAt"] == RESETS_AT

    result = _through_driver_and_orchestrator(outcome)
    store = _FakeStore()
    assert _classify(result, store) == "usage_limit"
    assert len(store.calls) == 1
    assert store.calls[0]["reset_at"] == datetime.fromtimestamp(RESETS_AT, tz=timezone.utc)
    assert store.calls[0]["provider"] == "claude"
    assert "5-hour usage limit reached" in TaskOrchestrator._short_failure_reason(result)


def test_sdk_rejected_event_without_429_is_usage_limit():
    """A refusal with no api_error_status and no limit wording in the result
    text is still classified by the structured event — in the driver and in
    the orchestrator (which also records the snapshot)."""
    outcome = _sdk_turn(_rate_limit_wire("rejected"), _result_wire(result="API Error"))
    assert outcome.error_class == "usage_limit"
    store = _FakeStore()
    assert _classify(_through_driver_and_orchestrator(outcome), store) == "usage_limit"
    assert len(store.calls) == 1


def test_sdk_429_with_empty_text_and_no_event_is_usage_limit():
    outcome = _sdk_turn(_result_wire(api_error_status=429, result=""))
    assert outcome.error_class == "usage_limit"
    assert _classify(_through_driver_and_orchestrator(outcome)) == "usage_limit"


def test_sdk_allowed_warning_event_is_not_mirrored_or_flagged():
    outcome = _sdk_turn(_rate_limit_wire("allowed_warning"), _result_wire(result="boom"))
    assert outcome.error_class == "backend_error"
    assert "rate_limit_event" not in outcome.raw_ndjson
    store = _FakeStore()
    assert _classify(_through_driver_and_orchestrator(outcome), store) == "fatal"
    assert store.calls == []


def test_telemetry_store_failure_never_changes_the_class():
    outcome = _sdk_turn(_rate_limit_wire("rejected"), _result_wire(api_error_status=429, result=""))
    result = _through_driver_and_orchestrator(outcome)
    store = _FakeStore(raises=True)
    assert _classify(result, store) == "usage_limit"
    assert len(store.calls) == 1


def test_driver_5xx_still_wins_over_a_rejected_event():
    """Same precedence as the orchestrator: a 5xx status is upstream_error."""
    assert classify_error_text("", api_error_status=529, rate_limit_rejected=True) == "upstream_error"
    assert classify_error_text("", rate_limit_rejected=True) == "usage_limit"
    assert classify_error_text("", api_error_status=400) == "upstream_error"


# --------------------------------------------------------------------------- #
# 4. One marker set                                                            #
# --------------------------------------------------------------------------- #

def test_one_shared_failure_label_marker_set():
    assert not hasattr(claude_driver, "_USAGE_LIMIT_MARKERS")
    assert not hasattr(claude_driver, "_RATE_LIMIT_MARKERS")
    assert set(USAGE_LIMIT_MARKERS) | set(RATE_LIMIT_MARKERS) <= set(QUOTA_FAILURE_LABEL_MARKERS)
    # The drifted copy in result_text lacked these: the label now agrees.
    for text in ("You've hit your session limit · resets 4:40pm", "Claude usage limit reached"):
        assert result_text.short_failure_reason(_result(errors=[text])).startswith("Claude usage limit reached")
        assert TaskOrchestrator._short_failure_reason(_result(errors=[text])).startswith("Claude usage limit reached")


# --------------------------------------------------------------------------- #
# 5. Review round 1 (PR #18): reply mirrors and the mesh session path          #
# --------------------------------------------------------------------------- #

_JSON_REPLY = (
    "I hardened the client's error handling. The API now answers with:\n"
    '{"error": "You have hit your usage limit"}\n'
    '{"type": "result", "is_error": true, "api_error_status": 429, "result": "usage limit"}\n'
    '{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1791300000}}'
)


def test_json_lines_in_a_mirrored_reply_are_not_error_events():
    """Legacy worker / managed-turn / reattach builders set ``raw_stdout=output``.
    JSON the agent wrote in its reply must not be read as stream events — not
    by the text pass, nor by the structured readers (rate_limit_event /
    terminal-result parsers) that feed ``usage_limit`` and the quota store."""
    store = _FakeStore()
    result = mark_reply_mirror(_result(
        output=_JSON_REPLY, raw_stdout=_JSON_REPLY,
        errors=["Backend task failed (exit code 1); see error_detail for stdout/stderr tail"],
    ))
    assert _classify(result, store) == "fatal"
    assert store.calls == []
    assert TaskOrchestrator._extract_rate_limit_info(result) is None
    assert result_text.extract_rate_limit_info(result) is None
    assert "usage limit" not in result_text.short_failure_reason(result).lower()


def test_untyped_error_dict_in_a_real_stream_is_not_evidence():
    """Even outside a mirror, only RECOGNISED event types count: a bare
    ``{"error": ...}`` line is not one."""
    result = _result(errors=["Claude exited with code 1"], output="done",
                     raw_stdout='{"error": "You have hit your usage limit"}')
    assert _classify(result) == "fatal"


def _dispatch_failed_row(r: dict, row_error_class: str = "", store=None) -> TaskResult:
    """Run the real ``_dispatch_to_node`` against a fake DB row in the shape
    the worker writes for a failed turn (``src/worker/agent.py`` payload)."""
    row = {"id": "t-mesh", "status": "failed", "result": json.dumps(r),
           "error": "worker failed", "backend": "claude", "error_class": row_error_class}
    db = SimpleNamespace(get_task=lambda task_id: row)
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._nudge_worker_for_dispatch = lambda *a, **k: None
    orch._task_cancel_events = {}
    orch.quota_coordinator = SimpleNamespace(store=store) if store is not None else None
    task = SimpleNamespace(id="t-mesh", prompt="p", metadata={})
    with patch("src.control.db.get_db", return_value=db):
        return asyncio.run(orch._dispatch_to_node(task, None, SimpleNamespace(node_id="w1")))


@pytest.mark.parametrize("err", [
    "SDK stream ended (normal EOF from SDK stream; missing terminal ResultMessage)",
    "ProcessError: Command failed with exit code 1 — Cannot write to terminated process",
    "TimeoutError: read timed out",
])
def test_mesh_session_path_keeps_the_workers_structured_class(err):
    """Session turns run through ``_dispatch_to_node``; it used to drop the
    worker's ``error_class``, so a worker-side ``sdk_stream_closed`` was
    re-derived from its text (``fatal`` / ``network`` / ``timeout``)."""
    result = _dispatch_failed_row({
        "success": False, "output": "partial work", "errors": [err],
        "raw_stderr": "error_class=sdk_stream_closed", "error_class": "sdk_stream_closed",
    })
    assert result.error_class == "sdk_stream_closed"
    assert _classify(result) == "sdk_stream_closed"


def test_mesh_session_path_falls_back_to_the_row_error_class():
    result = _dispatch_failed_row(
        {"success": False, "output": "", "errors": ["API Error: 529 Overloaded"]},
        row_error_class="upstream_error",
    )
    assert _classify(result) == "upstream_error"


def test_mesh_legacy_worker_mirror_with_json_reply_is_not_a_pause():
    """A legacy worker ships no raw_stdout, so the gateway mirrors ``output``
    into it — the reviewer's scenario, through the real builder."""
    from src.orchestrator import QUOTA_PAUSE_ERROR_CLASSES
    result = _dispatch_failed_row({"success": False, "output": _JSON_REPLY, "errors": ["Backend task failed (exit code 1)"]})
    assert result.raw_stdout == _JSON_REPLY
    assert _classify(result) not in QUOTA_PAUSE_ERROR_CLASSES


def test_print_resume_stream_used_as_output_is_still_read():
    """print_resume sets ``output = stdout`` when nothing in the stream is
    extractable, but it also parsed the terminal result into
    ``parsed_output``: that stdout is a real stream, not a reply mirror."""
    from src.backends.claude_driver import _parse_print_resume
    stream = "\n".join([
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": RESETS_AT}}),
        json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                    "errors": ["[ede_diagnostic] result_type=assistant"]}),
    ])
    raw = _parse_print_resume(stream, "", 1, 1.0)
    assert raw.output == stream.strip()
    result = _result(output=raw.output, errors=raw.errors, raw_stdout=raw.raw_stdout,
                     parsed_output=raw.parsed_output)
    store = _FakeStore()
    assert _classify(result, store) == "usage_limit"
    assert len(store.calls) == 1


# --------------------------------------------------------------------------- #
# 6. Review round 2 (PR #18): the gateway<->worker seam                       #
# --------------------------------------------------------------------------- #

def _worker_payload(raw: ExecutionResult) -> dict:
    """The fields ``src/worker/agent.py`` ships for a failed turn."""
    from src.worker.agent import _backend_error_detail, _bound_output
    return {
        "success": False, "output": _bound_output(raw.output or ""), "errors": list(raw.errors or []),
        "error_detail": _backend_error_detail(raw),
        "raw_stdout": _bound_output(raw.raw_stdout or ""), "raw_stderr": _bound_output(raw.raw_stderr or ""),
        "error_class": raw.error_class or "", "return_code": raw.return_code,
    }


@pytest.mark.parametrize("with_result_line", [False, True])
def test_mesh_print_resume_stream_as_output_keeps_the_refusal(with_result_line):
    """print_resume sets ``output = stdout`` when the stream holds no
    extractable text, and the worker ships ``output`` and ``raw_stdout``
    byte-identical with no ``parsed_output``. That is a REAL stream: the
    rejected rate_limit_event must still classify ``usage_limit`` and reach
    the store (equality alone used to mark it a mirror)."""
    from src.backends.claude_driver import _parse_print_resume
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s"}),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": RESETS_AT}}),
    ]
    if with_result_line:
        lines.append(json.dumps({"type": "result", "subtype": "error_during_execution",
                                 "is_error": True, "result": ""}))
    raw = _parse_print_resume("\n".join(lines), "", 1, 1.0)
    assert raw.output == raw.raw_stdout.strip()
    store = _FakeStore()
    result = _dispatch_failed_row(_worker_payload(raw), store=store)
    assert _classify(result, store) == "usage_limit"
    assert len(store.calls) == 1
    assert store.calls[0]["reset_at"] == datetime.fromtimestamp(RESETS_AT, tz=timezone.utc)


def test_mesh_error_detail_stdout_tail_is_not_error_text():
    """Worker stderr empty ⇒ the gateway falls back to ``error_detail`` for
    ``raw_stderr``. Its ``stdout_tail`` is the print_resume stream with the
    agent's own words; only the error parts may be read."""
    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text",
                    "text": "I reworked the quota banner so a usage limit shows the reset time."}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}),
    ])
    raw = ExecutionResult(success=False, output="I reworked the quota banner.",
                          errors=["Tool permission denied: Bash"], raw_stdout=stream,
                          raw_stderr="", return_code=1)
    payload = _worker_payload(raw)
    assert "usage limit" in payload["error_detail"]
    payload.pop("raw_stderr")  # legacy workers ship none; new ones ship "" — same fallback
    result = _dispatch_failed_row(payload)
    assert "usage limit" not in result.raw_stderr
    assert "exit_code=1" in result.raw_stderr
    assert _classify(result) == "auth"
    assert "usage limit" not in TaskOrchestrator._short_failure_reason(result).lower()
