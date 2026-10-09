#!/usr/bin/env python3
"""A104 Phase 4 — one-time live data migration onto the agent inbox.

For every OPEN or BLOCKED Case (legacy wait-group Cases and A84 outbox Cases alike):

1. **Seed** a ``pending`` inbox message for every dispatched child that FINISHED and was
   neither reviewed (a tagged ``review.*``) nor consumed (a completed continuation's
   watermark, or a delivered outbox row) — the never-built A84 design T9. Historical
   children carry no requester, so the recipient is inferred: (a) the Case member that
   was executing a turn when the child was dispatched (the R1 rule, applied to history),
   else (b) the Case's Manager seat holder at dispatch time (the rebind record). The
   inference used is reported per row.
2. **Ack** completions that were already reviewed or consumed but whose A84 outbox row was
   never cleared (e.g. 83d10aec's ``task_7b175284`` — its review sat past the oldest-500
   window).
3. **Retire junk**: A84 rows for Manager-own turns, operator messages and wake turns become
   ``dead(superseded)``.
4. **Re-arm live ALL wait groups** as D2 delivery filters (only groups with an unfinished member).
5. **Discharge** every pending/claimed ``cont:`` continuation token (``cancelled``,
   ``superseded_by_agent_inbox``) so no producer can resume it.
6. **Re-label** the telemetry of never-run (withdrawn) turns from ``cancelled`` to
   ``withdrawn`` (I6 — nothing is deleted, D6).

``--dry-run`` (default) computes the plan and the report and writes NOTHING. ``--apply``
executes the whole plan in ONE transaction and re-verifies: every genuine completion must
end ``pending`` or ``acked`` (lost = 0) or the transaction is rolled back. Idempotent: a
second ``--apply`` changes nothing.

Usage::

    python scripts/a104_seed_inbox.py --db <mesh.db> [--apply] [--report <path.json>]

Open the PRODUCTION database only after a fresh backup and with the A104 Gate 3 code
deployed (the schema must be at migration 46 — opening it here with older code would not).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field  # noqa: E402

from src.control import agent_inbox as ib  # noqa: E402
from src.control.db import MeshDB  # noqa: E402

LIVE: tuple[str, ...] = ("queued", "pending", "claimed", "running", "recovery_required")
CLOSED: tuple[str, ...] = ("closed", "cancelled", "completed", "done")


class ChildAction(BaseModel):
    task_id: str
    status: str
    action: str  # seed_pending | revive_pending | ack | keep
    reason: str
    recipient: Optional[str] = None
    inference: Optional[str] = None
    inbox_before: Optional[str] = None


class CasePlan(BaseModel):
    case_id: str
    mode: str
    status: str
    children: list[ChildAction] = Field(default_factory=list)
    junk_retired: list[str] = Field(default_factory=list)
    filters_armed: list[str] = Field(default_factory=list)
    pending_before: list[str] = Field(default_factory=list)
    pending_after: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    generated_at: str
    db_path: str
    applied: bool = False
    cases: list[CasePlan] = Field(default_factory=list)
    tokens_discharged: list[str] = Field(default_factory=list)
    telemetry_relabelled: int = 0
    genuine: list[str] = Field(default_factory=list)
    genuine_lost: list[str] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _q(conn, sql: str, *args: object) -> list[dict[str, object]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _consumed(conn, case_id: str) -> set[str]:
    out: set[str] = set()
    for r in _q(conn, "SELECT result FROM mesh_tasks WHERE action = 'manager_continuation' "
                      "AND status = 'completed' AND id LIKE ?", f"cont:{case_id}:%"):
        try:
            out.update(str(t) for t in (json.loads(str(r["result"] or "{}")).get("consumed_task_ids") or []))
        except ValueError:
            continue
    out.update(str(r["child_task_id"]) for r in _q(
        conn, "SELECT child_task_id FROM completion_outbox WHERE case_id = ? AND delivered_at IS NOT NULL",
        case_id))
    return out


def _reviewed(conn, case_id: str) -> set[str]:
    return {str(r["entity_id"]) for r in _q(
        conn, "SELECT DISTINCT entity_id FROM flow_events WHERE flow_run_id = ? AND event_type LIKE 'review.%' "
              "AND entity_type = 'task' AND entity_id IS NOT NULL", case_id)}


def _children(conn, case_id: str) -> list[dict[str, object]]:
    return _q(conn, """
        SELECT t.id, t.status, t.session_id, t.sender_session_id, t.created_at, t.completed_at
          FROM mesh_tasks t
         WHERE t.id IN (SELECT entity_id FROM flow_links WHERE flow_run_id = ? AND entity_type = 'task'
                         AND role = 'task' AND created_by = 'manager')
            OR (t.flow_run_id = ? AND t.sender_session_id IS NOT NULL)
         ORDER BY t.created_at""", case_id, case_id)


def _infer_requester(conn, case_id: str, child: dict[str, object]) -> tuple[Optional[str], str]:
    if child.get("sender_session_id"):
        return str(child["sender_session_id"]), "recorded"
    at = str(child["created_at"])
    rows = _q(conn, """
        SELECT DISTINCT t.session_id FROM flow_links l JOIN mesh_tasks t ON t.session_id = l.entity_id
         WHERE l.flow_run_id = ? AND l.entity_type = 'session' AND t.session_id != COALESCE(?, '')
           AND COALESCE(t.claimed_at, t.started_at, t.created_at) <= ?
           AND COALESCE(t.completed_at, '9999') >= ?
           AND t.status NOT IN ('queued', 'withdrawn')
           AND COALESCE(json_extract(t.payload, '$.metadata.source'), '') != 'automation_session'
         LIMIT 2""", case_id, child.get("session_id"), at, at)
    if len(rows) == 1:
        return str(rows[0]["session_id"]), "executing_member_at_dispatch"
    seat = _q(conn, "SELECT entity_id FROM flow_links WHERE flow_run_id = ? AND entity_type = 'session' "
                    "AND role = 'manager' AND created_at <= ? ORDER BY id DESC LIMIT 1", case_id, at)
    if seat:
        return str(seat[0]["entity_id"]), "manager_seat_at_dispatch"
    return None, "unresolved"


def build_plan(db: MeshDB, db_path: str) -> Plan:
    conn = db._conn()
    plan = Plan(generated_at=_now(), db_path=db_path)
    cases = _q(conn, "SELECT flow_run_id, COALESCE(continuation_mode, 'legacy') AS mode, "
                     "COALESCE(status, '') AS status FROM flow_runs "
                     f"WHERE COALESCE(status, '') NOT IN ({','.join('?' * len(CLOSED))}) ORDER BY created_at",
               *CLOSED)
    for c in cases:
        cid = str(c["flow_run_id"])
        cp = CasePlan(case_id=cid, mode=str(c["mode"]), status=str(c["status"]) or "open")
        consumed, reviewed = _consumed(conn, cid), _reviewed(conn, cid)
        inbox = {str(r["about_task_id"]): r for r in _q(
            conn, "SELECT about_task_id, state, last_error, recipient_session_id FROM agent_inbox WHERE case_id = ?",
            cid)}
        cp.pending_before = sorted(t for t, r in inbox.items() if r["state"] in ("pending", "delivered"))
        child_ids: set[str] = set()
        for ch in _children(conn, cid):
            tid, st = str(ch["id"]), str(ch["status"] or "")
            child_ids.add(tid)
            if st in LIVE:
                continue  # its completion will be written by the terminal txn (requester or not)
            existing = inbox.get(tid)
            before = f"{existing['state']}:{existing['last_error'] or ''}" if existing else None
            if tid in reviewed or tid in consumed:
                why = "reviewed" if tid in reviewed else "consumed_pre_inbox"
                if existing and existing["state"] in ("pending", "delivered"):
                    cp.children.append(ChildAction(task_id=tid, status=st, action="ack", reason=why,
                                                   inbox_before=before))
                elif existing and existing["state"] == "dead" and existing["last_error"] == "unaddressed_pre_inbox":
                    cp.children.append(ChildAction(task_id=tid, status=st, action="ack", reason=why,
                                                   inbox_before=before))
                continue
            plan.genuine.append(tid)
            if existing and existing["state"] in ("pending", "delivered", "acked"):
                cp.children.append(ChildAction(task_id=tid, status=st, action="keep", reason="already_in_inbox",
                                               recipient=str(existing["recipient_session_id"]),
                                               inbox_before=before))
                continue
            recipient, how = _infer_requester(conn, cid, ch)
            cp.children.append(ChildAction(
                task_id=tid, status=st, action="revive_pending" if existing else "seed_pending",
                reason="finished_unreviewed_unconsumed", recipient=recipient, inference=how, inbox_before=before,
            ))
        for tid, r in inbox.items():
            if tid in child_ids:
                continue
            if r["state"] in ("pending", "delivered") or (
                r["state"] == "dead" and r["last_error"] == "unaddressed_pre_inbox"
            ):
                cp.junk_retired.append(tid)
        for g in _q(conn, """
            SELECT p.entity_id AS gid, json_extract(p.payload_json, '$.condition') AS cond,
                   json_extract(p.payload_json, '$.member_task_ids') AS members
              FROM flow_events p
             WHERE p.flow_run_id = ? AND p.event_type = 'worker.wait_pending' AND p.entity_type = 'wait_group'
               AND NOT EXISTS (SELECT 1 FROM flow_events r WHERE r.flow_run_id = p.flow_run_id
                     AND r.event_type = 'worker.wait_resolved' AND r.entity_type = 'wait_group'
                     AND r.entity_id = p.entity_id AND r.id > p.id)""", cid):
            if str(g["cond"] or "ANY").upper() not in ("ALL", "NAMED"):
                continue
            members = [str(m) for m in json.loads(str(g["members"] or "[]"))]
            live = _q(conn, f"SELECT id FROM mesh_tasks WHERE id IN ({','.join('?' * len(members) or '?')}) "
                            f"AND status IN ({','.join('?' * len(LIVE))})", *(members or [""]), *LIVE)
            if live:
                cp.filters_armed.append(str(g["gid"]))
        plan.cases.append(cp)
    plan.tokens_discharged = [str(r["id"]) for r in _q(
        conn, "SELECT id FROM mesh_tasks WHERE id LIKE 'cont:%' AND status IN ('pending', 'claimed') "
              "AND COALESCE(queue_protocol, 0) = 0 ORDER BY id")]
    plan.telemetry_relabelled = int(conn.execute(
        "SELECT COUNT(*) FROM llm_turns WHERE final_status = 'cancelled' "
        "AND turn_id IN (SELECT id FROM mesh_tasks WHERE status = 'withdrawn')").fetchone()[0])
    return plan


def apply_plan(db: MeshDB, plan: Plan) -> None:
    """Execute the plan in ONE transaction; roll back if any genuine completion
    would be lost."""
    with db._write() as conn:
        _apply_rows(conn, plan, _now())
        lost = verify(conn, plan)
        if lost:
            raise RuntimeError(f"genuine completions would be lost: {lost} — rolled back")
    plan.applied = True


def _apply_rows(conn, plan: Plan, now: str) -> None:
    for cp in plan.cases:
        for a in cp.children:
            mid = ib.completion_message_id(a.task_id)
            if a.action == "ack":
                conn.execute(
                    "UPDATE agent_inbox SET state = 'acked', acked_at = ?, resolution = ?, updated_at = ?, "
                    "last_error = NULL WHERE message_id = ? AND state IN ('pending', 'delivered', 'dead')",
                    (now, a.reason, now, mid))
            elif a.action in ("seed_pending", "revive_pending"):
                if not a.recipient:
                    continue  # reported as lost by the verification below
                row = conn.execute("SELECT session_id, status FROM mesh_tasks WHERE id = ?",
                                   (a.task_id,)).fetchone()
                outcome = "success" if row["status"] == "completed" else str(row["status"])
                conn.execute(
                    "INSERT INTO agent_inbox (message_id, recipient_session_id, sender_session_id, about_task_id, "
                    "case_id, kind, outcome, state, attempts, created_at, updated_at, resolution) "
                    "VALUES (?, ?, ?, ?, ?, 'completion', ?, 'pending', 0, ?, ?, ?) "
                    "ON CONFLICT(message_id) DO UPDATE SET recipient_session_id = excluded.recipient_session_id, "
                    "state = 'pending', attempts = 0, next_attempt_at = NULL, last_error = NULL, "
                    "alerted_at = NULL, resolution = excluded.resolution, updated_at = excluded.updated_at "
                    "WHERE agent_inbox.state = 'dead'",
                    (mid, a.recipient, row["session_id"], a.task_id, cp.case_id, outcome, now, now,
                     f"seeded_a104:{a.inference}"))
        for tid in cp.junk_retired:
            conn.execute(
                "UPDATE agent_inbox SET state = 'dead', last_error = 'superseded', updated_at = ?, "
                "alerted_at = COALESCE(alerted_at, ?) WHERE message_id = ? "
                "AND (state IN ('pending', 'delivered') OR (state = 'dead' AND last_error = 'unaddressed_pre_inbox'))",
                (now, now, ib.completion_message_id(tid)))
        for gid in cp.filters_armed:
            g = conn.execute(
                "SELECT json_extract(payload_json, '$.member_task_ids') AS m FROM flow_events "
                "WHERE flow_run_id = ? AND event_type = 'worker.wait_pending' AND entity_type = 'wait_group' "
                "AND entity_id = ? ORDER BY id DESC LIMIT 1", (cp.case_id, gid)).fetchone()
            ib.arm_filter(conn, cp.case_id, gid, "ALL", [str(x) for x in json.loads(g["m"] or "[]")], now)
    for tok in plan.tokens_discharged:
        conn.execute(
            "UPDATE mesh_tasks SET status = 'cancelled', error = 'superseded_by_agent_inbox', "
            "completed_at = ?, updated_at = ? WHERE id = ? AND status IN ('pending', 'claimed')",
            (now, now, tok))
    conn.execute(
        "UPDATE llm_turns SET final_status = 'withdrawn' WHERE final_status = 'cancelled' "
        "AND turn_id IN (SELECT id FROM mesh_tasks WHERE status = 'withdrawn')")


def verify(conn, plan: Plan) -> list[str]:
    """Every genuine completion must be pending (or delivered) or acked in the inbox."""
    lost: list[str] = []
    for tid in plan.genuine:
        row = conn.execute("SELECT state, recipient_session_id FROM agent_inbox WHERE message_id = ?",
                           (ib.completion_message_id(tid),)).fetchone()
        if row is None or row["state"] not in ("pending", "delivered", "acked") or not row["recipient_session_id"]:
            lost.append(tid)
    return lost


def _after(db: MeshDB, plan: Plan) -> None:
    conn = db._conn()
    for cp in plan.cases:
        cp.pending_after = sorted(str(r["about_task_id"]) for r in _q(
            conn, "SELECT about_task_id FROM agent_inbox WHERE case_id = ? AND state IN ('pending', 'delivered')",
            cp.case_id))


def render_md(plan: Plan, simulated_lost: list[str]) -> str:
    lines = [f"# A104 Phase 4 inbox migration — {'APPLIED' if plan.applied else 'DRY-RUN'}",
             f"- db: `{plan.db_path}` · generated {plan.generated_at}",
             f"- genuine completions: {len(plan.genuine)} {plan.genuine} · **lost: {len(simulated_lost)}** {simulated_lost}",
             f"- continuation tokens discharged: {len(plan.tokens_discharged)} {plan.tokens_discharged}",
             f"- never-run telemetry rows relabelled cancelled→withdrawn: {plan.telemetry_relabelled}", "",
             "| case | mode | status | pending before | pending after | seeded / revived | acked | junk retired | filters |",
             "|---|---|---|---|---|---|---|---|---|"]
    for cp in plan.cases:
        seeded = [f"{a.task_id}→{a.recipient} ({a.inference})" for a in cp.children
                  if a.action in ("seed_pending", "revive_pending")]
        acked = [f"{a.task_id} ({a.reason})" for a in cp.children if a.action == "ack"]
        lines.append(f"| {cp.case_id[:8]} | {cp.mode} | {cp.status} | {cp.pending_before} | {cp.pending_after} | "
                     f"{seeded} | {acked} | {cp.junk_retired} | {cp.filters_armed} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true", help="execute (default is a dry-run that writes nothing)")
    ap.add_argument("--report", default=None, help="write <path>.json and <path>.md")
    args = ap.parse_args()
    db = MeshDB(args.db)
    plan = build_plan(db, args.db)
    if args.apply:
        apply_plan(db, plan)
        simulated_lost = verify(db._conn(), plan)
    else:
        # Dry-run: execute in a SAVEPOINT on a private connection and roll back, so the
        # report's "after" and "lost" columns are the real outcome — nothing persists.
        simulated_lost = _simulate(db, plan)
    if args.apply:
        _after(db, plan)
    out = render_md(plan, simulated_lost)
    if args.report:
        Path(args.report).with_suffix(".json").write_text(plan.model_dump_json(indent=2))
        Path(args.report).with_suffix(".md").write_text(out)
    print(out)
    return 1 if simulated_lost else 0


def _simulate(db: MeshDB, plan: Plan) -> list[str]:
    """Dry-run: run the exact apply body inside a write txn, read the outcome, then
    ROLL BACK — the report shows the real after-state; nothing persists."""
    class _Abort(Exception):
        pass

    lost: list[str] = []
    try:
        with db._write() as conn:
            _apply_rows(conn, plan, _now())
            lost = verify(conn, plan)
            for cp in plan.cases:
                cp.pending_after = sorted(str(r["about_task_id"]) for r in _q(
                    conn, "SELECT about_task_id FROM agent_inbox WHERE case_id = ? "
                          "AND state IN ('pending', 'delivered')", cp.case_id))
            raise _Abort()
    except _Abort:
        pass
    return lost


if __name__ == "__main__":
    raise SystemExit(main())
