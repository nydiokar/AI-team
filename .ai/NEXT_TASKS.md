# Next Tasks

**Current priority:** Phase 9 — Agent mesh worker + task server.
Tasks 1–3 are **complete, adversarially reviewed, bug-fixed, and smoke-tested**.
`scripts/test_mesh_local.py` passes 18/18 checks against the real FastAPI app + MeshDB.

---

## ✅ 1–3. Task server, worker daemon, orchestrator routing — DONE & TESTED

Files created:
- `src/control/task_server.py` — FastAPI app, all 9 endpoints, Bearer auth, MeshDB-backed
- `src/control/node_registry.py` — in-memory NodeRegistry + DB persistence + expiry loop
- `src/worker/__init__.py`, `src/worker/config.py`, `src/worker/agent.py` — full daemon
- `src/orchestrator.py` — `_run_backend_local`, `_dispatch_to_node`, `_dispatch_or_run_local`
- `ecosystem.config.js` — both PM2 entries added (disabled by default)
- `scripts/test_mesh_local.py` — automated smoke test (run with `python scripts/test_mesh_local.py`)

**14 issues found in adversarial review; the 9 critical/high ones are fixed** (see CONTEXT.md
Phase 9 section for the full list). Key fix: `_mesh_enqueue_task` now self-claims its own
shadow-written rows immediately, so a running worker can never double-execute a task that
the gateway is already running locally. `_dispatch_or_run_local` exists and is correct, but
is intentionally **not** wired into `process_task` yet (see "What's NOT done" below).

A pre-existing DB migration bug was also found and fixed: fresh `MeshDB` instances failed
to initialize (`cannot commit - no transaction is active`). The live `state/mesh.db` was
unaffected (already at schema v1) but any new deployment would have hit this.

---

## What's NOT done — and why that's the right call for now

`_dispatch_or_run_local` is a fully-working router, but it is **not called** from
`process_task`. Wiring it in for real would mean either:
(a) duplicating `process_task`'s retry/timeout/heartbeat machinery inside the
    remote-dispatch path, or
(b) a larger refactor that extracts that machinery so both paths share it.

Either is a real piece of work and higher-risk than what's been done so far. Given that
`MESH_ENABLED=false` is the default and the goal right now is a **safe, reversible trial**,
it's better to land this as a separate, focused change once you've validated the
server+worker mechanics in isolation (see the trial plan below).

---

## Recommended rollout — safe trial sequence

**Step A — Run server + worker in shadow mode (today, zero risk to the live gateway)**

This validates the *mechanics* (registration, heartbeat, polling, claiming, result
posting) using the smoke-test script, which already does this in-process. No live
processes need to run side-by-side with the gateway for this — `test_mesh_local.py`
covers the full server-side cycle.

If you want to see a *live* worker process run too:
```bash
# Terminal 1 — task server (separate port, separate DB to avoid touching live state)
WORKER_TOKEN=<token> MESH_DB_PATH=state/mesh_trial.db \
uvicorn src.control.task_server:app --host 127.0.0.1 --port 9099

# Terminal 2 — worker daemon pointed at the trial server
WORKER_NODE_ID=trial-node WORKER_TOKEN=<token> WORKER_TAILSCALE_IP=127.0.0.1 \
WORKER_API_PORT=9098 CONTROLLER_URL=http://127.0.0.1:9099 WORKER_BACKENDS=claude \
python -m src.worker.agent
```
Watch it register, heartbeat every 30s, and poll `/tasks/pending` (empty — nothing
enqueues to `mesh_trial.db`). This proves the daemon lifecycle works without touching
production state or risking duplicate execution. Stop with Ctrl+C — confirm clean
deregistration in the logs.

**Step B — Wire `_dispatch_or_run_local` into `process_task` (separate task)**

Once Step A's mechanics are confirmed solid, do the actual routing integration as its
own change — ideally on a feature branch / different machine, tested against a
non-critical repo, with `MESH_ENABLED=true` only for that one trial session
(`session.machine_id` pins it to a specific node).

**Step C — Real two-machine test**

Run the worker on a second device. Point `CONTROLLER_URL` at the main PC's task
server (Tailscale IP once enrolled, or LAN IP for an initial same-network test).
Send a Telegram message to a session pinned to that node's `machine_id` and watch
it route through DB → worker → result → Telegram.

---

## Tailscale prerequisite (your action, not code)

Before deploying to VPS:
- confirm VPS and main PC are enrolled in Tailscale
- record both Tailscale IPs
- set ACL: VPS port 9002 reachable from PC; PC port 9001 reachable from VPS
- generate `WORKER_TOKEN`: `openssl rand -hex 32`
- test connectivity: `curl http://{vps-tailscale-ip}:9002/health` from PC — should get connection refused (not timeout)

---

## VPS migration (after the rollout plan steps above are validated)

Per `docs/AGENT_MESH_SPEC.md` Phase 4:
- clone repo to VPS
- run `scripts/seed_db_from_json.py` on VPS after copying `state/` across
- run `scripts/fix_session_machine_ids.py` (needs to be written) — updates existing session `machine_id` values to match the PC's `WORKER_NODE_ID`
- start gateway on VPS, worker daemon on PC
- test end-to-end Telegram → VPS → PC worker → result → Telegram
- stop gateway on PC

---

## Deferred (from previous backlog — still valid but lower priority)

- Codex end-to-end validation (two-turn session test)
- Telegram command polish (compact replies, `/commit_all` decision)
- Backend CLI version pinning / smoke checks at startup
- Legacy `.task.md` watcher cleanup decision
