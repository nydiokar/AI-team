/**
 * Auth store — holds the operator-supplied Bearer DASHBOARD_TOKEN (spec §2.6 /
 * dashboard._require_auth). Persisted to localStorage so the token survives a
 * reload (matches the existing dashboard HTML shell behaviour). This is local UI
 * state (Zustand), separate from server state (TanStack Query) — spec §11.3.
 */
import { create } from "zustand";

const STORAGE_KEY = "ai_team_dash_token";

interface AuthState {
  token: string;
  hasToken: boolean;
  setToken: (token: string) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: localStorage.getItem(STORAGE_KEY) ?? "",
  hasToken: Boolean(localStorage.getItem(STORAGE_KEY)),
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
