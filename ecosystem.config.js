const path = require("path");

const isWindows = process.platform === "win32";

module.exports = {
  apps: [
    {
      name: "ai-team-gateway",
      cwd: __dirname,
      script: "main.py",
      interpreter: process.env.PM2_PYTHON || (isWindows ? "python" : "python3"),
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
  ],
};
