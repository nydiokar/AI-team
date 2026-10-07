#!/usr/bin/env python3
"""A84 carry (o) + TASK 6 — ISOLATED LIVE e2e on a FREE backend.

Proves the FULL completion-outbox path end to end, the way the A82 INT-tests ran
(real task-server app + real MeshDB + real WorkerAgent + a REAL backend driver),
WITHOUT touching the prod gateway or the prod flag. Everything here is a throwaway
temp DB and a process-local flag.

Two legs:
  LEG 1 (real worker completion → outbox → ONE Manager wake):
    A real managed worker turn is dispatched under an outbox-mode Case and driven
    through the REAL opencode-server backend running the FREE model
    `opencode/big-pickle`. The real `complete_turn` writes the durable
    completion_outbox row in the terminal txn; the real Wake-Dispatcher
    (`_continue_case_once`) delivers EXACTLY ONE coalesced wake — not via the
    legacy wait-group.

  LEG 2 (lost-carrier reaper backstop + late-result fence):
    A managed worker child whose carrier is provably gone/stale (real DB claim
    state left by a vanished carrier) is reaped by the REAL reaper
    (`_reap_lost_carriers` → `list_stale_managed_children` →
    `synthesize_managed_terminal`): a terminal is synthesized through the SAME
    atomic outbox seam, the Manager is woken once, and a LATE real result is
    fenced (idempotent replay, no second row, no second wake).

Backend used: opencode-server / opencode/big-pickle (FREE). Measured cost ≈ $0.
Run:  .venv/bin/python scripts/a84_outbox_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Isolate the environment BEFORE importing any app module.                     #
# --------------------------------------------------------------------------- #
_TMP = Path(tempfile.mkdtemp(prefix="a84_e2e_"))
_REPO = _TMP / "repo"
_REPO.mkdir(parents=True, exist_ok=True)
# Captured, override-proof DB path: config/settings.py may `load_dotenv(override=True)`
# when AI_TEAM_ENV_FILE is set and clobber os.environ["MESH_DB_PATH"]. We open MeshDB
# from THIS variable, so the harness is always on its own fresh temp DB.
_DB_FILE = str(_TMP / "mesh.db")
os.environ["MESH_ENABLED"] = "false"
os.environ["MESH_DB_PATH"] = _DB_FILE
os.environ.pop("AI_TEAM_ENV_FILE", None)  # keep .env from overriding our isolation
os.environ["CASE_COMPLETION_OUTBOX_ENABLED"] = "1"
os.environ["CASE_CONTINUATION_ENABLED"] = "1"
os.environ["MANAGER_ROLE_ENABLED"] = "1"
os.environ["HARNESS_FLOW_DRIVE"] = "1"
os.environ["CASE_RESPAWN_REQUIRES_APPROVAL"] = "0"
os.environ["AI_TEAM_ALLOW_OPENCODE_E2E"] = "1"  # permit the FREE backend under the cost guard
os.environ.pop("AI_TEAM_TEST_MODE", None)
os.environ.setdefault("OPENCODE_DEFAULT_MODEL", "opencode/big-pickle")

# Make the repo a git repo (opencode serve prefers one).
os.system(f"cd {_REPO} && git init -q && git config user.email e2e@local && "
          f"git config user.name e2e && echo '# a84 e2e' > README.md && "
          f"git add -A && git commit -q -m init")

import src.control.db as db_mod  # noqa: E402
import src.control.node_registry as nr_mod  # noqa: E402
import src.control.task_server as ts  # noqa: E402
import src.worker.agent as agent_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.backends.opencode import OpenCodeServerBackend  # noqa: E402
from src.control.db import MeshDB, continuation_task_id  # noqa: E402
from src.core.interfaces import Session, SessionStatus  # noqa: E402
from src.orchestrator import TaskOrchestrator  # noqa: E402
from src.worker.agent import WorkerAgent  # noqa: E402
from src.worker.managed_result_spool import ManagedClaimStore, ManagedResultSpool  # noqa: E402

from tests.test_case_continuation import _FakeSession, _FakeStore, _continue  # noqa: E402
from tests.test_completion_outbox_drain import _FakeOrch  # noqa: E402
from tests.test_turn_queue_carrier_integration import _ClientHTTP  # noqa: E402

NODE = socket.gethostname()
# Must match the TOKEN baked into the imported tests._ClientHTTP (it stamps the
# Authorization header from its own module global).
TOKEN = "tok"


class _DrainOrch(_FakeOrch):
    """The drain harness, extended with the late-manager-binding seam. Unlike the
    unit drain tests (no enrolled session), here a REAL worker session is enrolled,
    so ``_continue_case_once`` exercises the ``any_session_enrolled`` branch that
    calls ``_withdraw_rebound_continuation`` — delegate it to the real method."""

    async def _withdraw_rebound_continuation(self, db, case_id, generation, manager_sid):
        return await TaskOrchestrator._withdraw_rebound_continuation(
            self, db, case_id, generation, manager_sid)

    async def _reconcile_continuation_finalizers(self, db):
        return await TaskOrchestrator._reconcile_continuation_finalizers(self, db)


_LOG: List[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.now(tz=timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    _LOG.append(line)


def _mk_db() -> MeshDB:
    mdb = MeshDB(_DB_FILE)
    ts.get_db = lambda: mdb  # type: ignore[assignment]
    db_mod.get_db = lambda: mdb  # type: ignore[assignment]
    nr_mod._registry = nr_mod.NodeRegistry()
    ts._worker_token = lambda: TOKEN  # type: ignore[assignment]
    return mdb


def _mk_worker(http: _ClientHTTP) -> WorkerAgent:
    w = WorkerAgent.__new__(WorkerAgent)
    w.cfg = SimpleNamespace(
        node_id=NODE, backends=["opencode-server"], max_concurrent=2,
        accept_unpinned=True, managed_turns=True, tailscale_ip="127.0.0.1",
        api_port=0, projects_root="", controller_url="http://test",
        list_repos=lambda: [],
    )
    w._http = http
    w._incarnation_id = "inc-e2e"
    w._active, w._active_meta = {}, {}
    w._slots_used = 0
    w._inflight_sessions = set()
    w._semaphore = asyncio.Semaphore(2)
    w._codex_control_semaphore = asyncio.Semaphore(1)
    w._heartbeat_now, w._poll_now, w._shutdown = (
        asyncio.Event(), asyncio.Event(), asyncio.Event())
    w._backends = {"opencode-server": OpenCodeServerBackend()}
    w._telemetry_sink, w._canary = None, True
    w._model_capabilities = {}
    w._result_spool = ManagedResultSpool(str(_TMP / "carrier_state"))
    w._pending_result_delivery = set()
    w._managed_claims = {}
    w._claim_store = ManagedClaimStore(str(_TMP / "carrier_state"))
    w._start_retry_delays = (0.0, 0.0, 0.0)
    w._result_delivery_semaphore = asyncio.Semaphore(2)
    w._delivering = set()
    w._delivery_parked = set()
    w._held_probe_at = {}
    w._held_probe_interval_sec = 0.0
    w._managed_claims_blocked = None
    return w


def _register(http: _ClientHTTP) -> None:
    r = http.client.post("/nodes/register", json={
        "node_id": NODE, "tailscale_ip": "127.0.0.1", "api_port": 0,
        "incarnation_id": "inc-e2e",
        "capabilities": {"backends": ["opencode-server"], "queue_protocols": [0, 1],
                         "managed_backends": ["opencode-server"]},
    }, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text


def _seed_worker_turn(db: MeshDB, task_id: str, session_id: str, case_id: str,
                      prompt: str, *, action: str = "create_session") -> None:
    """Seed an outbox-mode Case worker CHILD turn (flow_run_id + task flow-link),
    enrolled and active, exactly as a real Manager dispatch would leave it. The
    payload carries the full session envelope the carrier reconstructs to drive
    the backend (``_make_session_from_payload``)."""
    db.upsert_session(Session(
        session_id=session_id, backend="opencode-server", repo_path=str(_REPO),
        status=SessionStatus.IDLE, created_at=db_mod._now(),
        updated_at=db_mod._now(), machine_id=NODE,
    ))
    db.enroll_session(session_id)
    db.enqueue_turn(
        task_id=task_id, session_id=session_id, backend="opencode-server",
        action=action,
        payload={
            "task_id": task_id, "prompt": prompt,
            "session": {
                "session_id": session_id, "backend": "opencode-server",
                "repo_path": str(_REPO), "machine_id": NODE,
                "model": "opencode/big-pickle", "last_user_message": prompt,
            },
        },
        turn_source="human", turn_kind="instruction", machine_id=NODE,
        flow_run_id=case_id,
    )
    db.create_flow_link(case_id, "task", task_id, "task", created_by="manager")
    db.activate_turn(task_id)


def _outbox(db: MeshDB, case_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM completion_outbox WHERE case_id=? ORDER BY child_task_id",
        (case_id,)).fetchall()]


def _task_row(db: MeshDB, task_id: str) -> Dict[str, Any]:
    return dict(db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE id=?", (task_id,)).fetchone())


def _reap(orch: Any, db: MeshDB) -> int:
    return asyncio.run(TaskOrchestrator._reap_lost_carriers(orch, db))


# --------------------------------------------------------------------------- #
# LEG 1 — real worker turn → outbox → one Manager wake                         #
# --------------------------------------------------------------------------- #
def leg1_real_completion(db: MeshDB, http: _ClientHTTP) -> str:
    log("LEG 1: real opencode/big-pickle worker turn under an outbox-mode Case")
    case_id = db.open_case("ship the feature", "mgr-sess", role="manager",
                           completion_criteria="worker finished")
    assert db.case_continuation_mode(case_id) == "outbox", "Case not born outbox-mode"
    log(f"  opened outbox Case {case_id} (continuation_mode=outbox)")

    w = _mk_worker(http)
    _register(http)
    _seed_worker_turn(
        db, "w-real", "wrk-real", case_id,
        prompt=("Reply with exactly the single token: PICKLE_OK . "
                "Do not create files. Keep the reply to one line."),
    )
    log("  dispatched worker child w-real; driving the REAL carrier + backend ...")

    t0 = time.time()

    async def scenario() -> None:
        rows = await w._fetch_pending()
        managed = [r for r in rows if r["id"] == "w-real"]
        assert managed and managed[0]["queue_protocol"] == 1, "turn not managed"
        await w._handle_task(managed[0])

    asyncio.run(scenario())
    dt = time.time() - t0
    row = _task_row(db, "w-real")
    log(f"  carrier returned in {dt:.1f}s: task status={row['status']} "
        f"backend_session_id={(row.get('result') and json.loads(row['result']).get('backend_session_id'))!r}")
    assert row["status"] == "completed", f"expected completed, got {row['status']} / {row.get('error')}"
    reply = json.loads(row["result"]).get("output", "")
    log(f"  real big-pickle reply: {reply!r}")

    # The durable outbox row was written in the SAME terminal txn by complete_turn.
    rows = _outbox(db, case_id)
    assert len(rows) == 1 and rows[0]["child_task_id"] == "w-real", f"outbox={rows}"
    assert rows[0]["outcome"] == "success" and rows[0]["delivered_at"] is None
    log(f"  completion_outbox: 1 row (child=w-real, outcome=success, undelivered) ✓")

    # The REAL Wake-Dispatcher drains it as ONE wake (NOT a wait-group).
    assert db._conn().execute(
        "SELECT COUNT(*) FROM flow_events WHERE flow_run_id=? AND event_type LIKE 'worker.wait%'",
        (case_id,)).fetchone()[0] == 0, "a wait-group was armed — should be outbox path"
    drain = _DrainOrch(_FakeStore(_FakeSession("mgr-sess")))
    delivered = _continue(drain, db, case_id)
    assert delivered == 1 and len(drain.deliveries) == 1, f"deliveries={drain.deliveries}"
    assert "w-real" in drain.deliveries[0]["description"]
    assert drain.deliveries[0]["session_id"] == "mgr-sess"
    log("  Wake-Dispatcher delivered EXACTLY ONE coalesced wake presenting w-real ✓")

    # ACK (crash-safe consumption) marks the row delivered; re-tick does not re-wake.
    db.record_continuation_consumed(case_id, continuation_task_id(case_id, 1), 1, ["w-real"])
    assert db.pending_case_outbox(case_id) == []
    retick = _DrainOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(retick, db, case_id) == 0 and retick.deliveries == []
    log("  ACK marked delivered(reason=wake); re-tick is a no-op (exactly-once) ✓")
    return case_id


# --------------------------------------------------------------------------- #
# LEG 2 — lost-carrier reaper backstop + late-result fence                     #
# --------------------------------------------------------------------------- #
def leg2_lost_carrier(db: MeshDB, http: _ClientHTTP, case_id: str) -> None:
    log("LEG 2: lost-carrier reaper on the SAME real outbox Case")
    # A second worker child the carrier genuinely CLAIMS (real route, real token)
    # and then vanishes before reporting terminal.
    _seed_worker_turn(db, "w-lost", "wrk-lost", case_id,
                      prompt="(never executed — carrier dies)")
    claim = http.post("/tasks/w-lost/claim-managed", {
        "node_id": NODE, "incarnation_id": "inc-e2e", "carrier_kind": "worker_daemon",
    })
    token = claim.get("claim_token") or claim.get("token")
    assert token, f"claim-managed returned no token: {claim}"
    log(f"  carrier claimed w-lost (real token captured); now simulating carrier death")

    # The carrier is gone: age its claim past the lease and take the node offline,
    # exactly the DB state a crashed/OOM'd carrier leaves behind.
    stale = (datetime.now(tz=timezone.utc) - timedelta(seconds=400)).isoformat()
    conn = db._conn()
    conn.execute("UPDATE mesh_tasks SET claimed_at=?, status='running' WHERE id='w-lost'", (stale,))
    conn.execute("UPDATE nodes SET status='offline' WHERE node_id=?", (NODE,))
    conn.commit()
    stale_rows = db.list_stale_managed_children()
    assert any(r["id"] == "w-lost" for r in stale_rows), f"reaper did not detect w-lost: {stale_rows}"
    log(f"  list_stale_managed_children detected w-lost (reason="
        f"{[r['_stale_reason'] for r in stale_rows if r['id']=='w-lost'][0]}) ✓")

    # The REAL reaper synthesizes the terminal through the SAME atomic outbox seam.
    orch = _DrainOrch(_FakeStore(_FakeSession("mgr-sess")))
    reaped = _reap(orch, db)
    assert reaped == 1, f"reaper synthesized {reaped} (expected 1)"
    row = _task_row(db, "w-lost")
    assert row["status"] == "failed" and row["error_class"] == "carrier_lost"
    ob = [r for r in _outbox(db, case_id) if r["child_task_id"] == "w-lost"]
    assert len(ob) == 1 and ob[0]["outcome"] == "failed", f"outbox={ob}"
    log("  reaper synthesized terminal(failed, carrier_lost) + ONE outbox row ✓")

    # Bring the node back online so the drain's manager wake is not mistaken for dead.
    conn.execute("UPDATE nodes SET status='online' WHERE node_id=?", (NODE,))
    conn.commit()

    # The Manager is woken exactly once for the reaped child.
    drain = _DrainOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(drain, db, case_id) == 1 and len(drain.deliveries) == 1
    assert "w-lost" in drain.deliveries[0]["description"]
    log("  Wake-Dispatcher woke the Manager once for the reaped child ✓")
    db.record_continuation_consumed(case_id, continuation_task_id(case_id, 2), 2, ["w-lost"])
    assert db.pending_case_outbox(case_id) == []

    # FENCE: the vanished carrier's result arrives LATE with its real token.
    res = db.complete_turn("w-lost", token, {"output": "late real result"}, status="completed")
    assert res.status == "failed", f"late result not fenced: {res.status}"
    ob2 = [r for r in _outbox(db, case_id) if r["child_task_id"] == "w-lost"]
    assert len(ob2) == 1 and ob2[0]["outcome"] == "failed", "late result produced a 2nd/altered row"
    retick = _DrainOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(retick, db, case_id) == 0 and retick.deliveries == []
    log("  LATE real result FENCED: idempotent replay, no 2nd row, no re-wake ✓")


def main() -> int:
    log(f"isolated temp dir: {_TMP}")
    log(f"backend=opencode-server model=opencode/big-pickle (FREE)  node={NODE}")
    db = _mk_db()
    http = _ClientHTTP(TestClient(ts.app))
    try:
        case_id = leg1_real_completion(db, http)
        leg2_lost_carrier(db, http, case_id)
    except Exception:
        import traceback
        log("FAILED:\n" + traceback.format_exc())
        _write_evidence(ok=False)
        return 1
    log("ALL ASSERTIONS PASSED — outbox delivery + lost-carrier reaper proven live.")
    _write_evidence(ok=True)
    return 0


def _write_evidence(ok: bool) -> None:
    out = Path(__file__).resolve().parents[1] / ".ai" / "dispatch" / "_a84_e2e_raw.log"
    out.write_text("\n".join(_LOG) + f"\n\nRESULT={'PASS' if ok else 'FAIL'}\n")
    log(f"raw log written to {out}")


if __name__ == "__main__":
    sys.exit(main())
