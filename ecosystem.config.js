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
    // Mesh task server (VPS-side — disabled by default)
    // Enable on the VPS with: pm2 start ecosystem.config.js --only ai-team-task-server
    // Requires: WORKER_TOKEN set in .env or env block below
    // ---------------------------------------------------------------
    {
      name: "ai-team-task-server",
      cwd: __dirname,
      script: "-m",
      interpreter: python,
      args: "uvicorn src.control.task_server:app --host 0.0.0.0 --port 9002",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      watch: false,
      min_uptime: "10s",
      max_restarts: 10,
      restart_delay: 3000,
      kill_timeout: 10000,
      // Disabled by default — start manually when deploying to VPS
      stop_exit_codes: [0],
      env: {
        PYTHONUNBUFFERED: "1",
        AI_TEAM_ENV_FILE: path.join(__dirname, ".env"),
        // WORKER_TOKEN: "set-in-.env"
        // MESH_DB_PATH: "state/mesh.db"
      },
      out_file: path.join(__dirname, "logs", "pm2-task-server-out.log"),
      error_file: path.join(__dirname, "logs", "pm2-task-server-error.log"),
      merge_logs: true,
      time: true,
    },

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
