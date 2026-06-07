const path = require("path");

const isWindows = process.platform === "win32";
const python = process.env.PM2_PYTHON || (isWindows ? "python" : "python3");

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
      min_uptime: "10s",
      max_restarts: 20,
      restart_delay: 2000,
      kill_timeout: 15000,
      env: {
        PYTHONUNBUFFERED: "1",
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
      },
      out_file: path.join(__dirname, "logs", "pm2-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-error.log"),
      merge_logs: true,
      time: true,
    },

    // ---------------------------------------------------------------
    // NOTE: the mesh task server is no longer a separate PM2 process.
    // As of Phase 9 Step D1 it runs *embedded* inside ai-team-gateway
    // (see src/control/embedded_server.py), started by the orchestrator on
    // its own event loop when MESH_ENABLED=true. This makes the gateway and
    // the task server share one get_registry() singleton, eliminating the
    // cross-process / DB-only node-discovery workaround.
    //
    // To run mesh routing: set MESH_ENABLED=true, MESH_TAILSCALE_IP, and
    // MESH_TASK_SERVER_PORT in .env. No extra PM2 entry is needed.
    // ---------------------------------------------------------------

    // ---------------------------------------------------------------
    // Worker daemon (per-machine — disabled by default)
    // Enable on worker machines with: pm2 start ecosystem.config.js --only ai-team-worker
    // Required env vars: WORKER_NODE_ID, WORKER_TOKEN, WORKER_TAILSCALE_IP,
    //                    CONTROLLER_URL, WORKER_BACKENDS
    // ---------------------------------------------------------------
    {
      name: "ai-team-worker",
      cwd: __dirname,
      script: "-m",
      interpreter: python,
      args: "src.worker.agent",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      watch: false,
      min_uptime: "10s",
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 35000,   // > 30s drain window in agent.py
      env: {
        PYTHONUNBUFFERED: "1",
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
        // Required — set these in .env or override here:
        // WORKER_NODE_ID: "main-pc"
        // WORKER_TOKEN: "..."
        // WORKER_TAILSCALE_IP: "100.x.x.x"   (or 127.0.0.1 for local test)
        // CONTROLLER_URL: "http://127.0.0.1:9002"
        // WORKER_BACKENDS: "claude,opencode"
        // WORKER_API_PORT: "9001"
        // WORKER_MAX_CONCURRENT: "2"
      },
      out_file: path.join(__dirname, "logs", "pm2-worker-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-worker-error.log"),
      merge_logs: true,
      time: true,
    },
  ],
};
