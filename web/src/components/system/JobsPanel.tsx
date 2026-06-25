/**
 * Jobs panel — parity with Telegram /jobs.
 * Shows running watched jobs and recently finished ones.
 * Collapsible (collapsed by default — it's secondary info).
 */
import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { useJobs } from "../../hooks/useLiveData";
import { cn } from "../../lib/cn";

function ageLabel(ts: string | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 90) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

const STATUS_ICON: Record<string, string> = {
  done: "✅",
  failed: "❌",
  lost: "⚠️",
  running: "🔵",
};

export function JobsPanel() {
  const [expanded, setExpanded] = useState(false);
  const { data, isLoading } = useJobs();

  const running = data?.running ?? [];
  const recent = (data?.recent ?? []).filter(
    (j) => j.status === "done" || j.status === "failed" || j.status === "lost",
  );
  const total = running.length + recent.length;

  return (
    <div className="mx-4 mb-2">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between py-2 text-[11px] font-semibold uppercase tracking-wide text-ink-muted hover:text-ink-soft"
        aria-expanded={expanded}
      >
        <span>
          Jobs
          {total > 0 && (
            <span className="ml-1.5 rounded-full bg-surface-2 px-1.5 py-0.5 text-[10px] font-normal">
              {total}
            </span>
          )}
          {running.length > 0 && (
            <span className="ml-1.5 rounded-full bg-accent-dim/60 px-1.5 py-0.5 text-[10px] font-medium text-accent">
              {running.length} running
            </span>
          )}
        </span>
        <ChevronDown
          className={cn("size-3.5 transition-transform", expanded && "rotate-180")}
        />
      </button>

      {expanded && (
        <div className="card-elev divide-y divide-hairline rounded-xl">
          {isLoading && (
            <p className="px-4 py-4 text-center text-sm text-ink-muted">Loading jobs…</p>
          )}

          {!isLoading && total === 0 && (
            <p className="px-4 py-4 text-center text-sm text-ink-muted">No watched jobs.</p>
          )}

          {running.map((j) => (
            <div key={j.id} className="flex items-start gap-2.5 px-4 py-2.5 text-[13px]">
              <span className="mt-0.5 text-base">{STATUS_ICON.running}</span>
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium text-ink">{j.label ?? j.id}</p>
                <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-ink-muted">
                  {j.pid && <span className="font-mono">PID {j.pid}</span>}
                  {j.last_checked_at && <span>checked {ageLabel(j.last_checked_at)}</span>}
                  {j.last_probe_error && (
                    <span className="text-bad truncate max-w-[160px]">{j.last_probe_error}</span>
                  )}
                </div>
              </div>
            </div>
          ))}

          {recent.map((j) => (
            <div key={j.id} className="flex items-start gap-2.5 px-4 py-2.5 text-[13px]">
              <span className="mt-0.5 text-base">{STATUS_ICON[j.status] ?? "❓"}</span>
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium text-ink">{j.label ?? j.id}</p>
                <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-ink-muted">
                  <span className={j.status === "failed" ? "text-bad" : ""}>{j.status}</span>
                  {j.exit_code != null && <span className="font-mono">exit={j.exit_code}</span>}
                  {j.updated_at && <span>{ageLabel(j.updated_at)}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
