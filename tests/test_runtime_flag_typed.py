"""[A62] Typed runtime-flag registry (bool | int | float | str), proven on the SDK
governor knobs CLAUDE_SDK_MAX_TURNS / CLAUDE_SDK_MAX_BUDGET_USD.

Covers: every existing bool flag renders byte-identically; PUT validates against the
declared value_type (422 on mismatch); the governor knobs resolve registry → env →
no cap at the SDK driver's session-process creation, on a controller (local DB) AND
on a worker (A88 controller snapshot), falling back to env without a snapshot.
No SDK boot, no paid CLI.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import src.control.db as db_mod
from src.control import control_api, controller_state
from src.core.interfaces import Session, SessionStatus
from src.worker.controller_state_client import RemoteControllerState

TOKEN = "a62-test-token"
_KNOBS = ("CLAUDE_SDK_MAX_TURNS", "CLAUDE_SDK_MAX_BUDGET_USD")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import config as _config_pkg

    for name in _KNOBS:
        monkeypatch.delenv(name, raising=False)
    # The env/default layer of the governor is the A53 config parse.
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", None, raising=False)
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_budget_usd", None, raising=False)
    controller_state.uninstall()
    yield
    controller_state.uninstall()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(type("_Orch", (), {})()))


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _put(client, flag, value):
    return client.put(f"/api/flags/{flag}", headers=_auth(), json={"value": value, "set_by": "test"})


def _flag(client, flag):
    return next(f for f in client.get("/api/flags", headers=_auth()).json()["flags"] if f["flag_name"] == flag)


# --- byte-identity of the existing bool flags ---------------------------------


def _pre_a62_render(flag_name, db):
    """The pre-A62 ``render_runtime_flag`` body, verbatim (minus the new key)."""
    definition = db_mod.RUNTIME_FLAG_DEFINITIONS[flag_name]
    writable = db_mod.runtime_flag_registry_writable(flag_name)
    row = db_mod._runtime_flag_row(flag_name, db=db) if writable else None
    import os
    env_raw = os.environ.get(flag_name)
    if row is not None:
        source, raw_value = "registry", str(row.get("value") or "")
    elif env_raw is not None:
        source, raw_value = "env", env_raw
    else:
        source, raw_value = "default", definition["default"]
    truthy = db_mod._truthy_flag(raw_value)
    return {
        "flag_name": flag_name, "value": truthy, "raw_value": "1" if truthy else "0",
        "source": source, "effect_scope": definition["effect_scope"],
        "registry_writable": writable, "description": definition["description"],
        "registry": row, "env_value": env_raw,
    }


_BOOL_FLAGS = sorted(
    n for n, d in db_mod.RUNTIME_FLAG_DEFINITIONS.items() if d.get("value_type", "bool") == "bool"
)


def test_only_intended_entries_are_non_bool():
    typed = {n: d["value_type"] for n, d in db_mod.RUNTIME_FLAG_DEFINITIONS.items() if "value_type" in d}
    assert typed == {
        "TOOL_PROFILES_OVERLAY": "str",
        "CLAUDE_SDK_MAX_TURNS": "int",
        "CLAUDE_SDK_MAX_BUDGET_USD": "float",
    }
    assert set(typed.values()) <= set(db_mod.RUNTIME_FLAG_VALUE_TYPES)


@pytest.mark.parametrize("env", [None, "1", "0", "yes", "off", "garbage", ""])
def test_every_bool_flag_renders_byte_identically(monkeypatch, env):
    db = db_mod.get_db()
    for name in _BOOL_FLAGS:
        if env is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, env)
        new = db_mod.render_runtime_flag(name, db=db)
        assert new.pop("value_type") == "bool"
        assert new == _pre_a62_render(name, db)


def test_bool_registry_rows_stay_one_zero_and_lax_inputs_unchanged(client):
    db = db_mod.get_db()
    for value, stored in ((True, "1"), (False, "0"), (1, "1"), (0, "0"), ("true", "1"), ("off", "0")):
        r = _put(client, "CASE_CONTINUATION_ENABLED", value)
        assert r.status_code == 200, r.text
        assert db.get_runtime_flag("CASE_CONTINUATION_ENABLED")["value"] == stored
        assert r.json()["flag"]["raw_value"] == stored
        assert r.json()["flag"]["value"] is (stored == "1")
    for bad in (2, "maybe", None, 1.5):
        r = _put(client, "CASE_CONTINUATION_ENABLED", bad)
        assert r.status_code == 422, (bad, r.text)
    assert client.put("/api/flags/CASE_CONTINUATION_ENABLED", headers=_auth(), json={}).status_code == 422


# --- the governor knobs through /api/flags ------------------------------------


def test_governor_knobs_listed_with_type_value_and_source(client, monkeypatch):
    turns = _flag(client, "CLAUDE_SDK_MAX_TURNS")
    budget = _flag(client, "CLAUDE_SDK_MAX_BUDGET_USD")
    assert (turns["value_type"], turns["value"], turns["source"]) == ("int", None, "default")
    assert (budget["value_type"], budget["value"], budget["source"]) == ("float", None, "default")
    assert turns["effect_scope"] == budget["effect_scope"] == "session_boot"
    assert turns["registry_writable"] is budget["registry_writable"] is True

    monkeypatch.setenv("CLAUDE_SDK_MAX_TURNS", "25")
    monkeypatch.setenv("CLAUDE_SDK_MAX_BUDGET_USD", "2.5")
    turns = _flag(client, "CLAUDE_SDK_MAX_TURNS")
    assert (turns["value"], turns["raw_value"], turns["source"]) == (25, "25", "env")
    assert _flag(client, "CLAUDE_SDK_MAX_BUDGET_USD")["value"] == 2.5

    assert _put(client, "CLAUDE_SDK_MAX_TURNS", 40).status_code == 200
    turns = _flag(client, "CLAUDE_SDK_MAX_TURNS")
    assert (turns["value"], turns["raw_value"], turns["source"]) == (40, "40", "registry")


def test_put_reflected_in_health_null_wins_delete_falls_back(client, monkeypatch):
    import config as _config_pkg

    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", 25, raising=False)
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_budget_usd", 2.5, raising=False)
    assert client.get("/health").json()["governor"] == {"sdk_max_turns": 25, "sdk_max_budget_usd": 2.5}

    assert _put(client, "CLAUDE_SDK_MAX_TURNS", 40).status_code == 200
    assert _put(client, "CLAUDE_SDK_MAX_BUDGET_USD", 7).status_code == 200  # int accepted for float
    assert client.get("/health").json()["governor"] == {"sdk_max_turns": 40, "sdk_max_budget_usd": 7.0}

    # R2: null = explicit "no cap" that overrides env.
    r = _put(client, "CLAUDE_SDK_MAX_TURNS", None)
    assert r.status_code == 200 and r.json()["flag"]["value"] is None
    assert db_mod.get_db().get_runtime_flag("CLAUDE_SDK_MAX_TURNS")["value"] == ""
    assert client.get("/health").json()["governor"]["sdk_max_turns"] is None
    # A rejected non-positive PUT leaves the explicit null in place.
    assert _put(client, "CLAUDE_SDK_MAX_TURNS", 0).status_code == 422
    assert client.get("/health").json()["governor"]["sdk_max_turns"] is None

    d = client.delete("/api/flags/CLAUDE_SDK_MAX_TURNS", headers=_auth())
    assert d.status_code == 200 and d.json()["flag"]["source"] != "registry"
    assert client.get("/health").json()["governor"]["sdk_max_turns"] == 25


@pytest.mark.parametrize(
    "flag,value",
    [
        ("CLAUDE_SDK_MAX_TURNS", "40"),
        ("CLAUDE_SDK_MAX_TURNS", True),
        ("CLAUDE_SDK_MAX_TURNS", 4.5),
        ("CLAUDE_SDK_MAX_BUDGET_USD", "2.5"),
        ("CLAUDE_SDK_MAX_BUDGET_USD", False),
        ("TOOL_PROFILES_OVERLAY", 3),
        ("TOOL_PROFILES_OVERLAY", None),
    ],
)
def test_wrong_typed_put_is_422_and_writes_nothing(client, flag, value):
    r = _put(client, flag, value)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "invalid_flag_value"
    assert detail["value_type"] == db_mod.runtime_flag_value_type(flag)
    assert db_mod.get_db().get_runtime_flag(flag) is None


@pytest.mark.parametrize(
    "flag,value",
    [("CLAUDE_SDK_MAX_TURNS", 0), ("CLAUDE_SDK_MAX_TURNS", -1),
     ("CLAUDE_SDK_MAX_BUDGET_USD", 0), ("CLAUDE_SDK_MAX_BUDGET_USD", 0.0),
     ("CLAUDE_SDK_MAX_BUDGET_USD", -2.5)],
)
def test_governor_non_positive_put_is_422_null_is_the_only_no_cap(client, flag, value):
    # A 0 must never be stored as a silent "unlimited" on a cost ceiling.
    r = _put(client, flag, value)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason"] == "invalid_flag_value"
    assert "use null for no cap" in r.json()["detail"]["message"]
    assert db_mod.get_db().get_runtime_flag(flag) is None


@pytest.mark.parametrize("raw", ["0", "-1", "inf", "nan", "abc"])
def test_non_positive_registry_row_is_ignored_not_unlimited(monkeypatch, raw):
    # Only a direct DB write can bypass the PUT check; the resolver must still
    # fall back to env rather than read it as "no cap".
    import config as _config_pkg

    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_budget_usd", 5.0, raising=False)
    with db_mod.get_db()._write() as conn:
        conn.execute(
            "INSERT INTO runtime_flags(flag_name, value, source, set_at, set_by) VALUES (?, ?, ?, ?, ?)",
            ("CLAUDE_SDK_MAX_BUDGET_USD", raw, "test", "2026-10-22T00:00:00Z", "test"),
        )
    assert db_mod.sdk_governor_ceilings()["sdk_max_budget_usd"] == 5.0


def test_str_flag_round_trips_raw(client):
    overlay = '{"profiles": {"worker_v1": ["Read", "mcp__jobs__list_jobs"]}}'
    r = _put(client, "TOOL_PROFILES_OVERLAY", overlay)
    assert r.status_code == 200
    assert r.json()["flag"]["value"] == overlay == r.json()["flag"]["raw_value"]
    assert db_mod.runtime_flag_value("TOOL_PROFILES_OVERLAY") == overlay


# --- worker-side resolution (A88 controller snapshot) ---------------------------


class _FakeHTTP:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, path, params=None, timeout=10):
        assert path == "/control/runtime-flags"
        return self.snapshot

    def post(self, path, body=None, timeout=10):
        raise AssertionError("unexpected post")


def _install_worker_snapshot(**rows):
    snap = {
        "revision": "r1",
        "flags": [{"flag_name": k, "value": v, "set_at": "2026-10-22T00:00:00Z"} for k, v in rows.items()],
    }
    remote = RemoteControllerState(_FakeHTTP(snap))
    assert remote.refresh() is True
    controller_state.install(remote)


def test_worker_resolves_governor_from_controller_snapshot():
    # The controller serves registry rows as strings over /control/runtime-flags.
    db = db_mod.get_db()
    db.set_runtime_flag("CLAUDE_SDK_MAX_TURNS", 40)
    db.set_runtime_flag("CLAUDE_SDK_MAX_BUDGET_USD", 2.5)
    import src.control.task_server as ts
    served = {f.flag_name: f.value for f in ts.control_runtime_flags().flags}
    assert served == {"CLAUDE_SDK_MAX_TURNS": "40", "CLAUDE_SDK_MAX_BUDGET_USD": "2.5"}

    _install_worker_snapshot(**served)
    assert db_mod.get_db() is None  # a worker never reads a local DB
    assert db_mod.sdk_governor_ceilings() == {"sdk_max_turns": 40, "sdk_max_budget_usd": 2.5}


def test_worker_without_snapshot_or_with_bad_row_falls_back_to_env():
    import config as _config_pkg

    _config_pkg.config.claude.sdk_max_turns = 12
    _config_pkg.config.claude.sdk_max_budget_usd = 1.5
    # Never fetched (controller unreachable / predates the knobs): env.
    controller_state.install(RemoteControllerState(_FakeHTTP({})))
    assert db_mod.sdk_governor_ceilings() == {"sdk_max_turns": 12, "sdk_max_budget_usd": 1.5}
    # An unparseable row is ignored for that knob only; the other one still applies.
    _install_worker_snapshot(CLAUDE_SDK_MAX_TURNS="forty", CLAUDE_SDK_MAX_BUDGET_USD="")
    assert db_mod.sdk_governor_ceilings() == {"sdk_max_turns": 12, "sdk_max_budget_usd": None}


def test_worker_snapshot_admits_a_long_str_row_without_dropping_governor():
    overlay = '{"profiles": {"worker_v1": ["Read", "Grep", "mcp__jobs__list_jobs"]}}'
    assert len(overlay) > 32
    _install_worker_snapshot(TOOL_PROFILES_OVERLAY=overlay, CLAUDE_SDK_MAX_TURNS="40")
    assert db_mod.sdk_governor_ceilings()["sdk_max_turns"] == 40
    assert db_mod.runtime_flag_value("TOOL_PROFILES_OVERLAY") == overlay


# --- the SDK driver consumes the resolution at session-process creation --------


class _CapturingSDKSession:
    def __init__(self, key, cwd, model, proc_env, **kwargs):
        self.key = key
        self.kwargs = kwargs
        self.effort = kwargs.get("effort")
        self.backend_session_id = ""
        self._closed = False
        self._on_proactive = None

    def start(self):
        pass


def _boot_driver_session(monkeypatch):
    import src.backends.claude_driver as cd

    monkeypatch.setattr(cd, "_SDKSession", _CapturingSDKSession)
    driver = cd.ClaudeSDKClientDriver()
    monkeypatch.setattr(driver, "_role_boot", lambda session: (None, None))
    session = Session(
        session_id=uuid.uuid4().hex, backend="claude", repo_path="/repo",
        status=SessionStatus.IDLE, created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    return driver._get_or_create(session, model=None, effort=None, proc_env={})


def test_driver_passes_registry_ceiling_on_worker(monkeypatch):
    import config as _config_pkg

    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", 12, raising=False)
    _install_worker_snapshot(CLAUDE_SDK_MAX_TURNS="40")
    sess = _boot_driver_session(monkeypatch)
    assert sess.kwargs["max_turns"] == 40
    assert sess.kwargs["max_budget_usd"] is None


def test_driver_without_registry_row_is_byte_identical_config_read(monkeypatch):
    import config as _config_pkg

    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", 12, raising=False)
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_budget_usd", 3.0, raising=False)
    sess = _boot_driver_session(monkeypatch)
    assert (sess.kwargs["max_turns"], sess.kwargs["max_budget_usd"]) == (12, 3.0)
