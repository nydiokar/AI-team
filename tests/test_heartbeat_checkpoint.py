"""Supervision-checkpoint heartbeat behaviour (feat/heartbeat-checkpoint).

Covers the three pieces the checkpoint work adds on top of the keep-warm beat:
  1. beat budget derived from the backend turn-timeout window;
  2. prompt selection — early awareness beat + late decision beat;
  3. deliberate renewal: ONLY a CONTINUE at the decision beat extends the
     budget (no automatic renewal), and CONCLUDE stops it.
"""

from datetime import datetime, timezone

from src.control import db as db_mod
from src.control.db import MeshDB, cache_heartbeat_max_beats_default
from src.orchestrator import (
    CACHE_HEARTBEAT_AWARENESS_PROMPT,
    CACHE_HEARTBEAT_DECISION_PROMPT,
    CACHE_HEARTBEAT_PROMPT,
    _select_cache_heartbeat_prompt,
)


# --------------------------------------------------------------------------- #
# 1. Beat budget covers one backend turn-timeout window
# --------------------------------------------------------------------------- #

def test_budget_default_derives_from_turn_timeout(monkeypatch) -> None:
    monkeypatch.delenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", raising=False)
    monkeypatch.delenv("SDK_TURN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("CACHE_HEARTBEAT_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("CACHE_HEARTBEAT_TTL_SEC", raising=False)
    # ceil(36000 / 2700) == 14 — blankets the 10h window up to the guaranteed wake.
    assert cache_heartbeat_max_beats_default() == 14


def test_budget_explicit_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", "9")
    assert cache_heartbeat_max_beats_default() == 9


def test_budget_floored_at_six_for_tiny_timeout(monkeypatch) -> None:
    monkeypatch.delenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", raising=False)
    monkeypatch.setenv("SDK_TURN_TIMEOUT_SEC", "3600")  # ceil(3600/2700)=2 -> floored to 6
    assert cache_heartbeat_max_beats_default() == 6


def test_checkpoint_flag_defaults_on() -> None:
    assert db_mod.RUNTIME_FLAG_DEFINITIONS["CACHE_HEARTBEAT_CHECKPOINT_ENABLED"]["default"] == "1"


# --------------------------------------------------------------------------- #
# 2. Prompt selection
# --------------------------------------------------------------------------- #

def test_disabled_always_keep_warm(monkeypatch) -> None:
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: False)
    for beat in (1, 6, 13, 14):
        assert _select_cache_heartbeat_prompt(beat, 14) is CACHE_HEARTBEAT_PROMPT


def test_enabled_selects_awareness_decision_and_keep_warm(monkeypatch) -> None:
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: True)
    monkeypatch.setattr(db_mod, "cache_heartbeat_awareness_beat", lambda: 6)
    assert _select_cache_heartbeat_prompt(6, 14) is CACHE_HEARTBEAT_AWARENESS_PROMPT
    assert _select_cache_heartbeat_prompt(13, 14) is CACHE_HEARTBEAT_DECISION_PROMPT  # max-1
    for beat in (1, 5, 7, 12, 14):
        assert _select_cache_heartbeat_prompt(beat, 14) is CACHE_HEARTBEAT_PROMPT


def test_small_budget_drops_awareness_but_keeps_decision(monkeypatch) -> None:
    # max_beats=4 -> decision beat 3; awareness (6) is past the decision beat, so
    # it never fires — only the decision checkpoint remains.
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: True)
    monkeypatch.setattr(db_mod, "cache_heartbeat_awareness_beat", lambda: 6)
    assert _select_cache_heartbeat_prompt(3, 4) is CACHE_HEARTBEAT_DECISION_PROMPT
    for beat in (1, 2, 4, 6):
        assert _select_cache_heartbeat_prompt(beat, 4) is CACHE_HEARTBEAT_PROMPT


def test_decision_prompt_mentions_no_infrastructure() -> None:
    # Point 3: the decision checkpoint must be purely operational — the Manager
    # never reasons about caches/heartbeats/timeouts.
    low = CACHE_HEARTBEAT_DECISION_PROMPT.lower()
    for leak in ("cache", "heartbeat", "timeout", "time out", "warm"):
        assert leak not in low, f"decision prompt leaked infra word: {leak!r}"


# --------------------------------------------------------------------------- #
# 3. Deliberate renewal (no automatic renewal)
# --------------------------------------------------------------------------- #

def _armed_hb(db: MeshDB, beat_count: int):
    """Arm an active controller and force its beat_count + a past expiry."""
    from datetime import timedelta

    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="op",
    )
    assert hb is not None
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with db._write() as conn:
        conn.execute(
            "UPDATE session_cache_heartbeats SET beat_count = ?, expires_at = ? WHERE id = ?",
            (beat_count, past, hb["id"]),
        )
        conn.execute(
            "UPDATE session_cache_heartbeat_owners SET expires_at = ? WHERE heartbeat_id = ?",
            (past, hb["id"]),
        )
    return str(hb["id"])


def _record(db: MeshDB, hb_id: str, output: str):
    db.record_cache_heartbeat_result(
        hb_id, "task_beat", success=True, output=output,
        cache_read_tokens=1000, cache_creation_tokens=0,
    )
    return db.get_cache_heartbeat(hb_id)


def test_continue_at_decision_beat_renews_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", "4")  # decision beat = 3
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: True)
    db = MeshDB(str(tmp_path / "mesh.db"))
    hb_id = _armed_hb(db, beat_count=2)  # this beat -> 3 == decision beat

    row = _record(db, hb_id, "Worker still building. CONTINUE_SUPERVISION")

    assert row["status"] in ("active", "observe_only")
    assert int(row["beat_count"]) == 0  # budget reset
    assert row["circuit_reason"] in (None, "")  # cleared
    # expiry pushed back into the future (was 10s in the past)
    exp = datetime.fromisoformat(str(row["expires_at"]))
    assert exp > datetime.now(timezone.utc)
    owners = [o for o in row["owners"] if o["status"] == "active"]
    assert owners and datetime.fromisoformat(str(owners[0]["expires_at"])) > datetime.now(timezone.utc)


def test_conclude_stops_the_controller(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", "4")
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: True)
    db = MeshDB(str(tmp_path / "mesh.db"))
    hb_id = _armed_hb(db, beat_count=2)

    row = _record(db, hb_id, "Task is finished, closing. CONCLUDE_SUPERVISION")

    assert row["status"] == "stopped"
    assert row["circuit_reason"] == "agent_concluded"


def test_continue_below_decision_beat_does_not_renew(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", "4")  # decision beat = 3
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: True)
    db = MeshDB(str(tmp_path / "mesh.db"))
    hb_id = _armed_hb(db, beat_count=0)  # this beat -> 1, well below decision beat 3

    row = _record(db, hb_id, "CONTINUE_SUPERVISION")

    assert row["status"] in ("active", "observe_only")
    assert int(row["beat_count"]) == 1  # normal increment, NOT reset


def test_continue_does_not_renew_when_checkpoints_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", "4")
    monkeypatch.setattr(db_mod, "cache_heartbeat_checkpoint_enabled", lambda: False)
    db = MeshDB(str(tmp_path / "mesh.db"))
    hb_id = _armed_hb(db, beat_count=2)  # -> 3; would be decision beat if enabled

    row = _record(db, hb_id, "CONTINUE_SUPERVISION")

    # No renewal path when disabled: increments normally, stays active (3 < 4).
    assert int(row["beat_count"]) == 3
    assert row["status"] in ("active", "observe_only")
