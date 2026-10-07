# A82 Stage 8a — Deploy + Validation Burn-down (Case 4d8a46b52d8a48bfa965350aa9bf0a64)

**Purpose:** if the Manager session carrying this Case dies at the cutover, the continuing
(respawned) Manager resumes from HERE. This is the plan of record. Last updated 2026-10-07.

Manager session: `f078ce07f3d1`. Operator: donpicks@proton.me.
Operator standing order: run this **fully autonomously and continuously** to completion. Make the
calls yourself. Do NOT stop to ask unless something is genuinely irreversible AND no safe default
exists — and even then, state the default and proceed. The operator explicitly does not want to be
pulled back in for routine steps.

## Objective (it is NOT just a deploy)
1. Deploy merged A82 Stage 8a to the two machines (Horse + this host "kanebra").
2. PROVE Stage 8a works live (the actual gate to Stage 8b): enqueue a message to a BUSY session →
   accepted (202, distinct turn id, no BUSY clobber) → delivered FIFO after the running turn,
   resuming the SAME native session (not a new create_session).
3. Leave a stable, verified final version that Stage 8b can build on.

## Locked decisions (do not re-litigate)
- Continuity model = **bridge**: stand up a second local worker on new code, move the managed
  carrier pointer to it, continue the Case there, then retire the old local worker.
- `WORKER_MANAGED_TURNS` is now **default-ON in code** (PR #185 merged) — overridable only by
  explicit `0/false/no/off`. So no worker needs it in env.
- `_REFUSE_SESSION_TURNS_WITHOUT_MESH` stays **True** (prod shape; fail-closed). No change.
- Closed-session enrollment: forward/live is what matters; inert back-enroll by migration 43 is
  accepted as-is.

## Node identities (from /api/nodes, 2026-10-07)
- **kanebra-worker** — THIS host, tailscale 100.88.11.88, port 9001. Carries the Manager session.
  Runs OLD code until promoted. Native PM2 app `ai-team-worker`, cwd /home/cifran/dev/AI-team.
- **kanebra-worker-canary** — bridge slot, port 9101 (offline until we start it). New code.
- **Horse** — tailscale 100.112.245.29, port 9001, online. Safe to restart (operator-confirmed:
  "nothing waits there, this is my order"). Its node id is already set; needs new code + psutil.
- `MESH_LOCAL_CARRIER_NODE_ID` is a GATEWAY-side var (not read by workers). Set it on the gateway
  container to the node id that should carry managed turns. Needs gateway restart to take effect.

## Key facts / gotchas
- Gateway + task-server run in **Docker** (image `ai-team:local`); the worker is **native PM2**.
  Restarting the GATEWAY does NOT kill the worker/session. Restarting the WORKER marks every
  driver session it carries as `lost` (node_registry sweep keyed by node_id/incarnation). THAT is
  what kills this session.
- A worker that restarts its OWN carrier aborts its own task — run the restart **detached**
  (`setsid`/`nohup`) or use `safe_worker_deploy.py` which is built for it.
- `safe_worker_deploy.py`: `--no-promote --keep-canary` = start+verify canary, leave real worker
  running (the bridge). Plain run = canary then promote (restarts real worker). Canary node =
  `{real_node}-canary`, port = `WORKER_API_PORT + 100`.
- Deploy order is **workers-first**: once A82 is live in the gateway + migration 43 applied, every
  session turn 503s (`carrier_required`) until a managed-turns worker (new code) is registered.
- Migration 43 (schema 42 → 43): additive + data UPDATE (fails pending protocol-0 rows; enrolls
  sessions). **DB backup mandatory before applying** (deploy skill auto-backs-up). No down-migration;
  rollback is image-level and you must NEVER roll back to pre-A82 while protocol-1 rows exist.
- psutil is declared/pinned but may not be installed in a given worker venv — VERIFY on each box.

## LIVE FINDINGS (2026-10-07, verified from controller/containers)
- `safe_worker_deploy.py` is NOT buggy — `_run(env=env)` only passes env to the subprocess; it does
  not mutate os.environ, so `_promote --update-env` reads a clean env. The `canary=true` leak on
  Horse was the WORKER's own shell improvisation (exported WORKER_CANARY then ran a --update-env
  restart). Lesson for kanebra: do NOT hand-run `--update-env` from a shell that touched
  WORKER_CANARY. Use the clean relaunch `pm2 delete ai-team-worker; pm2 start ecosystem.config.js
  --only ai-team-worker --update-env` (proven to clear it on Horse).
- WORKER_CANARY ⇒ worker runs polling_disabled (src/worker/agent.py:3387) = online but inert.
- This Manager session (f078ce07f3d1) carrier = `machine_id=kanebra-worker` (OLD code). Migration 43
  enrolls it ⇒ after the gateway cutover its turns route to the managed queue which kanebra-worker
  (old, non-managed) won't claim ⇒ THIS SESSION LIKELY FREEZES at cutover. Bash tool calls run
  locally in the worker, so within a single turn after cutover bash still works; the NEXT-turn
  re-entry may not arrive.
- Gateway container currently has MESH_LOCAL_CARRIER_NODE_ID UNSET (only MESH_ENABLED=true). Must be
  set to an ONLINE MANAGED carrier at recreate. Only Horse qualifies now.
- Image sqlite = 3.40.1 (>=3.35, RETURNING OK). No blocker.

## Execution model chosen
Drive cutover + validation from BASH within turns (local execution; does not need my own turn
routing), doing the destructive kanebra-worker restart DEAD LAST. If turns freeze after cutover,
continuation/operator finishes from the first unchecked step. Dropped the kanebra bridge (S4) — it
would not save this session (driver lives on kanebra-worker) and adds a canary on the session-critical
host. Horse suffices as the managed carrier for cutover.

## Step status (update as you go)
- [x] S1 Reconcile PR #185 onto main (routes re-homed, flag default-on, CI green) — accepted.
- [x] S2 Merge PR #185 → main. main @ `60d5747` (burndown commit on top).
- [x] S3 Deploy Horse (managed carrier #1). VERIFIED: Horse online, canary=false, slots=4,
      managed_backends=[claude,codex,opencode-server], inc 7b3599, new code. DONE.
- [~] S4 local bridge — DROPPED (see Execution model).
- [ ] S5 Deploy gateway + task-server (deploying-the-gateway skill): backup DB → rebuild ai-team:local
      from main → recreate gateway+task-server → migration 43 → verify schema_version=43 + health.
      Ensure MESH_LOCAL_CARRIER_NODE_ID=Horse effective in the gateway container (write .env add-only
      if missing, then recreate). Verify /api/turn-queue/coverage coverage_ok.
- [ ] S6 LIVE VALIDATION (O5) via API/curl against a Horse-carried session: start a turn; while BUSY
      enqueue a 2nd message → assert 202 + distinct turn id + no BUSY clobber; when turn 1 finishes,
      scheduler activates turn 2 delivered to the SAME session (resume, not create). Record evidence
      here. Done via API so it does not depend on this Manager's own turn routing.
- [ ] S7 Upgrade kanebra-worker to new code DEAD LAST: clean relaunch `pm2 delete ai-team-worker;
      pm2 start ecosystem.config.js --only ai-team-worker --update-env` (clears WORKER_CANARY; proven
      on Horse). THIS KILLS THIS MANAGER SESSION — expected. Run detached if issued from a
      kanebra-carried agent. Verify kanebra-worker returns canary=false + managed_backends populated.
      Optionally repoint MESH_LOCAL_CARRIER_NODE_ID=kanebra-worker + recreate gateway.
- [ ] S8 Close Case with continuation plan (Stage 8b: delete legacy execution code + its tests).

## If you are the continuing Manager
Re-read this file + `.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md` + git log. Run the
checking-live-state skill to see which steps already took effect (image sha, schema_version,
nodes online, which carriers advertise managed turns). Resume at the first unchecked step. Verify
everything against git and the running system — do not trust this doc over reality.
