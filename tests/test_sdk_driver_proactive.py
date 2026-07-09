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

from src.backends.claude_driver import _SDKSession

sdk = pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402


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
    the CLI would emit for it; `receive_messages` is the one continuous stream."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.replies: dict = {}
        self.queries_sent: list = []

    async def query(self, message: str, session_id: str = "default") -> None:
        self.queries_sent.append(message)
        for m in self.replies.get(message, []):
            self.q.put_nowait(m)

    async def receive_messages(self):
        while True:
            yield await self.q.get()


def _start_fake_session(fake: _FakeClient) -> _SDKSession:
    """Boot a _SDKSession backed by `fake` on its own loop thread, skipping the
    real SDK connect but running the real reader + submit machinery."""
    sess = _SDKSession("key", "/tmp", None, {})
    sess._client = fake

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
        r1 = sess.send("continue the work")
        assert r1.output == "Running backgrounded (~6 min)."
        assert r1.proactive is False

        # The background job finishes: the session autonomously produces a
        # follow-up turn on the stream with NO prompt from us.
        _emit_autonomous(sess, fake, _assistant("The job is done — it passed."),
                         _result("The job is done — it passed."))

        # Turn 2: a fresh prompt. Under the old receive_response() code this
        # returned the buffered background result. It must now return the
        # answer to THIS prompt.
        r2 = sess.send("what is the result?")
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
        r = sess.send("do the thing")
        assert r.is_error is True
        assert r.output == "I made real progress before overflowing."
        assert r.salvaged_output == "I made real progress before overflowing."
        assert r.error_text
    finally:
        sess.close()
