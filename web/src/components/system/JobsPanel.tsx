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
  ChevronRight,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useJobs } from "../../hooks/useLiveData";
import type { RawJob } from "../../transport/rawApi";
import { relAgeFrom } from "../../lib/time";
import {
  filterJobsByOwnership,
  type JobOwnershipFilter,
} from "../../lib/jobOwnership";

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

export function JobRow({ job, running }: { job: RawJob; running?: boolean }) {
  const v = STATUS_VISUAL[running ? "running" : job.status] ?? STATUS_VISUAL.lost;
  const { Icon, tint, spin } = v;
  // An orphaned job's session_id points at no reachable session — never link to a
  // dead page; surface it honestly instead so the job is visible, not hidden.
  const orphaned = Boolean(job.orphaned);
  const sessionHref = job.session_id && !orphaned ? `/sessions/${job.session_id}` : null;

  const inner = (
    <>
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${tint} ${spin ? "animate-spin" : ""}`} />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-ink">{job.label ?? job.id}</p>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-ink-muted">
          {orphaned && (
            <span
              className="max-w-full truncate text-warn"
              title={`Registered against session ${job.session_id}, which matches no known session.`}
            >
              orphaned · {job.node_id} · sess {job.session_id?.slice(0, 12)}
            </span>
          )}
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
      {sessionHref && <ChevronRight className="mt-0.5 size-3.5 shrink-0 text-ink-muted/60" />}
    </>
  );

  if (sessionHref) {
    return (
      <Link
        to={sessionHref}
        className="flex items-start gap-2.5 px-4 py-2.5 text-[13px] transition-colors hover:bg-surface-2/40"
      >
        {inner}
      </Link>
    );
  }

  return (
    <div className="flex items-start gap-2.5 px-4 py-2.5 text-[13px]">
      {inner}
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
  owned = "all",
}: {
  expanded: boolean;
  onSummary?: (s: JobsSummary) => void;
  owned?: JobOwnershipFilter;
}) {
  const { data, isLoading } = useJobs(
    20,
    undefined,
    owned === "unowned" ? "unowned" : undefined,
  );

  const running = filterJobsByOwnership(data?.running ?? [], owned);
  const recent = filterJobsByOwnership(
    (data?.recent ?? []).filter(
      (j) => j.status === "done" || j.status === "failed" || j.status === "lost",
    ),
    owned,
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
