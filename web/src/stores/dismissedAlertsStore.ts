/**
 * Dismissed system alerts — "stop showing me this outage banner."
 *
 * Per-viewer (same rationale as dismissedStore): hiding a banner on my phone must
 * not rewrite the probe's log. Keyed by alert id, so a NEW outage still surfaces.
 * Bounded to the most recent MAX_IDS so localStorage never grows unbounded.
 */
import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

const MAX_IDS = 50;

interface DismissedAlertsState {
  ids: number[];
  dismiss: (alertId: number) => void;
}

export const useDismissedAlertsStore = create<DismissedAlertsState>()(
  persist(
    (set) => ({
      ids: [],
      dismiss: (alertId) =>
        set((s) => (s.ids.includes(alertId) ? s : { ids: [...s.ids, alertId].slice(-MAX_IDS) })),
    }),
    {
      name: "systemAlerts.dismissed",
      storage: createJSONStorage(() => localStorage),
    },
  ),
);
