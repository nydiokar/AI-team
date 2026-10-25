/**
 * CaseCostSheet — the manager-vs-workers cost breakdown for one case (the
 * operator's explicit ask). Bottom sheet with the case title, a manager/worker
 * USD split (with the workers' share as a proportional bar), and one row per
 * session. Data is the live /api/cases/{id}/usage read model.
 */
import { Link } from "react-router-dom";
import { useRef } from "react";
import { X, Briefcase, UsersRound } from "lucide-react";
import { useCaseUsage } from "../../hooks/useCost";
import { caseTitle } from "../../transport/workAdapter";
import { CoverageChip } from "./CoverageChip";
import { formatTokens } from "../../lib/costPresentation";
import { formatCostUsd as formatUsd } from "../../lib/costAnalytics";
import { cn } from "../../lib/cn";
import * as Dialog from "@radix-ui/react-dialog";
import { CostQueryNotice } from "./CostPanel";

export function CaseCostSheet({
  flowRunId,
  onClose,
}: {
  flowRunId: string;
  onClose: () => void;
}) {
  const returnFocus = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const { data, isLoading, error, refetch } = useCaseUsage(flowRunId);
  const mgr = data?.mgrVsWorkers;
  const totalUsd = (mgr?.manager.usd.known ?? 0) + (mgr?.workers.usd.known ?? 0);
  const mgrPct = totalUsd > 0 ? ((mgr?.manager.usd.known ?? 0) / totalUsd) * 100 : 0;

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-black/65 backdrop-blur-sm" />
      <Dialog.Content onCloseAutoFocus={(event) => { event.preventDefault(); returnFocus.current?.focus(); }} className="card-elev fixed inset-x-0 bottom-0 z-50 mx-auto max-h-[85dvh] w-full max-w-[560px] overflow-y-auto rounded-t-2xl p-5 pb-8 outline-none sm:bottom-auto sm:top-1/2 sm:-translate-y-1/2 sm:rounded-2xl">
        <div className="mb-4 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Briefcase className="size-4 shrink-0 text-ink-muted" />
              <Dialog.Title className="truncate text-base font-semibold text-ink">
                {data ? caseTitle(data.case.objectiveLock, data.flowRunId) : "Case cost"}
              </Dialog.Title>
            </div>
            <Dialog.Description className="mt-1 text-xs text-ink-muted">Case lifetime usage · independent of the selected Cost filters.</Dialog.Description>
            <p className="mt-2 truncate font-mono text-[10px] text-ink-muted">{flowRunId}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex size-11 shrink-0 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
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

        <CostQueryNotice loading={false} error={error} hasData={Boolean(data)} onRetry={() => void refetch()} />

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
              <div className="mt-2 flex overflow-hidden rounded-full bg-surface-2" role="img" aria-label="Known USD split: manager and workers, amounts listed below">
                <div
                  className="h-2.5 rounded-full bg-accent/80 transition-all"
                  style={{ width: `${mgrPct}%` }}
                />
                <div className="h-2.5 bg-[#a99cf6]" style={{ width: `${totalUsd > 0 ? 100 - mgrPct : 0}%` }} />
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
                  <CoverageChip coveragePct={mgr.manager.usd.coveragePct} unpricedTokens={mgr.manager.usd.unpricedTokens} />
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
                  <CoverageChip coveragePct={mgr.workers.usd.coveragePct} unpricedTokens={mgr.workers.usd.unpricedTokens} />
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
      </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
