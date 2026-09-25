"""A82 Stage 1 — SDK backend ownership RED acceptance tests (SDK01-04 + Stage 0 traces).

These drive the REAL `_SDKSession` reader/submit/dispatch machinery
(`src/backends/claude_driver.py`) with a fake `claude_agent_sdk` client that
emits real SDK message objects (`AssistantMessage`, `ResultMessage`, and the
background-task lifecycle messages `TaskNotificationMessage`/`TaskUpdatedMessage`
that ship in the installed SDK 0.2.110).

They encode A82 §15 Manager decision 1 and design §6:

  * A managed (protocol-1) turn whose session lock is occupied must return a
    typed ownership conflict WITHOUT calling `cancel_inflight()`. Current
    `_SDKSession.send` (claude_driver.py:~1109) unconditionally interrupts on
    lock conflict — so any test asserting "no interrupt on a managed conflict"
    fails on the current code.
  * A native background task finishing MUST NOT be handed back as the reply to
    the wrong explicit request (no autonomous result misattribution). The driver
    routes an unsolicited terminal `ResultMessage` to the proactive sink when
    `_pending` is empty; there is currently NO lifecycle-aware ownership so a
    held native task does not retain the session slot.
  * Quiescence: A "task-finished notification" alone, and an empty `_pending`
    deque alone, are each individually INSUFFICIENT to prove the session is
    idle (Stage 0 §3). A real quiescence oracle requires ALL of: `_pending`
    empty AND no non-terminal tracked background task AND the terminal
    `ResultMessage` of the last query observed. Current code exposes no such
    oracle — the tests assert its target contract and fail (assertion or
    AttributeError-red).

All assertions target contracts the current driver LACKS; they are meaningful
red against `main`.
"""
import asyncio
import threading
import time

import pytest

from src.backends.claude_driver import _SDKSession

sdk = pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    TaskNotificationMessage,
    TaskUpdatedMessage,
    TERMINAL_TASK_STATUSES,
)


# --------------------------------------------------------------------------- #
# Real SDK message factories (mirror tests/test_sdk_driver_proactive.py)
# --------------------------------------------------------------------------- #
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


def _task_notification(task_id: str, status: str = "completed", sid: str = "sid-1") -> TaskNotificationMessage:
    """A background task reached a terminal status (completed/failed/stopped)."""
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file=f"/tmp/{task_id}.out",
        summary=f"task {task_id} {status}",
        uuid="tn",
        session_id=sid,
        tool_use_id=None,
        usage=None,
    )


def _task_updated(task_id: str, status: str, sid: str = "sid-1") -> TaskUpdatedMessage:
    """A background task changed status (pending/running/paused/... /terminal)."""
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch={"status": status},
        status=status,
        session_id=sid,
        uuid="tu",
    )


# --------------------------------------------------------------------------- #
# Fake SDK client + session boot (mirror the existing proactive harness, but
# also record whether interrupt() was ever called so we can assert NO interrupt)
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.replies: dict = {}
        self.queries_sent: list = []
        self.interrupts: int = 0

    async def query(self, message: str, session_id: str = "default") -> None:
        self.queries_sent.append(message)
        for m in self.replies.get(message, []):
            self.q.put_nowait(m)

    async def receive_messages(self):
        while True:
            yield await self.q.get()

    async def interrupt(self) -> None:
        self.interrupts += 1


def _start_fake_session(fake: _FakeClient) -> _SDKSession:
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
    sess._loop.call_soon_threadsafe(lambda: [fake.q.put_nowait(m) for m in msgs])


class _ManagedSendMissing(Exception):
    """Sentinel: the protocol-1 managed no-interrupt send does not exist yet."""


def _managed_send(sess: _SDKSession, message: str):
    """Invoke the managed (protocol-1) no-interrupt send path.

    A82 decision 1 requires a DISTINCT managed send that returns a typed
    ownership conflict and NEVER calls cancel_inflight. That method does not
    exist yet; resolve it dynamically and raise a distinct sentinel (NOT a
    conflict-shaped error) when the contract is unimplemented so the caller
    fails red rather than mistaking absence for a valid conflict.
    """
    for name in ("send_managed", "managed_send", "send_protocol1", "submit_managed_turn"):
        fn = getattr(sess, name, None)
        if callable(fn):
            return fn(message)
    raise _ManagedSendMissing(
        "no managed no-interrupt send method on _SDKSession "
        "(A82 decision 1: protocol-1 send must return a typed ownership "
        "conflict without cancel_inflight; contract not implemented)"
    )


def _quiescence_oracle(sess: _SDKSession):
    for name in ("is_quiescent", "quiescent", "is_idle", "session_quiescent"):
        fn = getattr(sess, name, None)
        if callable(fn):
            return fn
    return None


# --------------------------------------------------------------------------- #
# SDK01 — managed turn: NO implicit interrupt on lock conflict
# --------------------------------------------------------------------------- #
def test_SDK01_managed_send_lock_conflict_does_not_interrupt():
    """A82 §15 dec.1 / design §6: an occupied lock on the MANAGED path returns a
    typed ownership conflict and MUST NOT call cancel_inflight().

    RED: no managed send path exists; the legacy send() interrupts on conflict
    (claude_driver.py:~1133). Asserts the target contract → red.
    """
    fake = _FakeClient()
    started = threading.Event()

    def _first():
        started.set()
        try:
            sess.send("hold the lock")
        except Exception:
            pass

    sess = _start_fake_session(fake)
    try:
        holder = threading.Thread(target=_first, daemon=True)
        holder.start()
        assert started.wait(timeout=2)
        time.sleep(0.2)  # let the first turn acquire the lock

        conflict = None
        try:
            _managed_send(sess, "second managed turn")
        except _ManagedSendMissing:
            # RED: the managed no-interrupt path is not implemented yet.
            pytest.fail(
                "managed protocol-1 send path is not implemented; A82 decision 1 "
                "requires a typed ownership conflict without cancel_inflight"
            )
        except Exception as e:  # noqa: BLE001
            conflict = e

        assert conflict is not None, "managed send accepted a turn on a busy session"
        assert "conflict" in type(conflict).__name__.lower() or "ownership" in str(conflict).lower()
        assert fake.interrupts == 0, (
            "managed send called cancel_inflight/interrupt on lock conflict "
            "(forbidden by A82 decision 1)"
        )
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# SDK02 — no autonomous result misattribution
# --------------------------------------------------------------------------- #
def test_SDK02_background_result_not_served_as_explicit_reply():
    """An unsolicited background terminal result must NOT be returned as the
    reply to a later explicit prompt (design §6: background results cannot
    fulfil the wrong explicit request).

    RED by assertion: with a prompt pending, an unsolicited background result
    arriving first is popped off `_pending` and mis-served as that prompt's
    reply on current code.
    """
    fake = _FakeClient()
    fake.replies["explicit question"] = []  # its real reply arrives later

    proactive: list = []
    sess = _start_fake_session(fake)
    sess._on_proactive = lambda key, outcome: proactive.append((key, outcome))

    def _ask():
        try:
            _ask.result = sess.send("explicit question")
        except Exception as e:  # noqa: BLE001
            _ask.result = e

    _ask.result = None
    try:
        asker = threading.Thread(target=_ask, daemon=True)
        asker.start()
        time.sleep(0.2)  # let the explicit prompt register as pending
        _emit_autonomous(sess, fake, _result("BACKGROUND JOB OUTPUT — not your answer"))
        time.sleep(0.3)

        assert getattr(_ask.result, "output", None) != "BACKGROUND JOB OUTPUT — not your answer", (
            "background result was misattributed as the explicit prompt's reply"
        )
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# SDK03 — held native work retains ownership
# --------------------------------------------------------------------------- #
def test_SDK03_held_native_background_task_retains_ownership():
    """While a native background task is non-terminal, the session must report
    itself as NOT quiescent / still owned (design §6 driver reservation).

    RED: no ownership/quiescence oracle exists on _SDKSession.
    """
    fake = _FakeClient()
    fake.replies["kick off background"] = [
        _assistant("Launching a background task."),
        _result("Launched (running in background)."),
    ]
    sess = _start_fake_session(fake)
    try:
        r1 = sess.send("kick off background")
        assert r1.output == "Launched (running in background)."
        _emit_autonomous(sess, fake, _task_updated("bg-1", "running"))
        time.sleep(0.2)

        oracle = _quiescence_oracle(sess)
        assert oracle is not None, "no quiescence oracle on _SDKSession (contract missing)"
        assert oracle() is False, (
            "session reported quiescent while a background task is still running "
            "— held native work must retain ownership"
        )
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# SDK04 — quiescence oracle traces (Stage 0 §3): each signal alone insufficient
# --------------------------------------------------------------------------- #
def test_SDK04a_empty_pending_alone_is_not_quiescence():
    """Empty `_pending` deque alone does NOT prove quiescence (Stage 0 §3).

    After a turn completes `_pending` is empty, but a still-running background
    task means the session is NOT idle. RED: oracle missing.
    """
    fake = _FakeClient()
    fake.replies["start"] = [_assistant("ok"), _result("ok")]
    sess = _start_fake_session(fake)
    try:
        sess.send("start")
        _emit_autonomous(sess, fake, _task_updated("bg-9", "running"))
        time.sleep(0.2)
        assert len(sess._pending) == 0  # empty _pending
        oracle = _quiescence_oracle(sess)
        assert oracle is not None, "no quiescence oracle on _SDKSession (contract missing)"
        assert oracle() is False, (
            "empty _pending was treated as quiescence while a background task runs"
        )
    finally:
        sess.close()


def test_SDK04b_task_finished_notification_alone_is_not_quiescence():
    """A single task-finished notification does NOT prove the ensuing model
    continuation ended (Stage 0 §3). Quiescence also needs the terminal
    ResultMessage of the last query observed. RED: oracle missing.
    """
    fake = _FakeClient()
    fake.replies["go"] = [_assistant("dispatching"), _result("dispatched")]
    sess = _start_fake_session(fake)
    try:
        sess.send("go")
        notif = _task_notification("bg-terminal", status="completed")
        assert notif.status in TERMINAL_TASK_STATUSES
        # Task finished, THEN the agent autonomously continues (assistant text
        # only, NO terminal result yet).
        _emit_autonomous(sess, fake, notif, _assistant("Continuing after the task…"))
        time.sleep(0.2)
        oracle = _quiescence_oracle(sess)
        assert oracle is not None, "no quiescence oracle on _SDKSession (contract missing)"
        assert oracle() is False, (
            "a task-finished notification alone was treated as quiescence while "
            "a model continuation is still in flight"
        )
    finally:
        sess.close()


def test_SDK04c_quiescence_requires_all_three_signals():
    """Positive oracle contract: quiescent iff `_pending` empty AND no
    non-terminal tracked background task AND the last query's terminal
    ResultMessage observed (Stage 0 §3 conjunction). RED: oracle missing.
    """
    fake = _FakeClient()
    fake.replies["work"] = [_assistant("done"), _result("done")]
    sess = _start_fake_session(fake)
    try:
        sess.send("work")
        _emit_autonomous(
            sess,
            fake,
            _task_updated("bg-x", "running"),
            _task_notification("bg-x", status="completed"),
        )
        time.sleep(0.3)
        oracle = _quiescence_oracle(sess)
        assert oracle is not None, "no quiescence oracle on _SDKSession (contract missing)"
        assert oracle() is True, (
            "oracle failed to recognise genuine quiescence when all three "
            "signals hold"
        )
    finally:
        sess.close()
