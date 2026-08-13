/**
 * CaseCostSheet — the manager-vs-workers cost breakdown for one case (the
 * operator's explicit ask). Bottom sheet with the case title, a manager/worker
 * USD split (with the workers' share as a proportional bar), and one row per
 * session. Data is the live /api/cases/{id}/usage read model.
 */
import { Link } from "react-router-dom";
import { X, Briefcase, UsersRound } from "lucide-react";
import { useCaseUsage } from "../../hooks/useCost";
import { caseTitle } from "../../transport/workAdapter";
import { CoverageChip } from "./CoverageChip";
import { formatTokens, formatUsd } from "../../lib/costPresentation";
import { cn } from "../../lib/cn";

export function CaseCostSheet({
  flowRunId,
  onClose,
}: {
  flowRunId: string;
  onClose: () => void;
}) {
  const { data, isLoading, error } = useCaseUsage(flowRunId);
  const mgr = data?.mgrVsWorkers;
  const totalUsd = (mgr?.manager.usd.known ?? 0) + (mgr?.workers.usd.known ?? 0);
  const mgrPct = totalUsd > 0 ? ((mgr?.manager.usd.known ?? 0) / totalUsd) * 100 : 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Briefcase className="size-4 shrink-0 text-ink-muted" />
              <h2 className="truncate text-base font-semibold text-ink">
                {data ? caseTitle(data.case.objectiveLock, data.flowRunId) : "Case cost"}
              </h2>
            </div>
            <p className="mt-0.5 truncate font-mono text-[11px] text-ink-muted">
              {flowRunId}
            </p>
          </div>
          <button
            onClick={onClose}
            className="flex size-8 shrink-0 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        {isLoading && !data && (
          <div className="space-y-2 animate-pulse">
            <div className="h-16 rounded-xl bg-surface-2" />
            <div className="h-20 rounded-xl bg-surface-2" />
          </div>
        )}

        {!isLoading && error != null && (
          <p className="py-6 text-center text-[13px] text-bad">Couldn't load case cost.</p>
        )}

        {mgr && (
          <>
            <div className="card-elev rounded-xl px-4 py-3">
              <div className="flex items-center justify-between text-[12px] text-ink-muted">
                <span className="flex items-center gap-1.5">
                  <UsersRound className="size-3.5" />
                  manager vs workers
                </span>
                {mgr.workersSharePct != null && (
                  <span className="font-medium tabular-nums text-ink-soft">
                    workers {mgr.workersSharePct}%
                  </span>
                )}
              </div>
              <div className="mt-2 flex gap-1 overflow-hidden rounded-full bg-surface-2">
                <div
                  className="h-2.5 rounded-full bg-accent/80 transition-all"
                  style={{ width: `${mgrPct}%` }}
                />
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-[13px]">
                <div>
                  <div className="text-[11px] text-ink-muted">manager</div>
                  <div className="font-semibold tabular-nums text-ink">
                    {formatUsd(mgr.manager.usd.known)}
                  </div>
                  <div className="text-[11px] tabular-nums text-ink-muted">
                    {formatTokens(mgr.manager.tokens.total)} tokens
                  </div>
                </div>
                <div>
                  <div className="text-[11px] text-ink-muted">
                    workers{mgr.workerSessions > 0 ? ` (${mgr.workerSessions})` : ""}
                  </div>
                  <div className="font-semibold tabular-nums text-ink">
                    {formatUsd(mgr.workers.usd.known)}
                  </div>
                  <div className="text-[11px] tabular-nums text-ink-muted">
                    {formatTokens(mgr.workers.tokens.total)} tokens
                  </div>
                </div>
              </div>
            </div>

            <div className="mt-3 space-y-1.5">
              {(data?.sessions ?? []).map((s) => (
                <Link
                  key={s.sessionId}
                  to={`/sessions/${encodeURIComponent(s.sessionId)}`}
                  className="flex items-center gap-2 rounded-xl bg-surface-1/60 px-3 py-2 ring-1 ring-hairline/50 transition-colors hover:bg-surface-2"
                >
                  <span
                    className={cn(
                      "w-14 shrink-0 rounded-full px-1.5 py-0.5 text-center text-[10px] font-medium",
                      s.role === "manager" ? "bg-accent-dim/60 text-accent" : "bg-surface-2 text-ink-muted",
                    )}
                  >
                    {s.role}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-soft">
                    {s.sessionId.slice(0, 12)}
                  </span>
                  <CoverageChip coveragePct={s.usd.coveragePct} unpricedTokens={s.usd.unpricedTokens} />
                  <span className="shrink-0 text-[12px] font-semibold tabular-nums text-ink">
                    {formatUsd(s.usd.known)}
                  </span>
                </Link>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
