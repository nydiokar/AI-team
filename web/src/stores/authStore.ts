/**
 * Auth store — holds the operator-supplied Bearer DASHBOARD_TOKEN (spec §2.6 /
 * dashboard._require_auth). Persisted to localStorage so the token survives a
 * reload (matches the existing dashboard HTML shell behaviour). This is local UI
 * state (Zustand), separate from server state (TanStack Query) — spec §11.3.
 */
import { create } from "zustand";

const STORAGE_KEY = "ai_team_dash_token";

/**
 * Initial token resolution (U5): when the gateway serves this UI over the tailnet
 * it injects `window.__DASHBOARD_TOKEN__` into the page, so a trusted device skips
 * the TokenGate entirely. Falls back to a previously stored token (manual entry in
 * dev / vite). The injected token wins so a redeploy with a rotated token is picked
 * up without the user clearing localStorage.
 */
function initialToken(): string {
  const injected = (window as unknown as { __DASHBOARD_TOKEN__?: string })
    .__DASHBOARD_TOKEN__;
  if (typeof injected === "string" && injected.length > 0) {
    localStorage.setItem(STORAGE_KEY, injected);
    return injected;
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
