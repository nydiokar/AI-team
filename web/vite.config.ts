import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Mobile Web UI dev server. /api is proxied to the read-only dashboard
// (src/control/dashboard.py) so Sessions + System bind to LIVE data in dev.
// Override the target with VITE_API_TARGET if the dashboard runs elsewhere.
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
