/**
 * Optimistic sent-message store (UI-2). Holds the user's just-sent instructions
 * per session so the timeline shows them immediately — before any server round-
 * trip — and reconciles them with their delivery ack (spec §9.2 optimistic UI).
 *
 * This is CLIENT state: the backend produces no message.created event for a sent
 * instruction (whole-turn model, gap-doc §6), so the only record of "what I typed"
 * lives here until the polled session summary reflects the resulting turn.
 */
import { create } from "zustand";
import type { DeliveryState } from "../hooks/useSessionActions";

export interface SentMessage {
  /** Client id (also the idempotency key for the submit). */
  id: string;
  sessionId: string;
  text: string;
  createdAt: string;
  delivery: DeliveryState;
  taskId: string | null;
}

interface SentState {
  bySession: Record<string, SentMessage[]>;
  add: (msg: SentMessage) => void;
  update: (id: string, patch: Partial<SentMessage>) => void;
  forSession: (sessionId: string) => SentMessage[];
}

export const useSentStore = create<SentState>((set, get) => ({
  bySession: {},
  add: (msg) =>
    set((s) => ({
      bySession: {
        ...s.bySession,
        [msg.sessionId]: [...(s.bySession[msg.sessionId] ?? []), msg],
      },
    })),
  update: (id, patch) =>
    set((s) => {
      const next: Record<string, SentMessage[]> = {};
      for (const [sid, list] of Object.entries(s.bySession)) {
        next[sid] = list.map((m) => (m.id === id ? { ...m, ...patch } : m));
      }
      return { bySession: next };
    }),
  forSession: (sessionId) => get().bySession[sessionId] ?? [],
}));
