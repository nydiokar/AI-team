"""One clock everywhere — regression guard for DROP_TIMEZONE_NATIVE_TIME.

Before the fix, `session_store` wrote naive-LOCAL timestamps while `db.py` wrote
UTC-aware ones, so a single session's created_at/updated_at were on two clocks
(created LOOKED after updated) and any "time ago" that subtracted a naive `now`
from a tz-aware stored value raised a naive-vs-aware TypeError.

These lock the invariants:
  * `now_iso()` is tz-AWARE and monotonic (created_at <= updated_at holds).
  * `parse_iso()` tolerates legacy naive rows (reads them as local, returns aware).
  * the Telegram "time ago"/render helpers no longer raise on a UTC-aware value.
"""
import time
from datetime import datetime, timezone

from src.core.timeutil import now_iso, parse_iso


def test_now_iso_is_tz_aware():
    dt = parse_iso(now_iso())
    assert dt.tzinfo is not None
    # It is UTC (the storage convention), regardless of the host's local zone.
    assert dt.utcoffset() == timezone.utc.utcoffset(None)


def test_now_iso_is_monotonic_and_comparable():
    a = now_iso()
    time.sleep(0.005)
    b = now_iso()
    # Same clock ⇒ directly comparable; created (a) never after updated (b).
    assert parse_iso(a) <= parse_iso(b)
    # And as raw strings too (UTC-aware ISO sorts chronologically).
    assert a <= b


def test_parse_iso_naive_is_read_as_local_and_made_aware():
    """A legacy naive row (what old session_store wrote) parses to an AWARE dt so
    it can be subtracted from a UTC-aware now without a TypeError."""
    naive = "2026-07-13T13:15:42"
    dt = parse_iso(naive)
    assert dt.tzinfo is not None
    # Subtraction against an aware now must not raise.
    _ = (datetime.now(timezone.utc) - dt).total_seconds()


def test_parse_iso_aware_is_preserved():
    aware = "2026-07-13T11:26:34+00:00"
    dt = parse_iso(aware)
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 7, 13, 11, 26, 34, tzinfo=timezone.utc)


def test_telegram_age_helpers_survive_utc_aware_timestamps():
    """Regression: `_relative_age` / `_heartbeat_age` previously did
    `datetime.now()/utcnow() - <aware dt>` → TypeError, swallowed to the raw value.
    With one clock they compute a real age string."""
    from src.telegram.interface import TelegramInterface

    ts = now_iso()  # tz-aware, ~now
    rel = TelegramInterface._relative_age(ts)
    hb = TelegramInterface._heartbeat_age(ts)
    # A real age, not the raw ISO fallback.
    assert rel != ts and "ago" in rel or rel == "just now"
    assert hb != ts and "ago" in hb


def test_session_store_writes_one_clock(tmp_path, monkeypatch):
    """SessionStore.create must stamp created_at/updated_at on ONE (aware) clock so
    created_at <= updated_at always holds."""
    import src.services.session_store as ss

    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(ss, "_BINDINGS_FILE", tmp_path / "bindings.json")
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    # Avoid the DB shadow write in this unit test.
    monkeypatch.setattr(ss.SessionStore, "_shadow_write", lambda self, s: None)

    store = ss.SessionStore()
    session = store.create(backend="claude", repo_path=str(tmp_path))
    c, u = parse_iso(session.created_at), parse_iso(session.updated_at)
    assert c.tzinfo is not None and u.tzinfo is not None
    assert c <= u
