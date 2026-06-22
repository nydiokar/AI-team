import { defineConfig } from "vitest/config";
import path from "node:path";

// Vitest config kept separate from vite.config.ts: Vite 8's UserConfig no longer
// carries the `test` key, and tests don't need the React/Tailwind plugins.
export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "node",
    globals: true,
  },
});
