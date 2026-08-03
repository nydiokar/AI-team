"""
A54 / M3.4 Job 2 — durable Case reconstruction (``get_case_brief``) + boot reconcile.

A Manager that lost its in-process context (compaction, restart, respawn) must be
able to pick a Case back up from the DB ALONE — objective + criteria + round cap +
rounds used + dispatched workers + latest verdict + open/ready waits + armed
wait-groups and their satisfaction — in ONE bounded read, and re-establish its
outstanding waits/groups. These tests exercise the whole contract against a real
``MeshDB`` (no paid CLI, no live backend):

  * ``get_case_brief`` returns EVERY field from the seeded ledger alone.
  * it is a BOUNDED single query set (no per-worker fanout).
  * ``boot_reconcile_case`` reconciles waits AND re-arms live groups IDEMPOTENTLY
    (a double-boot writes no duplicate markers).
  * flag OFF ⇒ byte-identical: the brief is read-only; the boot hook no-ops.
"""

from src.control.db import MeshDB


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _relay_on(monkeypatch) -> None:
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")


def _cont_on(monkeypatch) -> None:
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")


def _dispatch_worker(db: MeshDB, case_id: str, task_id: str) -> None:
    """Seed a dispatched worker exactly as the orchestrator does: a task link
    (entity_type='task', role='task', created_by='manager') + a task.attached event."""
    db.create_flow_link(case_id, "task", task_id, "task", created_by="manager")


def _finished(db: MeshDB, case_id: str, task_id: str, outcome: str = "success") -> None:
    db.append_flow_event(
        case_id, "task.finished", "worker",
        entity_type="task", entity_id=task_id,
        payload={"outcome": outcome},
    )


def _events(db: MeshDB, case_id: str, event_type: str) -> list:
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


def _wait_group_markers(db: MeshDB, case_id: str, gid: str) -> list:
    return [
        e for e in db.list_flow_events(case_id)
        if e.get("entity_type") == "wait_group"
        and e.get("entity_id") == gid
        and e.get("event_type") == "worker.wait_pending"
    ]


# --------------------------------------------------------------------------- #
# Acceptance 1 — get_case_brief returns the FULL state from the DB alone       #
# --------------------------------------------------------------------------- #

def _seed_full_case(db: MeshDB) -> str:
    """A Case with 2 dispatched workers (one finished+reviewed, one in-flight),
    per-task waits, and an armed ANY wait-group over both."""
    case_id = db.open_case(
        "Ship the feature", "sess-mgr", role="manager",
        completion_criteria="tests green; diff reviewed", round_cap=7,
    )
    _dispatch_worker(db, case_id, "task_done")
    _dispatch_worker(db, case_id, "task_live")
    # A worker session link (kept warm) for task_done.
    db.create_flow_link(case_id, "session", "sess-worker-1", "worker", created_by="manager")
    # task_done finished + accepted; per-task wait recorded for both.
    db.record_worker_wait(case_id, "task_done")
    db.record_worker_wait(case_id, "task_live")
    _finished(db, case_id, "task_done", "success")
    db.append_flow_event(
        case_id, "review.accepted", "manager",
        payload={"verdict": "accepted", "reason": "diff verified"},
    )
    # Arm an ANY group over both.
    db.arm_wait_group(case_id, "batch-1", "ANY", ["task_done", "task_live"])
    return case_id


def test_brief_returns_full_state_from_db_alone(tmp_path, monkeypatch):
    _relay_on(monkeypatch)
    _cont_on(monkeypatch)
    db = _db(tmp_path)
    case_id = _seed_full_case(db)

    brief = db.get_case_brief(case_id)
    assert brief is not None

    # --- objective + criteria + caps (against the seed) ---
    assert brief["case_id"] == case_id
    assert brief["objective"] == "Ship the feature"
    assert brief["status"] is None  # open
    assert brief["completion_criteria"] == ["tests green; diff reviewed"]
    assert brief["round_cap"] == 7
    assert brief["rounds_used"] == 0
    assert brief["rounds_remaining"] == 7

    # --- dispatched workers (both), with finished/outcome per worker ---
    workers = {w["task_id"]: w for w in brief["workers"]}
    assert set(workers) == {"task_done", "task_live"}
    assert workers["task_done"]["finished"] is True
    assert workers["task_done"]["outcome"] == "success"
    assert workers["task_live"]["finished"] is False
    assert workers["task_live"]["outcome"] is None
    assert "sess-worker-1" in brief["worker_session_ids"]

    # --- latest Case-level review verdict ---
    assert brief["latest_review"]["verdict"] == "accepted"
    assert brief["latest_review"]["event_type"] == "review.accepted"

    # --- waits: task_done finished-but-unresolved ⇒ ready; task_live ⇒ open ---
    assert brief["ready_waits"] == ["task_done"]
    assert brief["open_waits"] == ["task_live"]

    # --- armed wait-group + live satisfaction (ANY, task_done finished) ---
    assert len(brief["wait_groups"]) == 1
    g = brief["wait_groups"][0]
    assert g["wait_group_id"] == "batch-1"
    assert g["condition"] == "ANY"
    assert set(g["members"]) == {"task_done", "task_live"}
    assert g["satisfied"] is True  # task_done finished + unconsumed
    assert g["presented_task_ids"] == ["task_done"]


def test_brief_unknown_case_is_none(tmp_path, monkeypatch):
    db = _db(tmp_path)
    assert db.get_case_brief("nope") is None


def test_brief_bounded_query_set_no_per_worker_fanout(tmp_path, monkeypatch):
    """The brief must be a BOUNDED read set — its per-query count must NOT grow with
    the number of workers (no N+1). Compare a 2-worker Case to a 12-worker Case and
    assert the SQL execute count is identical."""
    _relay_on(monkeypatch)
    _cont_on(monkeypatch)

    def _count_executes(n_workers: int) -> int:
        db = _db(tmp_path / f"w{n_workers}")
        case_id = db.open_case("obj", "sess", role="manager", round_cap=5)
        for i in range(n_workers):
            _dispatch_worker(db, case_id, f"task_{i}")
            _finished(db, case_id, f"task_{i}")
        db.arm_wait_group(case_id, "g", "ALL", [f"task_{i}" for i in range(n_workers)])

        conn = db._conn()
        calls = {"n": 0}

        def _trace(_sql: str) -> None:
            calls["n"] += 1

        conn.set_trace_callback(_trace)
        try:
            db.get_case_brief(case_id)
        finally:
            conn.set_trace_callback(None)
        return calls["n"]

    assert _count_executes(2) == _count_executes(12)


# --------------------------------------------------------------------------- #
# Acceptance 2 — boot reconcile: reconcile waits + re-arm groups, idempotent    #
# --------------------------------------------------------------------------- #

def test_boot_reconcile_reconciles_and_rearms_idempotently(tmp_path, monkeypatch):
    _relay_on(monkeypatch)
    _cont_on(monkeypatch)
    db = _db(tmp_path)
    case_id = _seed_full_case(db)

    # Pre: task_done finished with an unresolved per-task wait; group armed once.
    assert _events(db, case_id, "worker.wait_resolved") == []
    assert len(_wait_group_markers(db, case_id, "batch-1")) == 1

    # First boot: reconcile clears the finished-wait, re-arms the live group.
    r1 = db.boot_reconcile_case(case_id)
    assert r1["ok"] is True
    resolved = [x["task_id"] for x in r1["reconciled"]["resolved"]]
    assert "task_done" in resolved                 # finished ⇒ resolved
    assert r1["rearmed"] == ["batch-1"]            # live group re-armed

    resolved_after_1 = _events(db, case_id, "worker.wait_resolved")
    group_markers_after_1 = _wait_group_markers(db, case_id, "batch-1")

    # Second boot (double-boot): idempotent — NO duplicate markers written.
    r2 = db.boot_reconcile_case(case_id)
    assert r2["ok"] is True
    # task_done already resolved ⇒ not re-resolved; the group is still armed ⇒
    # re-arm is an idempotent no-op (returns the existing marker id).
    assert _events(db, case_id, "worker.wait_resolved") == resolved_after_1
    assert _wait_group_markers(db, case_id, "batch-1") == group_markers_after_1
    # Exactly one group marker ever — a double-boot did NOT create a second group.
    assert len(_wait_group_markers(db, case_id, "batch-1")) == 1


# --------------------------------------------------------------------------- #
# Acceptance 3/6 — flag OFF ⇒ byte-identical                                    #
# --------------------------------------------------------------------------- #

def test_brief_is_read_only_regardless_of_flags(tmp_path, monkeypatch):
    """The brief writes NOTHING — the event/link ledger is byte-identical before
    and after, whether the flags are ON or OFF."""
    # Flags OFF for the read path.
    monkeypatch.delenv("DURABLE_RELAY_ENABLED", raising=False)
    monkeypatch.delenv("CASE_CONTINUATION_ENABLED", raising=False)
    db = _db(tmp_path)
    case_id = db.open_case("obj", "sess", role="manager", round_cap=5)
    _dispatch_worker(db, case_id, "task_1")
    _finished(db, case_id, "task_1")

    before_events = db.list_flow_events(case_id)
    before_links = db.list_flow_links(flow_run_id=case_id)
    brief = db.get_case_brief(case_id)
    assert brief is not None
    assert db.list_flow_events(case_id) == before_events  # zero writes
    assert db.list_flow_links(flow_run_id=case_id) == before_links


def test_boot_reconcile_noop_when_relay_off(tmp_path, monkeypatch):
    monkeypatch.delenv("DURABLE_RELAY_ENABLED", raising=False)
    db = _db(tmp_path)
    case_id = db.open_case("obj", "sess", role="manager")
    _dispatch_worker(db, case_id, "task_1")
    _finished(db, case_id, "task_1")

    before = db.list_flow_events(case_id)
    result = db.boot_reconcile_case(case_id)
    assert result == {"ok": False, "reason": "durable_relay_disabled"}
    assert db.list_flow_events(case_id) == before  # byte-identical: no writes
