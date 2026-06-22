/**
 * Local UI/presentation state (spec §11.3) — kept separate from server state.
 * Active target filter for the Sessions screen; closed-sessions collapse state.
 */
import { create } from "zustand";

interface UiState {
  /** null = all targets. Filters the Sessions list (spec §7.1 target selector). */
  targetFilter: string | null;
  setTargetFilter: (id: string | null) => void;
  closedExpanded: boolean;
  toggleClosed: () => void;
}

export const useUiStore = create<UiState>((set) => ({
  targetFilter: null,
  setTargetFilter: (id) => set({ targetFilter: id }),
  closedExpanded: false,
  toggleClosed: () => set((s) => ({ closedExpanded: !s.closedExpanded })),
}));
