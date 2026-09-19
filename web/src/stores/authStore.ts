/**
 * Auth store — holds the operator-supplied Bearer DASHBOARD_TOKEN (spec §2.6 /
 * dashboard._require_auth). Persisted to localStorage so the token survives a
 * reload (matches the existing dashboard HTML shell behaviour). This is local UI
 * state (Zustand), separate from server state (TanStack Query) — spec §11.3.
 */
import { create } from "zustand";

const STORAGE_KEY = "ai_team_dash_token";

/**
 * Initial token resolution. On a trusted request (the operator's tailnet devices,
 * Host-allowlisted against DNS rebinding) the gateway injects
 * `window.__DASHBOARD_TOKEN__`, so no pairing is needed; the injected token wins so a
 * rotated token is picked up without clearing localStorage. Otherwise a device pairs
 * once via `/#token=<token>` (the fragment is never sent to the server; it is stripped
 * from the address bar) or the TokenGate. Either way it is persisted to localStorage.
 */
function initialToken(): string {
  const injected = (window as unknown as { __DASHBOARD_TOKEN__?: string })
    .__DASHBOARD_TOKEN__;
  if (typeof injected === "string" && injected.length > 0) {
    localStorage.setItem(STORAGE_KEY, injected);
    return injected;
  }
  const fromHash = new URLSearchParams(window.location.hash.slice(1)).get("token");
  if (fromHash) {
    localStorage.setItem(STORAGE_KEY, fromHash.trim());
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    return fromHash.trim();
  }
  return localStorage.getItem(STORAGE_KEY) ?? "";
}

interface AuthState {
  token: string;
  hasToken: boolean;
  setToken: (token: string) => void;
  clear: () => void;
}

const _initial = initialToken();

export const useAuthStore = create<AuthState>((set) => ({
  token: _initial,
  hasToken: Boolean(_initial),
  setToken: (token) => {
    const t = token.trim();
    localStorage.setItem(STORAGE_KEY, t);
    set({ token: t, hasToken: Boolean(t) });
  },
  clear: () => {
    localStorage.removeItem(STORAGE_KEY);
    set({ token: "", hasToken: false });
  },
}));
