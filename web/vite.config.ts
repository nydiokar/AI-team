import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import { execSync } from "node:child_process";

// Build identity — busts the persisted query cache on every deploy so we never
// serve a stale shape. Commit hash when available, else the build timestamp.
function buildVersion(): string {
  try {
    return execSync("git rev-parse --short HEAD").toString().trim();
  } catch {
    return `t${Date.now()}`;
  }
}

// Mobile Web UI dev server. /api + /health are proxied to the gateway's embedded
// Control API (src/control/control_api.py, default DASHBOARD_PORT=9003) so Sessions
// + System bind to LIVE data in dev. In prod the gateway serves this built UI
// itself (U5) — no vite. Override the target with VITE_API_TARGET if the gateway
// runs elsewhere (e.g. its Tailscale IP:port).
export default defineConfig({
  define: {
    __BUILD_VERSION__: JSON.stringify(buildVersion()),
  },
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5180,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:9003",
        changeOrigin: true,
      },
      "/health": {
        target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:9003",
        changeOrigin: true,
      },
    },
  },
});
