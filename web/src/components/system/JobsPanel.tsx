/**
 * Jobs panel — parity with Telegram /jobs. Shows running watched jobs and
 * recently finished ones.
 *
 * Headerless by design: the parent renders ONE collapsible SectionHeader and
 * passes `expanded` down, so we never stack a second "Jobs" title (the old
 * double-header). Status uses the app's lucide icon system, not emoji.
 */
import { useEffect } from "react";
import {
  Loader2,
  CheckCircle2,
  XCircle,
  AlertTriangle,
} from "lucide-react";
import { useJobs } from "../../hooks/useLiveData";
import type { RawJob } from "../../transport/rawApi";
import { relAgeFrom } from "../../lib/time";

type JobsSummary = { total: number; running: number };

const STATUS_VISUAL: Record<
  string,
  { Icon: typeof CheckCircle2; tint: string; spin?: boolean }
> = {
  running: { Icon: Loader2, tint: "text-running", spin: true },
  done: { Icon: CheckCircle2, tint: "text-ok" },
  failed: { Icon: XCircle, tint: "text-bad" },
  lost: { Icon: AlertTriangle, tint: "text-warn" },
};

function JobRow({ job, running }: { job: RawJob; running?: boolean }) {
  const v = STATUS_VISUAL[running ? "running" : job.status] ?? STATUS_VISUAL.lost;
  const { Icon, tint, spin } = v;
  return (
    <div className="flex items-start gap-2.5 px-4 py-2.5 text-[13px]">
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${tint} ${spin ? "animate-spin" : ""}`} />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-ink">{job.label ?? job.id}</p>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-ink-muted">
          {running ? (
            <>
              {job.pid && <span className="font-mono">PID {job.pid}</span>}
              {job.last_checked_at && <span>checked {relAgeFrom(job.last_checked_at)}</span>}
              {job.last_probe_error && (
                <span className="max-w-[160px] truncate text-bad">{job.last_probe_error}</span>
              )}
              {Boolean(job.notify_agent) && <span className="text-accent">agent</span>}
            </>
          ) : (
            <>
              <span className={job.status === "failed" ? "text-bad" : ""}>{job.status}</span>
              {job.exit_code != null && <span className="font-mono">exit {job.exit_code}</span>}
              {Boolean(job.notify_agent) && <span className="text-accent">agent</span>}
              {job.updated_at && <span>{relAgeFrom(job.updated_at)}</span>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * @param onSummary lets the parent header reflect total / running counts and
 *                  hide the whole section when there's nothing to show.
 */
export function JobsPanel({
  expanded,
  onSummary,
}: {
  expanded: boolean;
  onSummary?: (s: JobsSummary) => void;
}) {
  const { data, isLoading } = useJobs();

  const running = data?.running ?? [];
  const recent = (data?.recent ?? []).filter(
    (j) => j.status === "done" || j.status === "failed" || j.status === "lost",
  );
  const total = running.length + recent.length;
  const runningCount = running.length;

  // Report upward via effect (never setState in a parent during child render).
  useEffect(() => {
    onSummary?.({ total, running: runningCount });
  }, [total, runningCount, onSummary]);

  if (!expanded) return null;

  return (
    <div className="mx-4 mb-2">
      <div className="card-elev divide-y divide-hairline overflow-hidden rounded-xl">
        {isLoading && (
          <p className="px-4 py-4 text-center text-sm text-ink-muted">Loading jobs…</p>
        )}
        {!isLoading && total === 0 && (
          <p className="px-4 py-5 text-center text-sm text-ink-muted">No watched jobs.</p>
        )}
        {running.map((j) => (
          <JobRow key={j.id} job={j} running />
        ))}
        {recent.map((j) => (
          <JobRow key={j.id} job={j} />
        ))}
      </div>
    </div>
  );
}
