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
