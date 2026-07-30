"""[A52] M3.4 Job 1 adoption — the ``round_cap`` seam through ``db.open_case`` and
the dual-shape ``completion_criteria`` it produces.

Proves the packet's acceptance #1: ``open_case(round_cap=N)`` round-trips so
``case_round_cap(case)==N`` AND a plain-text ``completion_criteria`` still parses
into the human criterion list the A37 close-gate reconciles (no regression). Pure
DB — no paid CLI, no gateway.
"""

from src.control.db import (
    MeshDB,
    DEFAULT_CONTINUATION_ROUND_CAP,
    _compose_completion_criteria,
    _parse_completion_criteria,
    _unreconciled_criteria,
)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _stored_criteria(db: MeshDB, case_id: str):
    return (db.get_flow_run(case_id) or {}).get("completion_criteria")


# --------------------------------------------------------------------------- #
# db.open_case(round_cap=…) round-trip                                         #
# --------------------------------------------------------------------------- #

def test_open_case_round_cap_round_trips(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-sess", round_cap=2)
    assert db.case_round_cap(case_id) == 2


def test_open_case_round_cap_folds_human_criteria(tmp_path):
    """round_cap AND human criteria ride the same field; both survive."""
    db = _db(tmp_path)
    case_id = db.open_case(
        "obj", "mgr-sess",
        completion_criteria="tests green; diff reviewed; PR opened",
        round_cap=3,
    )
    # machine cap is readable
    assert db.case_round_cap(case_id) == 3
    # human criteria still visible to the A37 close-gate (NOT the raw JSON blob)
    parsed = _parse_completion_criteria(_stored_criteria(db, case_id))
    assert parsed == ["tests green; diff reviewed; PR opened"]


def test_open_case_no_round_cap_is_byte_identical(tmp_path):
    """Omitting round_cap stores the criteria verbatim (pre-A52 behaviour)."""
    db = _db(tmp_path)
    case_id = db.open_case(
        "obj", "mgr-sess", completion_criteria="plain text criterion",
    )
    assert _stored_criteria(db, case_id) == "plain text criterion"
    assert db.case_round_cap(case_id) == DEFAULT_CONTINUATION_ROUND_CAP
    assert _parse_completion_criteria(_stored_criteria(db, case_id)) == [
        "plain text criterion"
    ]


def test_open_case_round_cap_without_criteria(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-sess", round_cap=7)
    assert db.case_round_cap(case_id) == 7
    # no human criteria ⇒ nothing for the close-gate to reconcile
    assert _parse_completion_criteria(_stored_criteria(db, case_id)) == []


# --------------------------------------------------------------------------- #
# Pure helpers — compose + parse dual shapes                                   #
# --------------------------------------------------------------------------- #

def test_compose_none_cap_returns_raw_unchanged():
    assert _compose_completion_criteria("abc", None) == "abc"
    assert _compose_completion_criteria("abc", 0) == "abc"
    assert _compose_completion_criteria(None, None) is None


def test_compose_embeds_list_criteria_as_list():
    out = _compose_completion_criteria('["a", "b"]', 4)
    assert _parse_completion_criteria(out) == ["a", "b"]


def test_parse_object_shape_ignores_round_cap_only():
    assert _parse_completion_criteria('{"round_cap": 9}') == []


def test_parse_object_shape_extracts_string_criteria():
    assert _parse_completion_criteria(
        '{"round_cap": 9, "criteria": "ship it"}'
    ) == ["ship it"]


# --------------------------------------------------------------------------- #
# A37 close-gate no-regression over the folded object                         #
# --------------------------------------------------------------------------- #

def test_close_gate_reconciles_folded_criteria(tmp_path):
    """The dual-shape field does not make a Case perpetually unclosable: the
    human criterion can be reconciled met/waived exactly as before."""
    db = _db(tmp_path)
    case_id = db.open_case(
        "obj", "mgr-sess", completion_criteria="tests green", round_cap=2,
    )
    raw = _stored_criteria(db, case_id)
    # unmet before reconciliation
    assert _unreconciled_criteria(raw, []) == ["tests green"]
    # met after reconciliation ⇒ closable
    assert _unreconciled_criteria(
        raw, [{"criterion": "tests green", "status": "met"}]
    ) == []
