"""
A25 — Work Control Substrate schema tests: flow_links + flow_events.

Proves the additive migration-23 substrate:
  * fresh + re-opened DBs both carry flow_links, flow_events, and the optional
    convenience flow_run_id columns on mesh_tasks/approvals;
  * the migration is idempotent (re-open is a no-op, schema_version stays 24);
  * create_flow_link round-trips and is IDEMPOTENT on the unique key;
  * list_flow_links supports forward (by case) and reverse (by entity) lookups;
  * flow_events are append-only and preserve insertion order;
  * legacy writers (create_approval with no flow_run_id) still work → NULL column.

RECORD/relationship layer only — nothing here is read to drive execution.
"""

import pytest

from src.control.db import (
    MeshDB,
    FLOW_LINK_ENTITY_TYPES,
    FLOW_LINK_ROLES,
    FLOW_EVENT_TYPES,
    FLOW_EVENT_ACTORS,
)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _columns(db, table):
    return {r["name"] for r in db._conn().execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(db):
    return db._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0]


# ---------------------------------------------------------------------------
# Schema presence + idempotence
# ---------------------------------------------------------------------------

def test_fresh_db_has_substrate_schema(tmp_path):
    db = _db(tmp_path)
    assert "id" in _columns(db, "flow_links")
    assert {"flow_run_id", "entity_type", "entity_id", "role"} <= _columns(db, "flow_links")
    assert {"flow_run_id", "event_type", "actor", "payload_json"} <= _columns(db, "flow_events")
    # Optional convenience columns on hot tables.
    assert "flow_run_id" in _columns(db, "mesh_tasks")
    assert "flow_run_id" in _columns(db, "approvals")
    assert _schema_version(db) == 24


def test_reopen_is_idempotent(tmp_path):
    p = tmp_path / "mesh.db"
    db1 = MeshDB(str(p))
    v1 = _schema_version(db1)
    # Re-open the same file → migrations must be a no-op, not re-applied/erroring.
    db2 = MeshDB(str(p))
    assert _schema_version(db2) == v1 == 24
    assert "id" in _columns(db2, "flow_links")


# ---------------------------------------------------------------------------
# flow_links — round-trip, idempotence, forward/reverse lookup
# ---------------------------------------------------------------------------

def test_create_flow_link_round_trip(tmp_path):
    db = _db(tmp_path)
    lid = db.create_flow_link("case-1", "task", "task_abc", "root_task",
                              created_by="operator", metadata={"note": "hi"})
    assert isinstance(lid, int)
    rows = db.list_flow_links(flow_run_id="case-1")
    assert len(rows) == 1
    r = rows[0]
    assert (r["entity_type"], r["entity_id"], r["role"]) == ("task", "task_abc", "root_task")
    assert r["created_by"] == "operator"
    assert r["metadata_json"] == '{"note": "hi"}'


def test_create_flow_link_is_idempotent(tmp_path):
    db = _db(tmp_path)
    a = db.create_flow_link("case-1", "session", "sess-1", "manager")
    b = db.create_flow_link("case-1", "session", "sess-1", "manager")
    assert a == b  # same row id returned, no duplicate
    assert len(db.list_flow_links(flow_run_id="case-1")) == 1
    # A different role on the same entity is a DISTINCT link (not a dup).
    db.create_flow_link("case-1", "session", "sess-1", "reviewer")
    assert len(db.list_flow_links(flow_run_id="case-1")) == 2


def test_list_flow_links_forward_and_reverse(tmp_path):
    db = _db(tmp_path)
    db.create_flow_link("case-1", "task", "t1", "root_task")
    db.create_flow_link("case-1", "session", "s1", "worker")
    db.create_flow_link("case-2", "session", "s1", "evidence")

    # Forward: everything linked to case-1.
    fwd = db.list_flow_links(flow_run_id="case-1")
    assert {r["entity_id"] for r in fwd} == {"t1", "s1"}

    # Reverse: which cases reference session s1?
    rev = db.list_flow_links(entity_type="session", entity_id="s1")
    assert {r["flow_run_id"] for r in rev} == {"case-1", "case-2"}

    # Role filter.
    only_root = db.list_flow_links(flow_run_id="case-1", role="root_task")
    assert len(only_root) == 1 and only_root[0]["entity_id"] == "t1"


# ---------------------------------------------------------------------------
# flow_events — append-only, ordered
# ---------------------------------------------------------------------------

def test_flow_events_append_only_ordered(tmp_path):
    db = _db(tmp_path)
    e1 = db.append_flow_event("case-1", "flow.created", "operator")
    e2 = db.append_flow_event("case-1", "task.dispatched", "manager",
                              entity_type="task", entity_id="t1",
                              payload={"reason": "spawn worker"})
    e3 = db.append_flow_event("case-1", "flow.stage_changed", "system",
                              from_state="intent", to_state="execution")
    assert e1 < e2 < e3  # monotonic ids

    events = db.list_flow_events("case-1")
    assert [e["event_type"] for e in events] == [
        "flow.created", "task.dispatched", "flow.stage_changed",
    ]
    assert events[1]["entity_id"] == "t1"
    assert events[1]["payload_json"] == '{"reason": "spawn worker"}'
    assert events[2]["from_state"] == "intent" and events[2]["to_state"] == "execution"
    # A different case does not see case-1's events.
    assert db.list_flow_events("case-other") == []


# ---------------------------------------------------------------------------
# Legacy writers unaffected — old approval rows carry NULL flow_run_id
# ---------------------------------------------------------------------------

def test_legacy_approval_writer_still_works_with_null_flow(tmp_path):
    db = _db(tmp_path)
    db.create_approval("appr-1", action="run_thing", risk="medium")
    row = db.get_approval("appr-1")
    assert row is not None
    assert row["flow_run_id"] is None  # new column defaults NULL for old writers


# ---------------------------------------------------------------------------
# Vocabulary constants exported for callers/tests
# ---------------------------------------------------------------------------

def test_vocabulary_constants_present():
    assert "task" in FLOW_LINK_ENTITY_TYPES and "flow" in FLOW_LINK_ENTITY_TYPES
    assert "child_flow" in FLOW_LINK_ROLES and "root_task" in FLOW_LINK_ROLES
    assert "flow.created" in FLOW_EVENT_TYPES and "flow.closed" in FLOW_EVENT_TYPES
    assert "operator" in FLOW_EVENT_ACTORS and "system" in FLOW_EVENT_ACTORS
