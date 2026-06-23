import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Mobile Web UI dev server. /api + /health are proxied to the gateway's embedded
// Control API (src/control/control_api.py, default DASHBOARD_PORT=9003) so Sessions
// + System bind to LIVE data in dev. In prod the gateway serves this built UI
// itself (U5) — no vite. Override the target with VITE_API_TARGET if the gateway
// runs elsewhere (e.g. its Tailscale IP:port).
export default defineConfig({
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
