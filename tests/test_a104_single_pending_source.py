"""A104 architecture guard — ONE source of truth for "what is waiting for this agent".

The 2026-10-09 incident happened because three mechanisms answered that question
from different stores (the wait-group ledger in flow_events, the A84
completion_outbox, the cont:<case>:<N> continuation tokens) and disagreed: the wake
producer admitted a wake, the activation check withdrew it, the finalizer re-armed
it — 1,560 times. These tests fail the build if any parallel pathway comes back:

G01 none of the retired readers / writers / flags exists anywhere in src/ or scripts/
G02 the inbox tables are touched ONLY by src/control/agent_inbox.py (+ migration DDL)
G03 no runtime code reads the wait-group ledger (worker.wait_pending/resolved)
G04 no runtime code reads or writes completion_outbox / flow_runs.continuation_mode
G05 list_flow_events is display-only: its single caller is the audit timeline
G06 addressing is role-free: the completion writer reads the requester column only
G07 the wake producer AND the activation check read pending_for (one read)
G08 nothing creates a continuation (cont:) token or re-arms one
G09 behaviour: the producer and the activation check agree on every message state
    (pending / delivered / acked / dead) — a wake is admitted iff activation keeps it

Static checks parse the Python AST (string constants that are docstrings or
comments are ignored), so prose that mentions history never trips them.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"

RETIRED = (
    "compute_continuation_tick", "continuation_watermark", "list_continuation_rows",
    "record_continuation_consumed", "reconcile_finalizers", "_finalize_producer_token",
    "case_continuation_mode", "pending_case_outbox", "mark_case_outbox_delivered",
    "reviewed_task_ids", "_mark_outbox_delivered_conn", "backfill_missing_task_finished",
    "case_completion_outbox_enabled", "_compute_outbox_tick", "_continue_case_managed",
    "_withdraw_rebound_continuation", "_reconcile_continuation_finalizers",
    "_finalize_continuation", "_case_has_unresolved_wait_group",
    "CASE_COMPLETION_OUTBOX_ENABLED", "PRODUCER_CONSUMING_STATUSES",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")  # tolerate a BOM (some src files carry one)


def _py_files(*roots: Path) -> list[Path]:
    return sorted(p for r in roots for p in r.rglob("*.py") if "__pycache__" not in p.parts)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _code_strings(path: Path) -> list[tuple[int, str]]:
    """(line, value) of every string constant in CODE (docstrings excluded)."""
    tree = ast.parse(_read(path), filename=str(path))
    docs = _docstring_nodes(tree)
    return [
        (n.lineno, n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def _code_names(path: Path) -> set[str]:
    tree = ast.parse(_read(path), filename=str(path))
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
    return out


def _migration_span(path: Path) -> tuple[int, int]:
    """Line span of ``_get_migrations`` in db.py (schema DDL history lives there)."""
    tree = ast.parse(_read(path))
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_get_migrations":
            return n.lineno, n.end_lineno or n.lineno
    raise AssertionError("_get_migrations not found")


DB = SRC / "control" / "db.py"
INBOX = SRC / "control" / "agent_inbox.py"
ORCH = SRC / "orchestrator.py"


def _outside_migrations(path: Path, line: int) -> bool:
    if path != DB:
        return True
    lo, hi = _migration_span(DB)
    return not (lo <= line <= hi)


# G01 ------------------------------------------------------------------------ #
def test_G01_no_retired_pending_state_symbol_survives():
    hits = []
    for f in _py_files(SRC, SCRIPTS):
        names = _code_names(f)
        strings = {v for _, v in _code_strings(f)}
        for sym in RETIRED:
            if sym in names or sym in strings:
                hits.append(f"{f.relative_to(ROOT)}: {sym}")
    assert hits == [], "retired pending-state pathway is back:\n" + "\n".join(hits)


# G02 ------------------------------------------------------------------------ #
@pytest.mark.parametrize("table", ["agent_inbox", "inbox_wait_filters"])
def test_G02_inbox_tables_are_only_touched_by_the_inbox_module(table):
    allowed = {INBOX, SCRIPTS / "a104_seed_inbox.py"}
    hits = []
    for f in _py_files(SRC, SCRIPTS):
        if f in allowed:
            continue
        for line, value in _code_strings(f):
            if re.search(rf"\b(FROM|INTO|UPDATE|JOIN|TABLE)\s+{table}\b", value, re.I) and _outside_migrations(f, line):
                hits.append(f"{f.relative_to(ROOT)}:{line}")
    assert hits == [], f"{table} accessed outside src/control/agent_inbox.py:\n" + "\n".join(hits)


# G03 ------------------------------------------------------------------------ #
def test_G03_no_runtime_reader_of_the_wait_group_ledger():
    vocab_lines = set()
    tree = ast.parse(_read(DB))
    for n in tree.body:  # the documentary FLOW_EVENT_TYPES vocabulary tuple is allowed
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "FLOW_EVENT_TYPES" for t in n.targets):
            vocab_lines = set(range(n.lineno, (n.end_lineno or n.lineno) + 1))
    hits = []
    for f in _py_files(SRC, SCRIPTS):
        for line, value in _code_strings(f):
            if value in ("worker.wait_pending", "worker.wait_resolved"):
                if not (f == DB and line in vocab_lines):
                    hits.append(f"{f.relative_to(ROOT)}:{line} {value}")
    assert hits == [], "the wait-group ledger is being read/written again:\n" + "\n".join(hits)


# G04 ------------------------------------------------------------------------ #
@pytest.mark.parametrize("token", ["completion_outbox", "continuation_mode"])
def test_G04_retired_stores_are_never_read_or_written_at_runtime(token):
    hits = []
    for f in _py_files(SRC):
        for line, value in _code_strings(f):
            if token in value and _outside_migrations(f, line):
                hits.append(f"{f.relative_to(ROOT)}:{line}")
    assert hits == [], f"{token} is live again:\n" + "\n".join(hits)


# G05 ------------------------------------------------------------------------ #
def test_G05_list_flow_events_is_display_only():
    callers = []
    for f in _py_files(SRC, SCRIPTS):
        tree = ast.parse(_read(f))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "list_flow_events":
                kw = {k.arg: getattr(k.value, "value", None) for k in n.keywords}
                callers.append((f.relative_to(ROOT).as_posix(), kw.get("newest")))
    assert callers == [("src/control/routes/work.py", True)], (
        "list_flow_events (an oldest-N audit window) must not feed state; only the newest-window "
        f"timeline may call it. Callers: {callers}"
    )


# G06 ------------------------------------------------------------------------ #
def test_G06_addressing_is_role_free():
    from src.control import agent_inbox as ib

    src = inspect.getsource(ib.record_completion)
    body = src.split('"""')[-1]  # code after the docstring
    for forbidden in ("case_role", "role", "manager", "flow_links"):
        assert forbidden not in body, f"record_completion consults {forbidden!r}: addressing must be role-free"
    assert "sender_session_id" in body


# G07 ------------------------------------------------------------------------ #
def test_G07_producer_and_activation_read_pending_for():
    from src.orchestrator import TaskOrchestrator

    producer = inspect.getsource(TaskOrchestrator._deliver_inbox)
    activation = inspect.getsource(TaskOrchestrator._inbox_wake_obsolete)
    assert "pending_for" in producer and "pending_for" in activation
    obsolete = inspect.getsource(TaskOrchestrator._managed_turn_obsolete)
    assert "_inbox_wake_obsolete" in obsolete
    for name, code in (("producer", producer), ("activation", activation), ("obsolete", obsolete)):
        for legacy in ("list_flow_events", "wait_pending", "flow_events", "completion_outbox"):
            assert legacy not in code, f"{name} reads {legacy}: a second source of pending state"


# G08 ------------------------------------------------------------------------ #
def test_G08_nothing_creates_or_rearms_a_continuation_token():
    hits = []
    for f in _py_files(SRC, SCRIPTS):
        if f == DB:
            continue  # defines the legacy id helpers (still read by history)
        tree = ast.parse(_read(f))
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and n.id in ("CONTINUATION_ACTION", "continuation_task_id"):
                hits.append(f"{f.relative_to(ROOT)}:{n.lineno} {n.id}")
    assert hits == [], "something builds cont: continuation tokens again:\n" + "\n".join(hits)
    db_src = _read(DB)
    assert "attempt\"] = _token_attempt(payload) + 1" not in db_src  # the re-arm that looped


# G09 ------------------------------------------------------------------------ #
def test_G09_producer_and_activation_agree_on_every_message_state(tmp_path, monkeypatch):
    """Whatever state a wake's messages are in, the activation check keeps the wake
    iff a message is still in flight on it — the same answer the producer used."""
    from src.control import agent_inbox as ib
    from tests.inbox_seed import seed_finished_child
    from tests.test_agent_inbox_delivery import _case, _env, _tick, _wakes

    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    assert _tick(o) == 1
    (wake,) = _wakes(db)
    import asyncio

    def keep():
        row = db.get_task(wake["id"])
        return asyncio.run(o._managed_turn_obsolete(row)) is None

    assert keep() and db.pending_for("sess-1").carried_by(wake["id"])       # delivered → keep
    with db._write() as conn:                                               # acked → drop
        ib.ack_about_task(conn, "task_w1", ib.now_iso(), reason="reviewed")
    assert not keep() and not db.pending_for("sess-1").carried_by(wake["id"])
    with db._write() as conn:                                               # back to delivered → keep
        conn.execute("UPDATE agent_inbox SET state = 'delivered' WHERE about_task_id = 'task_w1'")
    assert keep()
    with db._write() as conn:                                               # dead → drop
        ib.kill_case(conn, cid, "case_closed", ib.now_iso())
    assert not keep()
