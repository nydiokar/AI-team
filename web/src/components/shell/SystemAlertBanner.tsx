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
import { TriangleAlert, CheckCircle2, ChevronDown } from "lucide-react";
import { useSystemAlerts } from "../../hooks/useSystemAlerts";

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

  const latest = alerts?.[0];
  if (!latest) return null;

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
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-center justify-center gap-2 px-4 py-1.5"
        >
          <Icon className="size-3.5 shrink-0" />
          <span className="truncate">{headline}</span>
          {latest.detail && (
            <ChevronDown
              className={`size-3 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
            />
          )}
        </button>
        {expanded && latest.detail && (
          <div className="border-t border-current/10 px-4 py-2 text-left font-mono text-[10px] leading-snug opacity-80">
            {latest.detail}
          </div>
        )}
      </motion.div>
    </AnimatePresence>
  );
}
