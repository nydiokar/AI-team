/**
 * SystemAlertBanner — durable liveness-outage banner, distinct from
 * ConnectionBanner (which only shows while THIS tab can't reach the gateway
 * right now). This one surfaces the external healthcheck probe's log
 * (~/scripts/aiteam-healthcheck.sh), so an outage that happened while nobody
 * had the dashboard open is still visible afterward — with the diagnostic
 * detail (blocking stack frame / log tail) the probe captured, not just
 * "something was wrong".
 */
import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";
import { TriangleAlert, CheckCircle2, ChevronDown, X } from "lucide-react";
import { useSystemAlerts } from "../../hooks/useSystemAlerts";
import { useDismissedAlertsStore } from "../../stores/dismissedAlertsStore";

const RECOVERED_VISIBLE_MS = 30 * 60 * 1000; // keep a resolved outage visible 30 min

const KIND_LABEL: Record<string, string> = {
  process_down: "Gateway process down",
  unresponsive: "Gateway unresponsive",
  degraded: "Gateway degraded",
};

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function fmtDuration(openedAt: string, resolvedAt: string): string {
  const ms = Math.max(0, new Date(resolvedAt).getTime() - new Date(openedAt).getTime());
  const min = Math.round(ms / 60000);
  if (min < 1) return "<1m";
  if (min < 60) return `${min}m`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

export function SystemAlertBanner() {
  const { data: alerts } = useSystemAlerts();
  const [expanded, setExpanded] = useState(false);
  const dismissedIds = useDismissedAlertsStore((s) => s.ids);
  const dismiss = useDismissedAlertsStore((s) => s.dismiss);

  const latest = alerts?.[0];
  if (!latest || dismissedIds.includes(latest.id)) return null;

  const ongoing = latest.resolvedAt === null;
  const recentlyResolved =
    !ongoing && latest.resolvedAt != null
      ? Date.now() - new Date(latest.resolvedAt).getTime() < RECOVERED_VISIBLE_MS
      : false;
  if (!ongoing && !recentlyResolved) return null;

  const tone = ongoing ? "text-bad bg-bad/10" : "text-warn bg-warn/10";
  const Icon = ongoing ? TriangleAlert : CheckCircle2;
  const headline = ongoing
    ? `${KIND_LABEL[latest.kind] ?? "Gateway issue"} since ${fmtTime(latest.openedAt)}`
    : `${KIND_LABEL[latest.kind] ?? "Gateway issue"} — recovered after ${fmtDuration(
        latest.openedAt,
        latest.resolvedAt as string,
      )}`;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ height: 0, opacity: 0 }}
        animate={{ height: "auto", opacity: 1 }}
        exit={{ height: 0, opacity: 0 }}
        transition={{ duration: 0.2 }}
        className={`overflow-hidden text-xs ${tone}`}
      >
        <div className="flex items-center">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="flex min-w-0 flex-1 items-center justify-center gap-2 py-1.5 pl-4"
          >
            <Icon className="size-3.5 shrink-0" />
            <span className="truncate">{headline}</span>
            {(latest.detail || !ongoing) && (
              <ChevronDown
                className={`size-3 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
              />
            )}
          </button>
          <button
            type="button"
            aria-label="Dismiss alert"
            onClick={() => dismiss(latest.id)}
            className="shrink-0 px-3 py-1.5 opacity-70 hover:opacity-100"
          >
            <X className="size-3.5" />
          </button>
        </div>
        {expanded && (ongoing ? latest.detail : true) && (
          <div className="border-t border-current/10 px-4 py-2 text-left font-mono text-[10px] leading-snug opacity-80">
            {ongoing ? (
              latest.detail
            ) : (
              <div className="flex flex-col gap-1">
                <span className="text-bad line-through opacity-70">{latest.message}</span>
                <span className="text-ok">
                  Recovered at {fmtTime(latest.resolvedAt as string)} — the gateway is healthy
                  again. This banner clears automatically, or dismiss it with ×.
                </span>
                {latest.detail && (
                  <details className="mt-1">
                    <summary className="cursor-pointer">Show outage diagnostic</summary>
                    <pre className="mt-1 whitespace-pre-wrap">{latest.detail}</pre>
                  </details>
                )}
              </div>
            )}
          </div>
        )}
      </motion.div>
    </AnimatePresence>
  );
}
