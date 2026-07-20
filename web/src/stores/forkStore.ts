/**
 * Pending fork carry-over (feat/session-fork-case).
 *
 * When a session is forked, the verbatim digest of the marked messages is held
 * CLIENT-side (never pasted as a first message) and attached to the NEW session's
 * FIRST instruction as `continue_inline` (+ the Case id as `case_id`). This store
 * stashes that pending carry-over, keyed by the NEW session id, so it survives the
 * navigation from the fork sheet to the new session detail — and a reload / PWA
 * restart before the first send (localStorage).
 *
 * The Composer consumes it once on the first successful send, then clears it, so a
 * fork's context rides in exactly once and every later turn is a normal turn.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface ForkCarry {
  /** Verbatim digest of the marked messages (bounded). */
  continueInline: string;
  /** Case the forked session belongs to (may be empty if the Case write failed). */
  caseId: string;
}

interface ForkState {
  bySession: Record<string, ForkCarry>;
  /** Stash a pending carry-over for a newly-forked session. */
  setCarry: (sessionId: string, carry: ForkCarry) => void;
  /** Remove a carry-over (consumed on first send, or on discard). */
  clearCarry: (sessionId: string) => void;
}

/** Hard cap on stashed carry-overs. A carry is normally consumed on the forked
 *  session's first send (or cleared on close); this bounds the localStorage record
 *  if a fork is abandoned before either — evicting the oldest by insertion order
 *  (JS objects preserve string-key insertion order). */
const MAX_CARRIES = 25;

export const useForkStore = create<ForkState>()(
  persist(
    (set) => ({
      bySession: {},
      setCarry: (sessionId, carry) =>
        set((s) => {
          const next = { ...s.bySession, [sessionId]: carry };
          const keys = Object.keys(next);
          if (keys.length > MAX_CARRIES) {
            for (const stale of keys.slice(0, keys.length - MAX_CARRIES)) {
              if (stale !== sessionId) delete next[stale];
            }
          }
          return { bySession: next };
        }),
      clearCarry: (sessionId) =>
        set((s) => {
          if (!(sessionId in s.bySession)) return s;
          const next = { ...s.bySession };
          delete next[sessionId];
          return { bySession: next };
        }),
    }),
    { name: "ai-team-fork-carry", version: 1 },
  ),
);
