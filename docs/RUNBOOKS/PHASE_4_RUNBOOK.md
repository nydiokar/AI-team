# Phase 4 Runbook — Migrate the Gateway to the VPS

Goal: move the always-on control plane (orchestrator + Telegram bot + embedded
mesh task server) from **this PC** to the **VPS**, and turn this PC into a
**worker node**. When done: VPS runs `ai-team-gateway`, this PC runs
`ai-team-worker`, and Telegram tasks route over Tailscale to the worker.

This is an **operational** push, not a coding task. Do it when you can babysit
it. Every destructive/irreversible step is called out, and §7 is the rollback.

> Terminology: in this repo "controller" = the gateway with `MESH_ENABLED=true`
> running the embedded task server (`src/control/embedded_server.py`). There is
> no separate task-server process anymore (that was removed in D1).

---

## 0. Pre-flight (do this on THIS PC, before touching the VPS)

- [ ] **Tailscale is up on both machines** and they can see each other:
  - `tailscale status` (note the VPS Tailscale IP — call it `VPS_TS_IP`,
    and this PC's — call it `PC_TS_IP`; both look like `100.x.y.z`)
  - From this PC: `ping <VPS_TS_IP>` succeeds.
- [ ] **Pick a stable worker node id** for this PC, e.g. `main-pc`. Write it
  down — it must stay constant or session affinity breaks.
- [ ] **Gateway is healthy right now**: `python main.py health` → `OK`.
- [ ] **You know the WORKER_TOKEN** (shared secret; same value on VPS + PC).
- [ ] Working tree is clean / committed (`git status`).

### 0a. Fix the two `.env` mesh bugs (flagged in D1 — verify before enabling)

These block `MESH_ENABLED=true`. Check this PC's `.env`:

- `MESH_TAILSCALE_IP` must be `PC_TS_IP` for now (on the VPS later it becomes
  `VPS_TS_IP`). It was previously a literal comment string — make it a real IP.
- `MESH_TASK_SERVER_PORT` — confirm it's the port you want the embedded server
  to listen on (default `9002`). The worker's `CONTROLLER_URL` must point here.

---

## 1. Retag existing sessions (RUN ON THIS PC — irreversible-ish, back up first)

Existing sessions have `machine_id = <this PC's hostname>`. After migration the
gateway lives on the VPS, so those sessions must point at the **worker node id**
that now owns their backend state (this PC).

```bash
# BACK UP FIRST (cheap insurance — lets you undo §1 instantly):
cp -r state/sessions state/sessions.bak

# Dry run — shows exactly what changes, writes nothing:
python scripts/fix_session_machine_ids.py --node-id main-pc

# If the dry run looks right, apply:
python scripts/fix_session_machine_ids.py --node-id main-pc --apply
```

The script is idempotent and only rewrites sessions whose `machine_id` matches
this PC's hostname. Re-running is a no-op. `--node-id` must equal the
`WORKER_NODE_ID` you'll use for this PC's worker (`main-pc`).

> Rollback for this step: `rm -rf state/sessions && mv state/sessions.bak state/sessions`

---

## 2. Stand up the repo on the VPS

```bash
# On the VPS:
git clone <your repo url> ai-team && cd ai-team
git checkout main            # or the release branch you deploy from
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Copy state from this PC to the VPS (sessions, mesh db, results as desired). Use
the Tailscale IP so it goes over the tailnet:

```bash
# From THIS PC (after §1 apply), to the VPS:
scp -r state/sessions  user@<VPS_TS_IP>:~/ai-team/state/sessions
scp    state/mesh.db   user@<VPS_TS_IP>:~/ai-team/state/mesh.db   # if present
# results/ and summaries/ are optional history — copy if you want them on the VPS.
```

---

## 3. Configure `.env` on the VPS (controller role)

The VPS `.env` is the same as this PC's gateway `.env` **except**:

| Key | VPS value |
|-----|-----------|
| `MESH_ENABLED` | `true` |
| `MESH_TAILSCALE_IP` | `VPS_TS_IP` (the VPS's own tailnet IP) |
| `MESH_TASK_SERVER_PORT` | `9002` (or your chosen port) |
| `MESH_DB_PATH` | `state/mesh.db` |
| `WORKER_TOKEN` | same shared secret as the PC worker |
| `GATEWAY_TELEGRAM_*` | same bot token / allowed users / chat id as before |
| `CLAUDE_BASE_CWD` / `CLAUDE_ALLOWED_ROOT` | VPS paths (the VPS may have no repos — that's fine; work routes to the worker) |

Sanity check on the VPS before starting anything live:

```bash
python main.py doctor     # effective config + CLI availability
python main.py health     # expect OK (telegram + a backend present)
```

---

## 4. Cutover (the sharp edge — do these in order, fast)

> ⚠️ This is the only window where Telegram is briefly unowned. Keep it short.

1. **Stop the gateway on THIS PC** (so two gateways never poll Telegram at once):
   ```bash
   pm2 stop ai-team-gateway       # on THIS PC
   ```
2. **Start the gateway on the VPS**:
   ```bash
   pm2 start ecosystem.config.js --only ai-team-gateway --update-env   # on VPS
   pm2 logs ai-team-gateway        # watch: embedded task server should bind :9002
   ```
   Look for the embedded server starting (mesh enabled) and the Telegram bot
   coming online. Send `/status` (or any message) in Telegram — the VPS should
   answer now.
3. **Start the worker on THIS PC** (D6 made this entry bootable). Required env
   in this PC's `.env`:
   ```
   WORKER_NODE_ID=main-pc
   WORKER_TOKEN=<same shared secret>
   WORKER_TAILSCALE_IP=<PC_TS_IP>
   CONTROLLER_URL=http://<VPS_TS_IP>:9002
   WORKER_BACKENDS=claude,opencode
   WORKER_PROJECTS_ROOT=C:/Users/Cicada38/Projects   # optional, enables repo discovery
   ```
   First start:
   ```bash
   pm2 start ecosystem.config.js --only ai-team-worker --update-env    # on THIS PC
   pm2 logs ai-team-worker
   ```
   Expect, in order: `event=registered ... controller=http://<VPS_TS_IP>:9002`,
   then `event=nudge_listener_started`, then periodic `heartbeat`.

   Future worker updates must use the canary deploy script, not a raw restart:
   ```bash
   python scripts/safe_worker_deploy.py
   ```
   The script starts `ai-team-worker-canary` with `WORKER_CANARY=true`, waits for
   the controller to see that canary heartbeat, and only then restarts the real
   `ai-team-worker`. If canary startup or heartbeat fails, the existing worker
   stays untouched and the canary is removed.

---

## 5. Verify the round-trip

- [ ] **Worker registered**: VPS gateway logs show the node; `/nodes` in
      Telegram (if present) lists `main-pc`.
- [ ] **New task routes to the worker**: send a fresh coding task in Telegram.
      Watch this PC's worker log for `task_claimed` → `task_result_posted
      success=true`, and confirm the result lands back in Telegram.
- [ ] **Resume works**: continue an existing (migrated) session. It must run on
      `main-pc` (affinity). If the worker is up, it should claim it.
- [ ] **Offline behaviour**: `pm2 stop ai-team-worker` on the PC, send a resume
      for a `main-pc` session → expect a fast "node offline" failure, not a hang.
      Restart the worker afterwards.

---

## 6. Make it durable (only after §5 is green)

On the VPS:
```bash
pm2 save
pm2 startup        # run the printed command, then `pm2 save` again
```
On THIS PC (worker):
```bash
pm2 save           # so the worker comes back on reboot
```
Then update memory / `docs/PROGRESS_LOG.md`: Phase 4 done, controller = VPS,
worker = main-pc.

---

## 7. Rollback (if anything in §4–§5 goes wrong)

The migration is reversible because the PC gateway is only **stopped**, not
deleted, and sessions were backed up in §1.

1. On THIS PC: `pm2 stop ai-team-worker`
2. On the VPS: `pm2 stop ai-team-gateway` (frees the Telegram bot)
3. On THIS PC: `pm2 start ai-team-gateway --update-env`
4. If §1 caused session issues:
   `rm -rf state/sessions && mv state/sessions.bak state/sessions`
5. Confirm Telegram answers from the PC gateway again (`/status`).

You're back to the pre-migration single-machine setup. Diagnose offline, retry
the cutover later.

---

## Notes / gotchas

- **One Telegram owner at a time.** Two gateways polling the same bot token =
  conflicting `getUpdates`. §4 step 1 (stop PC gateway) before step 2 (start VPS)
  is non-negotiable.
- **`.env` `override=True`.** dotenv beats process env in this project, so the
  real config is whatever `.env` says — set values there, not just in the PM2
  `env` block.
- **Worker needs the backend CLIs installed locally** (`claude`, `opencode`,
  etc.) — the worker executes; the VPS only dispatches.
- **Fallback safety net:** if the node registry is empty, the orchestrator falls
  back to local execution — so a VPS with no workers still runs tasks itself
  (slower path, but no hard outage).
