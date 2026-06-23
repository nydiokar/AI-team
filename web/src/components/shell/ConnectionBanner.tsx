/**
 * Connection banner (spec §9.1). Derives connection state from the LIVE poll:
 * auth failure / unreachable / offline-with-cache. Stale data must read as
 * distinct from confirmed-current (acceptance #18). Animates in/out.
 */
import { AnimatePresence, motion } from "framer-motion";
import { TriangleAlert, WifiOff, KeyRound } from "lucide-react";
import { useSessions } from "../../hooks/useLiveData";
import { ApiError } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";

export function ConnectionBanner() {
  const hasToken = useAuthStore((s) => s.hasToken);
  const { status, error, isRefetching, dataUpdatedAt } = useSessions();

  let content: { text: string; tone: string; Icon: typeof WifiOff } | null = null;

  if (hasToken) {
    if (error instanceof ApiError && error.status === 401) {
      content = { text: "Invalid token — re-enter your DASHBOARD_TOKEN.", tone: "text-bad bg-bad/10", Icon: KeyRound };
    } else if (status === "error") {
      content = {
        text: dataUpdatedAt ? "Offline — showing last known state." : "Can't reach the gateway.",
        tone: "text-bad bg-bad/10",
        Icon: WifiOff,
      };
    } else if (status === "pending" && !dataUpdatedAt) {
      content = { text: "Connecting…", tone: "text-warn bg-warn/10", Icon: TriangleAlert };
    } else if (isRefetching && dataUpdatedAt) {
      content = null; // healthy refetch — don't alarm
    }
  }

  return (
    <AnimatePresence>
      {content && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.2 }}
          className={`flex items-center justify-center gap-2 overflow-hidden px-4 py-1.5 text-xs ${content.tone}`}
        >
          <content.Icon className="size-3.5" />
          {content.text}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
