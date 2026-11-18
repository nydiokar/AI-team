"""Reader-loop correctness for the SDK driver.

These tests drive the REAL _SDKSession streaming path (reader loop + submit +
FIFO dispatch) with a fake SDK client that emits real claude_agent_sdk message
objects. They pin the two behaviours the rewrite exists for:

1. Replies are matched to the prompt that asked for them — an unsolicited turn
   that lands on the stream between prompts does NOT get served as the reply to
   the next prompt (the "-1 turn offset" bug).
2. That unsolicited turn (a run_in_background job finishing) is delivered to the
   proactive sink instead, so the user still gets it — as its own message.
"""
import asyncio
import threading
import time

import pytest

from src.backends.claude_driver import SDKStreamEndedError, _SDKSession
from src.core.turn_liveness import turn_control

sdk = pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, UserMessage  # noqa: E402


def _assistant(text: str, sid: str = "sid-1") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-test",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id="m",
        stop_reason=None,
        session_id=sid,
        uuid="u",
    )


def _result(text: str, sid: str = "sid-1", is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=sid,
        stop_reason=None,
        total_cost_usd=0.0,
        usage=None,
        result=text,
        structured_output=None,
        model_usage=None,
        permission_denials=[],
        deferred_tool_use=None,
        errors=None,
        api_error_status=None,
        uuid="u",
    )


class _FakeClient:
    """Stands in for ClaudeSDKClient. `replies` maps a prompt to the messages
    the CLI would emit for it; `receive_messages` is the one continuous stream.

    [A102] The single `_SDKSession.send` writes the prompt as an AsyncIterable
    frame carrying the caller's turn uuid and correlates the reply by the CLI's
    `--replay-user-messages` echo — so the fake echoes a `UserMessage` with that
    uuid before the reply frames."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.replies: dict = {}
        self.queries_sent: list = []
        self.query_uuids: list = []

    async def _record(self, message) -> tuple:
        import uuid as _uuid

        if isinstance(message, str):
            text, uid = message, str(_uuid.uuid4())
        else:
            text, uid = None, None
            async for m in message:
                text = m["message"]["content"]
                uid = m.get("uuid") or str(_uuid.uuid4())
        self.queries_sent.append(text)
        self.query_uuids.append(uid)
        return text, uid

    async def query(self, message, session_id: str = "default") -> None:
        text, uid = await self._record(message)
        self.q.put_nowait(UserMessage(content=text, uuid=uid))
        for m in self.replies.get(text, []):
            self.q.put_nowait(m)

    async def receive_messages(self):
        while True:
            yield await self.q.get()


class _ClosingFakeClient(_FakeClient):
    async def query(self, message, session_id: str = "default") -> None:
        text, uid = await self._record(message)
        self.q.put_nowait(UserMessage(content=text, uuid=uid))
        for m in self.replies.get(text, []):
            self.q.put_nowait(m)
        self.q.put_nowait(None)

    async def receive_messages(self):
        while True:
            msg = await self.q.get()
            if msg is None:
                return
            yield msg


def _start_fake_session(fake: _FakeClient) -> _SDKSession:
    """Boot a _SDKSession backed by `fake` on its own loop thread, skipping the
    real SDK connect but running the real reader + submit machinery."""
    sess = _SDKSession("key", "/tmp", None, {})
    sess._client = fake
    sess._replay_user_messages = True  # echo-correlated session (A102 single send)

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sess._loop = loop

        async def boot() -> None:
            sess._reader_task = asyncio.create_task(sess._reader_loop())
            sess._ready.set()
            while not sess._closed:
                await asyncio.sleep(0.02)
            sess._reader_task.cancel()
            # Await the reader so its `finally` fails any still-pending turn,
            # else a thread blocked in send() never returns (interpreter hang).
            try:
                await sess._reader_task
            except BaseException:  # noqa: BLE001
                pass

        loop.run_until_complete(boot())
        loop.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert sess._ready.wait(timeout=3), "fake session never became ready"
    return sess


def _emit_autonomous(sess: _SDKSession, fake: _FakeClient, *msgs) -> None:
    """Push messages onto the stream with NO query — simulates the live session
    continuing on its own after a backgrounded job completes."""
    sess._loop.call_soon_threadsafe(lambda: [fake.q.put_nowait(m) for m in msgs])


def test_usage_is_delivered_before_result_and_only_to_echoed_owner():
    from concurrent.futures import ThreadPoolExecutor

    fake = _FakeClient()
    assistant = _assistant("private response")
    assistant.usage = {"input_tokens": 100, "output_tokens": 8}
    fake.replies["work"] = [assistant]
    sess = _start_fake_session(fake)
    seen = []
    usage_arrived = threading.Event()

    def usage_cb(usage, message_id):
        seen.append((usage, message_id))
        usage_arrived.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sess.send, "work", turn=turn_control("usage-turn"), usage_cb=usage_cb)
            try:
                assert usage_arrived.wait(3)
                assert seen == [({"input_tokens": 100, "output_tokens": 8}, "m")]
                assert not future.done(), "usage must arrive before the turn returns"
            finally:
                _emit_autonomous(sess, fake, _result("finished"))
            assert future.result(timeout=3).output == "finished"
        # No owned turn: these counters must not leak into the previous one.
        _emit_autonomous(sess, fake, assistant, _result("autonomous"))
        settled = threading.Event()
        sess._loop.call_soon_threadsafe(settled.set)
        assert settled.wait(3)
        assert len(seen) == 1
    finally:
        sess.close()


def test_reply_is_not_one_turn_behind_after_a_background_continuation():
    fake = _FakeClient()
    fake.replies["continue the work"] = [
        _assistant("Running backgrounded (~6 min)."),
        _result("Running backgrounded (~6 min)."),
    ]
    fake.replies["what is the result?"] = [
        _result("Here is the real answer to your question."),
    ]

    proactive: list = []
    sess = _start_fake_session(fake)
    sess._on_proactive = lambda key, outcome: proactive.append((key, outcome))

    try:
        # Turn 1: kicks off a background job, ends with the "running" message.
        r1 = sess.send("continue the work", turn=turn_control("u-1"))
        assert r1.output == "Running backgrounded (~6 min)."
        assert r1.proactive is False

        # The background job finishes: the session autonomously produces a
        # follow-up turn on the stream with NO prompt from us.
        _emit_autonomous(sess, fake, _assistant("The job is done — it passed."),
                         _result("The job is done — it passed."))

        # Turn 2: a fresh prompt. Under the old receive_response() code this
        # returned the buffered background result. It must now return the
        # answer to THIS prompt.
        r2 = sess.send("what is the result?", turn=turn_control("u-2"))
        assert r2.output == "Here is the real answer to your question."

        # And the background turn reached the user via the proactive sink.
        deadline = time.time() + 3
        while not proactive and time.time() < deadline:
            time.sleep(0.02)
        assert len(proactive) == 1
        key, outcome = proactive[0]
        assert key == "key"
        assert outcome.output == "The job is done — it passed."
        assert outcome.proactive is True
    finally:
        sess.close()


def test_error_result_surfaces_salvage_and_error_fields():
    fake = _FakeClient()
    fake.replies["do the thing"] = [
        _assistant("I made real progress before overflowing."),
        _result("context length exceeded", is_error=True),
    ]
    sess = _start_fake_session(fake)
    try:
        r = sess.send("do the thing", turn=turn_control("u-err"))
        assert r.is_error is True
        assert r.output == "I made real progress before overflowing."
        assert r.salvaged_output == "I made real progress before overflowing."
        assert r.error_text
    finally:
        sess.close()


def test_stream_eof_before_result_is_typed_and_diagnostic():
    fake = _ClosingFakeClient()
    fake.replies["do the thing"] = [
        _assistant("I changed files but did not emit a terminal result."),
    ]
    sess = _start_fake_session(fake)
    try:
        with pytest.raises(SDKStreamEndedError) as exc:
            sess.send("do the thing", turn=turn_control("u-eof"))
        assert "missing terminal ResultMessage" in str(exc.value)
        assert "normal EOF" in exc.value.reason
    finally:
        sess.close()


def test_context_telemetry_excludes_child_usage_and_preserves_window_and_boundary():
    import json
    from claude_agent_sdk import SystemMessage
    from src.core.telemetry import TelemetryContext
    from src.core.telemetry_adapters.claude_stream_json import ClaudeStreamJsonAdapter
    from src.core.telemetry_projection import project_turn

    fake = _FakeClient()
    parent = _assistant("parent")
    parent.usage = {"input_tokens": 100, "cache_read_input_tokens": 39900,
                    "cache_creation_input_tokens": 0, "output_tokens": 10}
    child = _assistant("child")
    child.message_id = "child"
    child.parent_tool_use_id = "tool-1"
    child.usage = {"input_tokens": 1000, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 20}
    result = _result("done")
    result.usage = parent.usage
    result.total_cost_usd = 1.27
    result.model_usage = {"claude-opus-4": {"contextWindow": 200000}}
    fake.replies["work"] = [SystemMessage(subtype="compact_boundary", data={
        "compact_metadata": {"pre_tokens": 180000, "trigger": "auto"}}), parent, child, result]
    sess = _start_fake_session(fake)
    try:
        outcome = sess.send("work", turn=turn_control("ctx-test"))
        lines = [json.loads(line) for line in outcome.raw_ndjson.splitlines()]
        assert len([line for line in lines if line["type"] == "assistant"]) == 1
        adapter = ClaudeStreamJsonAdapter(TelemetryContext(
            turn_id="turn-ctx", invocation_id="inv-ctx", node_id="node", backend="claude", model="claude-opus-4",
        ), emitter_process_instance_id="proc")
        events = [event for line in outcome.raw_ndjson.splitlines() for event in adapter.consume_line(line)]
        metrics = project_turn(events)["turn"]["metrics"]
        assert metrics["turn_exit_context_tokens"] == 40000
        assert metrics["context_used_ratio"] == .2
        assert metrics["compactions"][0]["before_tokens"] == 180000
        assert metrics["provider_reported_cost_usd"] == 1.27
    finally:
        sess.close()


@pytest.mark.parametrize("pre_key", ["pre_tokens", "preTokens"])
def test_sdk_cost_only_compact_receipt_survives_without_usage(pre_key: str) -> None:
    from claude_agent_sdk import SystemMessage
    from src.core.telemetry import TelemetryContext
    from src.core.telemetry_adapters.claude_stream_json import ClaudeStreamJsonAdapter
    from src.core.telemetry_projection import project_turn

    fake = _FakeClient()
    result = _result("compacted")
    result.usage = {}
    result.total_cost_usd = 0
    fake.replies["/compact"] = [SystemMessage(subtype="compact_boundary", data={
        "compact_metadata": {pre_key: 180000, "trigger": "manual"}}), result]
    sess = _start_fake_session(fake)
    try:
        outcome = sess.send("/compact", turn=turn_control("cost-only-compact"))
        adapter = ClaudeStreamJsonAdapter(TelemetryContext(
            turn_id="compact", invocation_id="inv", node_id="node", backend="claude",
        ), emitter_process_instance_id="proc")
        events = [event for line in outcome.raw_ndjson.splitlines() for event in adapter.consume_line(line)]
        metrics = project_turn(events)["turn"]["metrics"]
        assert metrics["provider_reported_cost_usd"] == 0
        assert metrics["input_tokens"] is None
        assert metrics["turn_exit_context_tokens"] is None
        assert metrics["compactions"][0]["after_tokens"] is None
        assert metrics["compactions"][0]["before_tokens"] == 180000
    finally:
        sess.close()
