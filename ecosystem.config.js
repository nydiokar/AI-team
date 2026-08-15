const path = require("path");
const fs = require("fs");

const isWindows = process.platform === "win32";
const venvPython = path.join(__dirname, ".venv", isWindows ? "Scripts/python.exe" : "bin/python");
const python = process.env.PM2_PYTHON || (fs.existsSync(venvPython) ? venvPython : (isWindows ? "python" : "python3"));
const defaultNodePath = isWindows ? "C:/Program Files/nodejs/node.exe" : "";
const nodePath = process.env.CODEX_NODE_PATH
  || process.env.NODE_EXE
  || (defaultNodePath && fs.existsSync(defaultNodePath) ? defaultNodePath : "");
const nodeDir = nodePath ? path.dirname(nodePath) : "";
const npmDir = isWindows && process.env.APPDATA ? path.join(process.env.APPDATA, "npm") : "";
const inheritedPath = process.env.PATH || process.env.Path || process.env.path || "";
const pathParts = [nodeDir, npmDir, inheritedPath].filter(Boolean);
const workerPath = isWindows ? pathParts.join(path.delimiter) : inheritedPath;

// Env-var prefixes PM2 must NOT capture from the launching shell. See the
// ai-team-worker block for the full rationale; short version: `pm2 save` writes
// the captured environment to ~/.pm2/dump.pm2 in PLAINTEXT, so anything a
// .env-sourced shell held leaks there and is then replayed on every resurrect.
// Every app loads .env itself, so nothing here needs to be inherited.
const SECRET_ENV_PREFIXES = [
  "WORKER_", "MESH_", "CONTROLLER_", "DASHBOARD_", "CLAUDE_", "CLAUDECODE",
  "VAPID_", "SOLANA_", "SOVA_", "TELEGRAM_", "ANTHROPIC_", "OPENAI_",
  "GITHUB_", "GH_", "AWS_", "VSCODE_", "WT_", "TERM_PROGRAM",
];

module.exports = {
  apps: [
    // ---------------------------------------------------------------
    // Primary gateway (always enabled)
    // ---------------------------------------------------------------
    {
      name: "ai-team-gateway",
      cwd: __dirname,
      script: "main.py",
      interpreter: python,
      args: "",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      watch: false,
      windowsHide: true,
      min_uptime: "10s",
      max_restarts: 20,
      restart_delay: 2000,
      kill_timeout: 15000,
      // See the ai-team-worker block below for why these two exist.
      filter_env: SECRET_ENV_PREFIXES,
      env: {
        PYTHONUNBUFFERED: "1",
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
      },
      out_file: path.join(__dirname, "logs", "pm2-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-error.log"),
      merge_logs: true,
      time: false,
    },

    // ---------------------------------------------------------------
    // Mesh task server.
    //
    // Two ways to run it:
    //   (a) EMBEDDED (default today): runs inside ai-team-gateway on its own
    //       event loop when MESH_ENABLED=true (src/control/embedded_server.py).
    //       Gateway + server share one get_registry() singleton. No extra PM2
    //       entry needed — just set MESH_ENABLED=true + MESH_TASK_SERVER_PORT.
    //   (b) STANDALONE (State Separation Phase 2, below): runs as its own
    //       ai-team-server process via server_main.py, so a gateway restart no
    //       longer kills the task queue / node registry. The gateway then talks
    //       to it over HTTP (src/control/task_server_client.py).
    //
    // The standalone entry is DISABLED by default. Do NOT run (a) and (b) at the
    // same time — they'd both try to bind MESH_TASK_SERVER_PORT. The cutover
    // (stop embedding, start ai-team-server) lands later in Phase 2.
    //
    // Enable standalone: pm2 start ecosystem.config.js --only ai-team-server
    // Required env (in .env): MESH_TASK_SERVER_PORT, WORKER_TOKEN,
    //   MESH_TAILSCALE_IP (or blank for 127.0.0.1), MESH_DB_PATH.
    // ---------------------------------------------------------------
    {
      name: "ai-team-server",
      cwd: __dirname,
      script: "server_main.py",
      interpreter: python,
      args: "",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      watch: false,
      min_uptime: "10s",
      max_restarts: 20,
      restart_delay: 2000,
      kill_timeout: 10000,   // no active execution to drain; just stop serving
      windowsHide: true,
      filter_env: SECRET_ENV_PREFIXES,
      env: {
        PYTHONUNBUFFERED: "1",
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
      },
      out_file: path.join(__dirname, "logs", "pm2-server-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-server-error.log"),
      merge_logs: true,
      time: false,
    },

    // ---------------------------------------------------------------
    // Worker daemon (per-machine — disabled by default)
    // Enable on worker machines with: pm2 start ecosystem.config.js --only ai-team-worker
    //
    // PM2 resolves `script` as a real file path, so we launch via the
    // worker_main.py shim rather than `python -m src.worker.agent` (PM2 would
    // try to run a file literally named "-m" and fail to start).
    //
    // Required env vars (set in .env — worker_main.py loads it):
    //   WORKER_NODE_ID, WORKER_TOKEN, WORKER_TAILSCALE_IP, CONTROLLER_URL,
    //   WORKER_BACKENDS
    // Optional:
    //   WORKER_API_PORT (9001), WORKER_MAX_CONCURRENT (2), WORKER_PROJECTS_ROOT
    //
    // windowsHide: PM2 spawns `interpreter` (the venv python.exe) directly. When
    // the PM2 daemon itself has no console — i.e. after a reboot, when the "PM2
    // Resurrect" scheduled task runs pm2-ressurect.bat — that child ALLOCATES ITS
    // OWN console and a stray terminal window pops up on the desktop, titled with
    // the venv interpreter path. Started by hand from a terminal it inherits that
    // console instead and stays invisible, which is why this only shows up on
    // boot-resurrect. windowsHide suppresses the allocation. No-op off Windows.
    //
    // filter_env: PM2 snapshots the FULL environment of whatever shell started the
    // app, and `pm2 save` writes that snapshot into ~/.pm2/dump.pm2 verbatim. A
    // shell that had .env sourced therefore leaks every secret it held (tokens,
    // private keys) into that file as plaintext, and `pm2 resurrect` then replays
    // those STALE values on every boot. Since agent.py:main() calls load_dotenv()
    // with override=False, inherited vars silently WIN over .env — so a stale dump
    // both leaks secrets and shadows the real config. Dropping these prefixes at
    // spawn keeps them out of the snapshot; .env stays the single source of truth.
    // ---------------------------------------------------------------
    {
      name: "ai-team-worker",
      cwd: __dirname,
      script: "worker_main.py",
      interpreter: python,
      args: "",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      watch: false,
      windowsHide: true,
      min_uptime: "10s",
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 35000,   // > 30s drain window in agent.py
      filter_env: SECRET_ENV_PREFIXES,
      env: {
        PYTHONUNBUFFERED: "1",
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
        PATH: workerPath,
        Path: workerPath,
        CODEX_NODE_PATH: nodePath,
        // Required — set these in .env or uncomment + fill here:
        // WORKER_NODE_ID: "main-pc",
        // WORKER_TOKEN: "...",
        // WORKER_TAILSCALE_IP: "100.x.x.x",   // or 127.0.0.1 for local test
        // CONTROLLER_URL: "http://127.0.0.1:9002",
        // WORKER_BACKENDS: "claude,opencode",
        // Optional:
        // WORKER_API_PORT: "9001",
        // WORKER_MAX_CONCURRENT: "2",
        // WORKER_PROJECTS_ROOT: "C:/Users/<user>/Projects",  // enables repo discovery
        // CODEX_NODE_PATH: "C:/Program Files/nodejs/node.exe",  // required if PM2 cannot see node.exe
      },
      out_file: path.join(__dirname, "logs", "pm2-worker-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-worker-error.log"),
      merge_logs: true,
      time: false,
    },

    // ---------------------------------------------------------------
    // Auto-deploy poller (T1 — gateway/server host ONLY, disabled by default)
    //
    // Pull-based CI: polls origin/main and self-deploys on the box it runs on.
    // Chosen over GitHub Actions -> SSH because the Pi5 is behind home NAT and
    // we don't want CI reaching into the tailnet / inbound SSH. PM2 owns the
    // schedule via cron_restart (run, exit, re-run) — no systemd timer needed.
    //
    // SCOPE: enable on the GATEWAY host ONLY. Do NOT enable on worker boxes —
    // auto-restarting a worker mid-task drops its in-flight claim (the T4 bug).
    //
    // Enable on the Pi5:  pm2 start ecosystem.config.js --only ai-team-deploy
    // Script: scripts/auto_deploy.sh  (git fetch -> ff-only -> pm2 reload ->
    //   /health gate -> rollback on failure). Linux/bash only.
    // Tunables via env (see the script header): DEPLOY_PM2_APPS,
    //   DEPLOY_HEALTH_URL, DEPLOY_HEALTH_TIMEOUT, DEPLOY_BRANCH.
    // ---------------------------------------------------------------
    {
      name: "ai-team-deploy",
      cwd: __dirname,
      script: path.join(__dirname, "scripts", "auto_deploy.sh"),
      interpreter: "bash",
      exec_mode: "fork",
      instances: 1,
      autorestart: false,      // not a daemon — it runs once and exits
      cron_restart: "*/2 * * * *",  // re-run every 2 minutes
      watch: false,
      env: {
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
        // Deploy gateway + standalone task server together (live split runs
        // MESH_EMBEDDED_SERVER=false, so ai-team-server owns :9002). Apps not
        // present on this host are skipped automatically by the script.
        DEPLOY_PM2_APPS: "ai-team-gateway ai-team-server",
        DEPLOY_HEALTH_URL: "http://127.0.0.1:9002/health",
        DEPLOY_HEALTH_TIMEOUT: "60",
      },
      out_file: path.join(__dirname, "logs", "pm2-deploy-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-deploy-error.log"),
      merge_logs: true,
      time: false,
    },
  ],
};
