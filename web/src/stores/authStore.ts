/**
 * Auth store — holds the operator-supplied Bearer DASHBOARD_TOKEN (spec §2.6 /
 * dashboard._require_auth). Persisted to localStorage so the token survives a
 * reload (matches the existing dashboard HTML shell behaviour). This is local UI
 * state (Zustand), separate from server state (TanStack Query) — spec §11.3.
 */
import { create } from "zustand";

const STORAGE_KEY = "ai_team_dash_token";

/**
 * Initial token resolution. The gateway NEVER embeds the token in the served page
 * (anyone able to fetch `/` would get full API access). A device pairs once by
 * opening `/#token=<token>` — the fragment is never sent to the server, so it stays
 * out of logs/referrers — or by typing it into the TokenGate. Either way it is
 * persisted to localStorage; the fragment is stripped from the address bar.
 */
function initialToken(): string {
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
