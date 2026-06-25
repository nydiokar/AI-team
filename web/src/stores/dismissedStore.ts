/**
 * Dismissed-tasks store — "I don't want to act on this."
 *
 * Failed tasks are terminal and pile up forever; a user needs a way to clear them
 * from view without retrying. "Dismiss" is inherently a PER-VIEWER preference (not
 * a global state change — hiding a failure on my phone shouldn't rewrite the record
 * for anyone else), so it lives client-side, persisted to localStorage so a refresh
 * doesn't resurrect everything. If cross-device sync is ever wanted, this swaps for
 * a backend POST /api/tasks/{id}/acknowledge without touching the screen.
 */
import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

interface DismissedState {
  /** Set of dismissed task ids (stored as an array for JSON persistence). */
  ids: string[];
  dismiss: (taskId: string) => void;
  restore: (taskId: string) => void;
  clear: () => void;
  isDismissed: (taskId: string) => boolean;
}

export const useDismissedStore = create<DismissedState>()(
  persist(
    (set, get) => ({
      ids: [],
      dismiss: (taskId) =>
        set((s) => (s.ids.includes(taskId) ? s : { ids: [...s.ids, taskId] })),
      restore: (taskId) => set((s) => ({ ids: s.ids.filter((id) => id !== taskId) })),
      clear: () => set({ ids: [] }),
      isDismissed: (taskId) => get().ids.includes(taskId),
    }),
    {
      name: "tasks.dismissed",
      storage: createJSONStorage(() => localStorage),
    },
  ),
);
