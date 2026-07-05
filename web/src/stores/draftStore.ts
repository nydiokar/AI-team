/**
 * Per-session composer drafts (Telegram-style "unsent text stays in the box").
 *
 * The Composer used to hold its input in local React state, so navigating away
 * from a session detail (to peek at another session and copy a result) unmounted
 * it and threw the half-typed instruction away. This store lifts that text out of
 * the component and persists it to localStorage, so a draft survives:
 *   - navigating between sessions within the app (store outlives the unmount),
 *   - a full reload / the PWA being backgrounded and killed (localStorage).
 *
 * Keyed by sessionId. Empty drafts are pruned so the session list can treat
 * "has a key" as "has a draft". Cleared explicitly when its instruction is sent.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface DraftState {
  bySession: Record<string, string>;
  /** Set (or, when empty, prune) the draft for a session. */
  setDraft: (sessionId: string, text: string) => void;
  /** Remove a draft outright (on successful send). */
  clearDraft: (sessionId: string) => void;
}

export const useDraftStore = create<DraftState>()(
  persist(
    (set) => ({
      bySession: {},
      setDraft: (sessionId, text) =>
        set((s) => {
          if (!text) {
            if (!(sessionId in s.bySession)) return s;
            const next = { ...s.bySession };
            delete next[sessionId];
            return { bySession: next };
          }
          if (s.bySession[sessionId] === text) return s;
          return { bySession: { ...s.bySession, [sessionId]: text } };
        }),
      clearDraft: (sessionId) =>
        set((s) => {
          if (!(sessionId in s.bySession)) return s;
          const next = { ...s.bySession };
          delete next[sessionId];
          return { bySession: next };
        }),
    }),
    { name: "ai-team-drafts", version: 1 },
  ),
);
