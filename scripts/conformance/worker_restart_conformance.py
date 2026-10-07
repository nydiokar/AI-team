#!/usr/bin/env python3
"""A98 — Worker-restart graceful-reconcile CONFORMANCE CHECKER (frozen expected behavior).

This file is BOTH the frozen reference (the STEPS contract below is the authoritative
expected causal chain) AND the comparator: given a node and a time window it reads the
live mesh.db (read-only) + the controller event logs and emits, step by step:

    PASS  — the expected event/effect is present
    FAIL  — expected but absent/contradicted (with the likely cause)
    UNKNOWN — not determinable from the available trail (itself a finding: missing log)

It then names the FIRST divergent step and its diagnosis bucket so a reviewer can say
"on step N, X was supposed to happen; Y happened instead — flag off / half-built /
missing carrier / missing action unit / operator-inaction".

Scenario it is frozen against: a Manager is AWAITING on a worker (wait-group armed);
the worker daemon on node N restarts (OOM/crash/deploy); its in-memory SDK drivers die;
the system must detect it, page the operator once, and re-establish the Manager + worker
sessions on the SAME Case — NOT dead-end on resume-into-a-corpse.

Usage:
    python scripts/conformance/worker_restart_conformance.py --print-spec
    python scripts/conformance/worker_restart_conformance.py --node Horse --since 2026-10-07T01:40 --until 2026-10-07T08:30
    python scripts/conformance/worker_restart_conformance.py --node Horse            # default: last 6h
    python scripts/conformance/worker_restart_conformance.py --node <n> --json       # machine-readable

Read-only. Never writes. Safe against the live DB (opens ?mode=ro).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

DB_PATH = os.path.expanduser("~/ai-team-data/controller/state/mesh.db")
LOG_GLOBS = [
    os.path.expanduser("~/ai-team-data/controller/logs/events.ndjson*"),
    os.path.expanduser("~/ai-team-data/controller/logs/orchestrator.log*"),
]

PASS, FAIL, UNKNOWN, INFO = "PASS", "FAIL", "UNKNOWN", "INFO"


# ───────────────────────────── FROZEN CONTRACT ──────────────────────────────
# Each step: what SHOULD happen, the action unit (component), the ledger/log
# evidence a checker looks for, and the diagnosis bucket when it is absent.
@dataclass
class Step:
    n: int
    name: str
    trigger: str
    action_unit: str
    effect: str
    page: str                 # who/what gets messaged (or "—")
    evidence: str             # what the checker inspects
    diagnosis_if_absent: str  # how to pinpoint the failure
    check: Optional[Callable] = field(default=None, repr=False)


FROZEN_STEPS = [
    Step(
        n=0, name="baseline (pre-restart)",
        trigger="Manager armed a wait-group on worker task(s)",
        action_unit="Manager role loop (HARNESS_FLOW_DRIVE, CASE_CONTINUATION_ENABLED)",
        effect="Manager AWAITING_INPUT/driver_live; flow has an open worker.wait_pending; node N online @ incarnation I0",
        page="—",
        evidence="flow_events worker.wait_pending without matching wait_resolved; sessions.driver_status='live'",
        diagnosis_if_absent="No wait armed ⇒ scenario N/A (not a restart-recovery case).",
    ),
    Step(
        n=1, name="worker process dies / restarts",
        trigger="worker daemon exits (OOM / crash / deploy)",
        action_unit="(external) OS / PM2 / Docker",
        effect="in-memory SDK drivers on node N destroyed; backend conversation persists on disk (backend_session_id)",
        page="O7 early-warning (if memory-driven): event=worker_memory_pressure on heartbeat BEFORE death",
        evidence="new worker process instance; (optional) event=worker_memory_pressure in the minutes prior",
        diagnosis_if_absent="If OOM with NO prior worker_memory_pressure ⇒ O7 watchdog off/old worker code (WORKER_MEMORY_WATCHDOG_DISABLED or worker not restarted onto A98).",
    ),
    Step(
        n=2, name="re-register + mark sessions lost (O3, pre-existing)",
        trigger="restarted worker POST /nodes/register with a NEW incarnation I1",
        action_unit="NodeRegistry.register → db.mark_driver_sessions_lost_for_node (node_registry.py)",
        effect="nodes.incarnation_id I0→I1; node N's idle/awaiting SDK sessions driver_status live→lost; claims released",
        page="—",
        evidence="event=driver_sessions_marked_lost node_id=N; event=orphaned_claims_released; sessions flipped to driver_status='lost'",
        diagnosis_if_absent="No mark-lost ⇒ MISSING CARRIER: worker sent no incarnation_id (old worker code), OR sessions were BUSY at restart (mark-lost excludes BUSY — known gap), OR controller didn't see the re-register.",
    ),
    Step(
        n=3, name="detect restart + page operator (O5)",
        trigger="orchestrator reconcile tick observes node N incarnation I0→I1",
        action_unit="TaskOrchestrator._detect_node_restarts_once → NotificationService.notify_restart",
        effect="durable warning + best-effort Web Push + Telegram",
        page="OPERATOR: Web Push + Telegram 'Worker restarted — N, K sessions lost'",
        evidence="event=node_restart_detected node_id=N; event=node_restart_notification",
        diagnosis_if_absent="No detect/page ⇒ FLAG RESTART_NOTIFY_DISABLED on, OR reconcile loop not running (MESH off / interval 0), OR gateway on pre-A98 image (O5 not deployed). No Push/Telegram received but event present ⇒ notifier/bot unconfigured (best-effort).",
    ),
    Step(
        n=4, name="Manager wake routes to FRESH FORK (O1)",
        trigger="wait resolved OR wake tick on the lost Manager session (AWAITING_INPUT + driver_status='lost')",
        action_unit="_continue_case_once → _mesh_dispatch_payload (action=create_session) + _maybe_inject_restart_recovery_context",
        effect="fresh create_session (role re-boot + A54 boot-reconcile + <prior_context>); driver_status lost→live; SAME Case",
        page="—",
        evidence="Manager's next mesh_task action='create_session' (NOT resume_session) AND error_class!='session_lost'; event=restart_context_injected",
        diagnosis_if_absent="action='resume_session' + error_class='session_lost' ⇒ FLAG RESTART_LOST_SESSION_FORK_DISABLED on, OR session turn-queue-enrolled (A82, scoped out), OR gateway pre-A98. This is the ORIGINAL incident behavior.",
    ),
    Step(
        n=5, name="dead-at-ERROR Manager respawns (O2 safety net)",
        trigger="wake tick on a satisfied Case whose Manager is ERROR+driver_status='lost' (fork failed / died at idle / terminal session_lost)",
        action_unit="_continue_case_once → _is_restart_dead_session → _handle_dead_manager_session (A55)",
        effect="auto-respawn OR case_manager_respawn approval; NEW Manager on SAME Case via get_case_brief; waits re-armed",
        page="OPERATOR: case_manager_respawn approval (if CASE_RESPAWN_REQUIRES_APPROVAL, default ON)",
        evidence="flow_event case.manager_respawned OR approval action=case_manager_respawn; (else) event=case.manager_unavailable escalation",
        diagnosis_if_absent="Manager ERROR+lost with no respawn/approval ⇒ FLAG CASE_CONTINUATION_ENABLED off OR RESPAWN_ON_RESTART_ERROR_DISABLED on; approval raised but never actioned ⇒ OPERATOR-INACTION (wedged Case); respawn_failed ⇒ MISSING CARRIER (no placement node / worker offline).",
    ),
    Step(
        n=6, name="worker session re-established + continuation",
        trigger="now-live Manager (or pending re-run) dispatches the worker's next turn",
        action_unit="same O1 routing for the worker session",
        effect="worker session recovers (create_session fork), committed work NOT redone from scratch; wait resolved/re-armed",
        page="—",
        evidence="worker mesh_task action='create_session' success; no repeated cold resume_session failures; flow progresses (wait_resolved / review.*)",
        diagnosis_if_absent="Repeated resume_session+session_lost on the worker ⇒ same O1 flag/enrollment cause as step 4; expensive repeated cold resumes ⇒ cost-guard regression.",
    ),
    Step(
        n=7, name="FINAL STATE",
        trigger="—",
        action_unit="—",
        effect="Case open & progressing; Manager live on same Case; worker live; operator paged once (+approval if gated)",
        page="—",
        evidence="Manager session driver_status='live'/AWAITING_INPUT; Case not silently stalled; ≤1 restart page",
        diagnosis_if_absent="Manager ERROR+lost & Case open with no respawn/approval/page ⇒ THE 2026-10-07 INCIDENT recurred — walk back to the first FAIL above.",
    ),
]


# ───────────────────────────── evidence helpers ─────────────────────────────
def _ro_conn():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _iso(s: str) -> str:
    return s


def _log_lines(since: str, until: str):
    """Yield (ts, line) from controller logs within [since, until], best-effort."""
    for pat in LOG_GLOBS:
        for path in sorted(glob.glob(pat)):
            try:
                with open(path, "r", errors="replace") as fh:
                    for line in fh:
                        # events.ndjson lines carry a leading "timestamp"; orchestrator.log
                        # lines start with an ISO-ish stamp. Cheap window filter by substring.
                        if _in_window(line, since, until):
                            yield line.rstrip("\n")
            except Exception:
                continue


def _in_window(line: str, since: str, until: str) -> bool:
    # Extract the first 19-char ISO date if present; keep lines we can't date (safer to include).
    import re
    m = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", line)
    if not m:
        return True
    ts = m.group(0).replace(" ", "T")
    return since[:19] <= ts <= until[:19]


def _is_session_lost(error_class: str, error_text: str) -> bool:
    """A resume that hit the restart-lost refusal. error_class is often re-classified
    to 'fatal', so the verbatim worker text is the reliable signal."""
    if (error_class or "").strip() == "session_lost":
        return True
    t = (error_text or "").lower()
    return "cannot be resumed by the continuous driver" in t or "session was lost after a worker restart" in t


def _grep(lines, *needles) -> list:
    out = []
    for ln in lines:
        if all(nd in ln for nd in needles):
            out.append(ln)
    return out


# ───────────────────────────── per-step checks ──────────────────────────────
def run_checks(node: str, since: str, until: str) -> list:
    conn = _ro_conn()
    logs = list(_log_lines(since, until))
    results = []

    def add(step: Step, status: str, detail: str):
        results.append({"n": step.n, "name": step.name, "status": status, "detail": detail,
                        "diagnosis": step.diagnosis_if_absent if status == FAIL else ""})

    # Step 0 — baseline: a wait was armed on this node's Case(s) in-window
    waitp = conn.execute(
        "SELECT COUNT(*) FROM flow_events WHERE event_type='worker.wait_pending' AND created_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]
    add(FROZEN_STEPS[0], PASS if waitp else UNKNOWN,
        f"worker.wait_pending events in window: {waitp}")

    # Step 1 — restart + O7 early warning
    mem = _grep(logs, "event=worker_memory_pressure", node)
    add(FROZEN_STEPS[1], PASS if mem else UNKNOWN,
        f"worker_memory_pressure lines: {len(mem)}"
        + (" (no OOM early-warning trail — O7 off or worker on old code)" if not mem else ""))

    # Step 2 — mark sessions lost on re-register
    marked_log = _grep(logs, "event=driver_sessions_marked_lost", f"node_id={node}")
    lost_sessions = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE machine_id=? AND driver_status='lost' AND updated_at BETWEEN ? AND ?",
        (node, since, until)).fetchone()[0]
    if marked_log or lost_sessions:
        add(FROZEN_STEPS[2], PASS, f"driver_sessions_marked_lost log lines: {len(marked_log)}; sessions flipped→lost in window: {lost_sessions}")
    else:
        add(FROZEN_STEPS[2], FAIL, "no driver_sessions_marked_lost and no session flipped to 'lost' in window")

    # Step 3 — detect + page
    detect = _grep(logs, "event=node_restart_detected", f"node_id={node}")
    notif = _grep(logs, "event=node_restart_notification")
    if detect:
        add(FROZEN_STEPS[3], PASS, f"node_restart_detected: {len(detect)}; notification emits: {len(notif)}")
    else:
        add(FROZEN_STEPS[3], FAIL, "no node_restart_detected event (operator not paged for this restart)")

    # Step 4 — Manager wake routes to create_session (fork), NOT resume_session/session_lost
    mgr_rows = conn.execute(
        """SELECT t.action, t.status, COALESCE(t.error_class,''), COALESCE(t.error,'') FROM mesh_tasks t
           JOIN sessions s ON s.session_id=t.session_id
           WHERE s.machine_id=? AND s.case_role='manager' AND t.created_at BETWEEN ? AND ?
           ORDER BY t.created_at""", (node, since, until)).fetchall()
    forks = [r for r in mgr_rows if r[0] == "create_session"]
    # corpse = a resume into a restart-lost session (error_class is often re-classified
    # to 'fatal', so detect by the worker's verbatim refusal text, not the class).
    corpse = [r for r in mgr_rows if r[0] == "resume_session" and _is_session_lost(r[2], r[3])]
    if corpse and not forks:
        add(FROZEN_STEPS[4], FAIL, f"Manager resume_session→session_lost x{len(corpse)} and 0 create_session forks (INCIDENT behavior)")
    elif forks:
        add(FROZEN_STEPS[4], PASS, f"Manager create_session forks: {len(forks)}; resume→session_lost: {len(corpse)}")
    else:
        add(FROZEN_STEPS[4], UNKNOWN, f"no manager turns on node in window (rows={len(mgr_rows)})")

    # Step 5 — respawn / approval for ERROR+lost Managers
    respawned = conn.execute(
        "SELECT COUNT(*) FROM flow_events WHERE event_type='case.manager_respawned' AND created_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]
    try:
        appr = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE action='case_manager_respawn' AND created_at BETWEEN ? AND ?",
            (since, until)).fetchone()[0]
    except Exception:
        appr = 0
    err_lost = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE machine_id=? AND case_role='manager' AND status='error' AND driver_status='lost'",
        (node,)).fetchone()[0]
    if respawned or appr:
        add(FROZEN_STEPS[5], PASS, f"case.manager_respawned: {respawned}; respawn approvals: {appr}")
    elif err_lost:
        add(FROZEN_STEPS[5], FAIL, f"{err_lost} Manager session(s) ERROR+driver_lost with NO respawn/approval (stalled)")
    else:
        add(FROZEN_STEPS[5], UNKNOWN, "no ERROR+driver_lost Manager; O2 net not exercised")

    # Step 6 — worker session re-established
    wrk_rows = conn.execute(
        """SELECT t.action, COALESCE(t.error_class,''), COALESCE(t.error,'') FROM mesh_tasks t
           JOIN sessions s ON s.session_id=t.session_id
           WHERE s.machine_id=? AND s.case_role='worker' AND t.created_at BETWEEN ? AND ?""",
        (node, since, until)).fetchall()
    w_corpse = [r for r in wrk_rows if r[0] == "resume_session" and _is_session_lost(r[1], r[2])]
    w_fork = [r for r in wrk_rows if r[0] == "create_session"]
    if w_corpse and not w_fork:
        add(FROZEN_STEPS[6], FAIL, f"worker resume→session_lost x{len(w_corpse)}, no forks")
    elif w_fork or wrk_rows:
        add(FROZEN_STEPS[6], PASS, f"worker forks: {len(w_fork)}; resume→session_lost: {len(w_corpse)}; total turns: {len(wrk_rows)}")
    else:
        add(FROZEN_STEPS[6], UNKNOWN, "no worker turns on node in window")

    # Step 7 — final state
    stalled = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE machine_id=? AND case_role='manager' AND status='error' AND driver_status='lost'",
        (node,)).fetchone()[0]
    if stalled and not (respawned or appr):
        add(FROZEN_STEPS[7], FAIL, f"{stalled} Manager(s) still ERROR+lost with no respawn — incident-shaped end state")
    else:
        add(FROZEN_STEPS[7], PASS, "no stranded ERROR+lost Manager without respawn")

    conn.close()
    return results


def print_spec():
    print("# FROZEN EXPECTED BEHAVIOR — worker restart graceful reconcile (A98)\n")
    for s in FROZEN_STEPS:
        print(f"## Step {s.n}: {s.name}")
        print(f"  trigger : {s.trigger}")
        print(f"  action  : {s.action_unit}")
        print(f"  effect  : {s.effect}")
        print(f"  page    : {s.page}")
        print(f"  evidence: {s.evidence}")
        print(f"  if-absent (pinpoint): {s.diagnosis_if_absent}\n")


def main():
    ap = argparse.ArgumentParser(description="A98 worker-restart conformance checker")
    ap.add_argument("--print-spec", action="store_true", help="print the frozen expected-behavior contract and exit")
    ap.add_argument("--node", help="node_id that restarted (e.g. Horse)")
    ap.add_argument("--since", help="ISO window start (default: now-6h)")
    ap.add_argument("--until", help="ISO window end (default: now)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.print_spec or not args.node:
        print_spec()
        if not args.node:
            print("(pass --node <id> [--since ISO] [--until ISO] to evaluate a real incident)")
        return 0

    now = _dt.datetime.now(_dt.timezone.utc)
    until = args.until or now.strftime("%Y-%m-%dT%H:%M:%S")
    since = args.since or (now - _dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")

    results = run_checks(args.node, since, until)
    first_fail = next((r for r in results if r["status"] == FAIL), None)

    if args.json:
        print(json.dumps({"node": args.node, "since": since, "until": until,
                          "results": results, "first_divergence": first_fail}, indent=2))
        return 0

    print(f"\nA98 worker-restart conformance — node={args.node}  window=[{since} .. {until}]\n")
    for r in results:
        mark = {"PASS": "✅", "FAIL": "❌", "UNKNOWN": "❓", "INFO": "·"}.get(r["status"], "?")
        print(f"  {mark} step {r['n']:<1} {r['name']}: {r['detail']}")
        if r["status"] == FAIL and r["diagnosis"]:
            print(f"        ↳ pinpoint: {r['diagnosis']}")
    print()
    if first_fail:
        print(f"VERDICT: FIRST DIVERGENCE at step {first_fail['n']} ({first_fail['name']}).")
        print(f"         {first_fail['detail']}")
        print(f"         pinpoint: {first_fail['diagnosis']}")
        return 2
    print("VERDICT: conformant — recovery followed the frozen expected chain (or scenario not exercised).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
