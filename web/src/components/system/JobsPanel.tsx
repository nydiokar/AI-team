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
import { clockLabel, elapsed, relAgeFrom } from "../../lib/time";
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

function elapsedBetween(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt || !finishedAt) return "";
  const start = new Date(startedAt).getTime();
  const finish = new Date(finishedAt).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(finish)) return "";
  const sec = Math.max(0, (finish - start) / 1000);
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

export function JobRow({ job, running }: { job: RawJob; running?: boolean }) {
  const v = STATUS_VISUAL[running ? "running" : job.status] ?? STATUS_VISUAL.lost;
  const { Icon, tint, spin } = v;
  // Unlinked means the job has a session_id but this gateway has no reachable
  // session page for it. Process truth comes from status/lost/probe fields.
  const unlinkedSession = Boolean(job.orphaned);
  const sessionHref = job.session_id && !unlinkedSession ? `/sessions/${job.session_id}` : null;
  const started = job.started_at ? clockLabel(job.started_at) : "";
  const runningFor = running ? elapsed(job.started_at) : "";
  const finishedFor = !running ? elapsedBetween(job.started_at, job.finished_at) : "";

  const inner = (
    <>
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${tint} ${spin ? "animate-spin" : ""}`} />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-ink">{job.label ?? job.id}</p>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-ink-muted">
          {unlinkedSession && (
            <span
              className="max-w-full truncate text-warn"
              title={`Registered against session ${job.session_id}, which has no reachable page in this gateway.`}
            >
              unlinked session · sess {job.session_id?.slice(0, 12)}
            </span>
          )}
          {running ? (
            <>
              {started && <span>started {started}</span>}
              {runningFor && <span className="tabular-nums">running {runningFor}</span>}
              <span>{job.node_id}</span>
              {job.pid && <span className="font-mono">PID {job.pid}</span>}
              {job.last_checked_at && <span>probe {relAgeFrom(job.last_checked_at)}</span>}
              {job.last_probe_error && (
                <span className="max-w-[160px] truncate text-bad">{job.last_probe_error}</span>
              )}
              {Boolean(job.notify_agent) && <span className="text-accent">agent</span>}
            </>
          ) : (
            <>
              <span className={job.status === "failed" ? "text-bad" : ""}>{job.status}</span>
              {job.exit_code != null && <span className="font-mono">exit {job.exit_code}</span>}
              {started && <span>started {started}</span>}
              {finishedFor && <span className="tabular-nums">ran {finishedFor}</span>}
              {Boolean(job.notify_agent) && <span className="text-accent">agent</span>}
              {job.updated_at && <span>updated {relAgeFrom(job.updated_at)}</span>}
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
