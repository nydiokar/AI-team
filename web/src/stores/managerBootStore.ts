/**
 * Optimistic Manager-boot store.
 *
 * A Manager fork/invoke delivers its assignment (objective + any forked prior
 * conversation) ENTIRELY server-side on the Manager's first turn (POST /api/manager
 * → invoke_manager). Unlike a typed message, the client never echoes it, and the
 * conversation transcript (GET /api/sessions/{id}/messages) is only written when a
 * turn COMPLETES. So during the (long) boot turn the operator sees the session
 * "working" but no assignment — blind to the goal they just gave it.
 *
 * This store stashes a lightweight optimistic record of that assignment, keyed by
 * the NEW session id, so the session timeline can render a synthetic user bubble
 * immediately. It survives the fork-sheet → session-detail navigation and a
 * reload/PWA restart before the boot turn lands (localStorage). The timeline stops
 * rendering it — and the detail screen clears it — once the real boot turn appears
 * in the transcript, so the full server-rendered assignment takes over with no
 * duplicate.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface ManagerBoot {
  /** The operator-supplied objective (goal) for this Manager. */
  objective: string;
  /** True when this Manager was forked from a prior conversation (context carried). */
  hasPriorContext: boolean;
  /** Wall-clock stamp so the synthetic bubble has an honest anchor. */
  createdAt: string;
}

interface ManagerBootState {
  bySession: Record<string, ManagerBoot>;
  setBoot: (sessionId: string, boot: ManagerBoot) => void;
  clearBoot: (sessionId: string) => void;
}

/** Hard cap on stashed boot records — normally cleared once the boot turn lands;
 *  this bounds the localStorage record if a session is abandoned first, evicting
 *  the oldest by insertion order (JS objects preserve string-key insertion order). */
const MAX_BOOTS = 25;

export const useManagerBootStore = create<ManagerBootState>()(
  persist(
    (set) => ({
      bySession: {},
      setBoot: (sessionId, boot) =>
        set((s) => {
          const next = { ...s.bySession, [sessionId]: boot };
          const keys = Object.keys(next);
          if (keys.length > MAX_BOOTS) {
            for (const stale of keys.slice(0, keys.length - MAX_BOOTS)) {
              if (stale !== sessionId) delete next[stale];
            }
          }
          return { bySession: next };
        }),
      clearBoot: (sessionId) =>
        set((s) => {
          if (!(sessionId in s.bySession)) return s;
          const next = { ...s.bySession };
          delete next[sessionId];
          return { bySession: next };
        }),
    }),
    { name: "ai-team-manager-boot", version: 1 },
  ),
);
