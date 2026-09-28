"""Offline contract tests for the canonical live activity boundary."""

import asyncio
import sys
from types import SimpleNamespace

from src.backends.claude_driver import _claude_activity_tool, _make_activity_cb, _SDKSession
from src.backends.codex_native import _publish_codex_activity
from src.core.activity import BackendActivity, publish_activity
from src.core.telemetry import TelemetryContext


def test_shared_activity_requires_scoped_ids_and_allowlisted_values(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "src.core.observability.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    assert publish_activity(
        session_id="session-1", task_id="task-1", category="tool_started", tool="Bash"
    )
    assert emitted == [(
        ("task_activity",),
        {"session_id": "session-1", "task_id": "task-1", "turn_id": "task-1", "label": "Using Bash"},
    )]
    assert not publish_activity(session_id="session-1", task_id=None, category="writing")
    assert not publish_activity(session_id=" ", task_id="task-1", category="writing")
    assert not publish_activity(
        session_id="session-1", task_id="task-1", category="tool_started", tool="Bash SECRET"
    )
    assert not publish_activity(
        session_id="session-1", task_id="task-1", category="writing", tool="Bash"
    )
    try:
        BackendActivity(
            session_id="session-1", task_id="task-1", category="writing",
            payload={"text": "SECRET"},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("raw payload metadata was accepted")
    assert "SECRET" not in str(emitted)


def test_activity_model_bounds_identifiers_and_rejects_unknown_fields():
    assert BackendActivity(session_id="s", task_id="t", category="thinking").label == "Thinking…"
    try:
        BackendActivity(session_id="s" * 129, task_id="t", category="thinking")
    except ValueError:
        pass
    else:
        raise AssertionError("oversized session id was accepted")
    try:
        BackendActivity(session_id="s", task_id="t", category="thinking", command="secret")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown metadata was accepted")


def test_activity_emission_failure_does_not_escape_backend(monkeypatch):
    def fail_event(*_args, **_kwargs):
        raise OSError("event file unavailable")

    monkeypatch.setattr("src.core.observability.emit_event", fail_event)
    assert not publish_activity(
        session_id="session-1", task_id="task-1", category="writing"
    )


def test_claude_sdk_mapping_and_callback_emit_only_canonical_label(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "src.core.observability.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    assert _claude_activity_tool("Bash") == "Bash"
    assert _claude_activity_tool("mcp__jobs__dispatch_worker") is None
    callback = _make_activity_cb("session-1", "task-1")
    callback("tool_started", _claude_activity_tool("Bash"))
    callback("writing")
    assert [event[1]["label"] for event in emitted] == ["Using Bash", "Writing response…"]
    assert all(event[1]["session_id"] == "session-1" and event[1]["task_id"] == "task-1" for event in emitted)
    assert not _make_activity_cb("session-1", None)


def test_claude_sdk_stream_maps_fake_content_blocks_without_copying_text(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "src.core.observability.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ThinkingBlock:
        pass

    class ToolUseBlock:
        def __init__(self):
            self.name = "bash"
            self.input = {"command": "SECRET_COMMAND"}

    class AssistantMessage:
        def __init__(self):
            self.content = [ToolUseBlock(), ThinkingBlock(), TextBlock("SECRET_MODEL_OUTPUT")]

    class ResultMessage:
        pass

    sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        ToolUseBlock=ToolUseBlock,
        ThinkingBlock=ThinkingBlock,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    session = _SDKSession("gateway-session", "/repo", None, {})
    callback = _make_activity_cb("gateway-session", "task-current")

    class Client:
        async def receive_messages(self):
            yield AssistantMessage()
            session._pending.clear()

    session._client = Client()
    session._pending.append(SimpleNamespace(progress_cb=callback))
    asyncio.run(session._reader_loop())

    assert [event[1]["label"] for event in emitted] == [
        "Using Bash", "Thinking…", "Writing response…"
    ]
    assert all(
        event[1]["session_id"] == "gateway-session" and event[1]["task_id"] == "task-current"
        for event in emitted
    )
    assert "SECRET_COMMAND" not in str(emitted)
    assert "SECRET_MODEL_OUTPUT" not in str(emitted)


def test_codex_item_mapping_uses_context_and_ignores_unknown_or_malformed(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "src.core.observability.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    context = TelemetryContext(
        turn_id="task-9", invocation_id="inv-9", node_id="worker", session_id="session-9"
    )
    item = {"id": "native-item", "type": "commandExecution", "command": "SECRET_COMMAND"}
    _publish_codex_activity(context, "item/started", item)
    _publish_codex_activity(context, "item/completed", item)
    _publish_codex_activity(context, "item/started", {"type": {"bad": "shape"}})
    _publish_codex_activity(context, "item/started", {"type": "unknown", "text": "SECRET_TEXT"})
    _publish_codex_activity(None, "item/started", item)
    assert [event[1]["label"] for event in emitted] == ["Using Bash", "Finished Bash"]
    assert all(event[1]["session_id"] == "session-9" and event[1]["task_id"] == "task-9" for event in emitted)
    assert "SECRET" not in str(emitted)


def test_existing_activity_api_accepts_publisher_envelope(monkeypatch):
    from src.control.task_server import ActivityPayload, submit_activity

    emitted = []
    monkeypatch.setattr(
        "src.core.observability.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    result = submit_activity(ActivityPayload(
        node_id="worker", session_id="session-1", task_id="task-1",
        turn_id="task-1", label="Using Bash",
    ))
    assert result == {"accepted": True}
    assert emitted == [(
        ("task_activity",),
        {"node_id": "worker", "session_id": "session-1", "task_id": "task-1",
         "turn_id": "task-1", "label": "Using Bash"},
    )]
