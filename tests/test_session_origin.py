"""M1 Step 2 — SessionOrigin persistence (additive, back-compatible).

SessionStore reads DB-first (the mesh DB is canonical), so origin must survive
the round-trip through BOTH the JSON file and the DB mirror. The conftest forces
shadow_write on and isolates the DB per test, so save()->get() exercises the
DB-first read path.
"""
import json

from src.core.interfaces import Session, SessionOrigin, SessionStatus
from src.services.session_store import SessionStore


def _new(store: SessionStore, **kw) -> Session:
    return store.create(backend="claude", repo_path=str(kw.pop("repo", "/tmp/x")), **kw)


def test_default_origin_round_trips_to_telegram_user(tmp_path):
    store = SessionStore()
    s = _new(store)
    assert s.origin == SessionOrigin("telegram", "user")

    loaded = store.get(s.session_id)
    assert loaded is not None
    assert loaded.origin == SessionOrigin("telegram", "user")


def test_web_origin_survives_save_load_on_db_mirror(tmp_path):
    store = SessionStore()
    s = _new(store)
    s.origin = SessionOrigin("web", "user")
    store.save(s)

    # DB-first read path (canonical) — this is what production list/get hit.
    loaded = store.get(s.session_id)
    assert loaded is not None
    assert loaded.origin == SessionOrigin("web", "user")

    # And it shows up via list_all (also DB-first).
    listed = {x.session_id: x for x in store.list_all()}
    assert listed[s.session_id].origin == SessionOrigin("web", "user")


def test_web_origin_survives_in_json_file(tmp_path):
    """Independently of the DB, the on-disk JSON carries origin."""
    store = SessionStore()
    s = _new(store)
    s.origin = SessionOrigin("web", "cron")
    store.save(s)

    from src.services.session_store import _SESSIONS_DIR
    raw = json.loads((_SESSIONS_DIR / f"{s.session_id}.json").read_text(encoding="utf-8"))
    assert raw["origin"] == {"channel": "web", "kind": "cron"}


def test_pre_m1_session_json_without_origin_still_loads():
    """A session dict with no `origin` key (pre-M1) defaults to telegram/user."""
    d = {
        "session_id": "legacy123456",
        "backend": "claude",
        "repo_path": "/tmp/legacy",
        "status": SessionStatus.IDLE.value,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        # no "origin" key on purpose
    }
    s = SessionStore._from_dict(d)
    assert s.origin == SessionOrigin("telegram", "user")
