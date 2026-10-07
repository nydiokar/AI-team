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

## Step status (update as you go)
- [x] S1 Reconcile PR #185 onto main (routes re-homed, flag default-on, CI green) — accepted.
- [x] S2 Merge PR #185 → main. main @ `60d5747`. Live image still `prod-621acd6` (18 behind).
- [ ] S3 Deploy Horse (new managed carrier #1). Verify: Horse re-registers new incarnation + git
      HEAD = main + `pip show psutil` present + advertises managed turns.
- [ ] S4 Bring up local bridge `kanebra-worker-canary` (new code) via
      `safe_worker_deploy.py --no-promote --keep-canary` AFTER `git pull`. Verify canary online.
- [ ] S5 Deploy gateway + task-server (deploying-the-gateway skill): backup → rebuild image from
      main → migration 43 → health. Set `MESH_LOCAL_CARRIER_NODE_ID` = `kanebra-worker-canary`
      (the bridge) so managed turns route to new code. Verify schema_version=43, coverage_ok true.
- [ ] S6 Retire old local worker: promote it onto new code (`safe_worker_deploy.py` promote, or
      pm2 restart ai-team-worker --update-env after git pull), then delete the canary. (This is the
      step that kills the original Manager session — expected. The continuing Manager owns it.)
- [ ] S7 LIVE VALIDATION (O5): dispatch a worker turn to a busy session; while running, enqueue a
      second message → assert 202 + distinct turn id + no BUSY clobber; when turn 1 finishes, the
      scheduler activates turn 2 and delivers it to the SAME session (resume, not create). This is
      the Stage 8a proof. See tests/test_turn_queue_stage8a.py for the expected end-to-end shape.
- [ ] S8 Close Case with continuation plan (Stage 8b: delete legacy execution code + its tests).

## If you are the continuing Manager
Re-read this file + `.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md` + git log. Run the
checking-live-state skill to see which steps already took effect (image sha, schema_version,
nodes online, which carriers advertise managed turns). Resume at the first unchecked step. Verify
everything against git and the running system — do not trust this doc over reality.
