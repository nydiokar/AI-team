import logging
import sys

from src.core import observability


def _exception_record(message: str, exception: Exception) -> logging.LogRecord:
    try:
        raise exception
    except Exception:
        return logging.LogRecord(
            name="telegram.ext.Updater",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=sys.exc_info(),
        )


def test_bracketed_formatter_includes_exception_type_message_and_traceback(monkeypatch):
    monkeypatch.setattr(observability, "_NODE_ID", "gateway-1")
    record = _exception_record(
        "Exception happened while polling for updates.",
        RuntimeError("polling conflict"),
    )

    output = observability._BracketedFormatter().format(record)

    assert "[node=gateway-1]" in output
    assert "telegram.ext.Updater: Exception happened while polling for updates." in output
    assert "Traceback (most recent call last):" in output
    assert "RuntimeError: polling conflict" in output


def test_turn_context_adds_accounting_fields_and_task_alias(monkeypatch):
    monkeypatch.setattr(observability, "_NODE_ID", "gateway-1")
    with observability.log_context(
        turn_id="turn-1",
        invocation_id="inv-1",
        backend="codex",
        session_id="session-1",
    ):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "hello", (), None
        )
        output = observability._BracketedFormatter().format(record)
        context = observability._current_context()

    assert context["task_id"] == "turn-1"
    assert "task=turn-1" in output
    assert "invocation=inv-1" in output
    assert "backend=codex" in output


def test_new_task_id_refreshes_stale_turn_id_no_leak():
    """A new turn that sets only task_id must not inherit a prior turn's turn_id.

    Regression for the "one turn behind" bug: the process-global correlation
    context leaked a stale turn_id across turns/sessions, so results were emitted
    against the previous message.
    """
    tok_a = observability.set_log_context(task_id="task_AAA", session_id="sess-1")
    try:
        ctx_a = observability._current_context()
        assert ctx_a["task_id"] == "task_AAA"
        assert ctx_a["turn_id"] == "task_AAA"

        # New turn, different session, only task_id supplied.
        tok_b = observability.set_log_context(task_id="task_BBB", session_id="sess-2")
        try:
            ctx_b = observability._current_context()
            assert ctx_b["task_id"] == "task_BBB"
            # The load-bearing assertion: turn_id follows the new task_id.
            assert ctx_b["turn_id"] == "task_BBB", ctx_b

            # An explicit turn_id (without task_id) still aliases the other way.
            tok_c = observability.set_log_context(turn_id="turn_CCC")
            try:
                ctx_c = observability._current_context()
                assert ctx_c["turn_id"] == "turn_CCC"
                assert ctx_c["task_id"] == "turn_CCC"
            finally:
                observability.reset_log_context(tok_c)
        finally:
            observability.reset_log_context(tok_b)
    finally:
        observability.reset_log_context(tok_a)


def test_bracketed_formatter_redacts_secrets_from_exception_text(monkeypatch):
    monkeypatch.setattr(observability, "_NODE_ID", "gateway-1")
    secret = "123456789:ABC_private_token"
    record = _exception_record(
        "Telegram polling failed",
        RuntimeError(f"https://api.telegram.org/bot{secret}/getUpdates"),
    )

    output = observability._BracketedFormatter().format(record)

    assert secret not in output
    assert "/bot<REDACTED>/getUpdates" in output
