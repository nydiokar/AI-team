/**
 * [A82 Stage 8a] Retired backends — mirror of the gateway registry policy
 * (`src/backends/registry.py` RETIRED_BACKENDS). Existing sessions on a retired
 * backend stay readable and closable; no new session, turn or compaction is
 * accepted (the gateway answers 410 `backend_retired`).
 */
export const RETIRED_BACKENDS: Readonly<Record<string, string>> = {
  opencode: "The OpenCode CLI backend is retired — start a new OpenCode Server session to continue.",
};

/** Operator-facing reason when `backend` is retired, else null. */
export function retiredBackendReason(backend: string | null | undefined): string | null {
  return RETIRED_BACKENDS[(backend ?? "").trim().toLowerCase()] ?? null;
}

/** Backends offered for a NEW session (never a retired one). */
export const NEW_SESSION_BACKENDS = [
  { id: "claude", label: "Claude Code", icon: "🧠" },
  { id: "codex", label: "Codex", icon: "🤖" },
  { id: "opencode-server", label: "OpenCode Server", icon: "🛰" },
] as const;
