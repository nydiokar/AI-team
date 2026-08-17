/**
 * WorkScreen — the mobile operations inbox (A28). An honesty-first view of
 * Work/Cases from the A27 read model, grouped by AUTHORITATIVE attention bucket.
 * It is not a workflow editor; the only write here is the operator maintenance
 * cleanup for stale Cases whose Manager session is gone/inactive.
 *
 * When the substrate is empty (HARNESS_FLOW_DRIVE off / no cases yet) it says so
 * plainly rather than inventing rows.
 */
import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle, ChevronDown, Inbox, RefreshCw, ShieldCheck, Zap } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { Button } from "../components/ui/Button";
import { WorkCaseRow } from "../components/work/WorkCaseRow";
import { PausedCaseInbox } from "../components/work/PausedCaseInbox";
import { NewSessionSheet } from "../components/sessions/NewSessionSheet";
import { useSweepOrphanedCases, useWorkList } from "../hooks/useWork";
import { BUCKET_ORDER, bucketMeta } from "../lib/workPresentation";
import type { CaseSummary, WorkBucket } from "../domain/work";
import type { RawCaseOrphanSweepResponse } from "../transport/rawApi";
import { cn } from "../lib/cn";

function SkeletonCard() {
  return (
    <div className="card-elev animate-pulse rounded-2xl px-4 py-4">
      <div className="flex items-center gap-2.5">
        <div className="h-4 w-40 rounded-md bg-surface-2" />
        <div className="ml-auto h-5 w-16 rounded-full bg-surface-2" />
      </div>
      <div className="mt-2 flex items-center gap-2">
        <div className="h-3.5 w-20 rounded-md bg-surface-2" />
        <div className="h-3 w-24 rounded bg-surface-2" />
      </div>
      <div className="mt-2.5 h-3 w-2/3 rounded bg-surface-2" />
    </div>
  );
}

function CaseList({ cases }: { cases: CaseSummary[] }) {
  return (
    <div className="desktop-card-list px-4">
      {cases.map((c, i) => (
        <motion.div
          key={c.flowRunId}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, delay: Math.min(i * 0.03, 0.2) }}
        >
          <WorkCaseRow item={c} />
        </motion.div>
      ))}
    </div>
  );
}

export function WorkScreen() {
  const { data, isLoading, error } = useWorkList();
  const sweep = useSweepOrphanedCases();
  const [closedExpanded, setClosedExpanded] = useState(false);
  const [invokeOpen, setInvokeOpen] = useState(false);
  const [sweepOpen, setSweepOpen] = useState(false);
  const [sweepResult, setSweepResult] = useState<RawCaseOrphanSweepResponse | null>(null);

  const grouped = useMemo(() => {
    const groups: Record<WorkBucket, CaseSummary[]> = {
      needs_decision: [],
      blocked: [],
      review: [],
      active: [],
      closed: [],
      unknown: [],
    };
    for (const c of data?.cases ?? []) groups[c.bucket].push(c);
    return groups;
  }, [data]);

  const empty = !isLoading && !error && (data?.cases.length ?? 0) === 0;
  const sweepCandidates = sweepResult?.candidates ?? [];
  const cleanedCount = sweepResult?.cleaned?.length ?? 0;

  const runSweep = async (dryRun: boolean) => {
    try {
      const result = await sweep.mutateAsync({
        dryRun,
        limit: 200,
        reason: "manager_session_unavailable",
      });
      setSweepResult(result);
    } catch {
      // useMutation owns the error state rendered by the panel.
    }
  };

  return (
    <div className="pb-8">
      <CompactTopBar
        title="Work"
        subtitle="Cases · invoke the Manager to drive work"
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => {
                setSweepOpen((v) => !v);
                if (!sweepOpen && sweepResult == null) void runSweep(true);
              }}
              className="flex size-9 items-center justify-center rounded-full bg-surface-1 text-warn ring-1 ring-hairline hover:bg-surface-2"
              aria-label="Stale case cleanup"
            >
              <ShieldCheck className="size-5" />
            </button>
            <button
              onClick={() => setInvokeOpen(true)}
              className="flex size-9 items-center justify-center rounded-full bg-accent-dim/60 text-accent ring-1 ring-accent/30 hover:bg-accent-dim"
              aria-label="Invoke Manager"
            >
              <Zap className="size-5" />
            </button>
          </div>
        }
      />
      {invokeOpen && (
        <NewSessionSheet initialRole="manager" onClose={() => setInvokeOpen(false)} />
      )}

      {/* Cases whose quota window reopened and are waiting on a decision. Renders
          nothing when there are none — it is a prompt, not a permanent widget. */}
      <PausedCaseInbox />

      {sweepOpen && (
        <div className="px-4 pt-4">
          <div className="rounded-xl border border-hairline bg-surface-1 px-3 py-3">
            <div className="flex items-center gap-2">
              <AlertTriangle className="size-4 text-warn" />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-semibold text-ink">Stale cases</p>
                <p className="mt-0.5 text-[12px] text-ink-muted">
                  {sweep.isPending
                    ? "Scanning..."
                    : sweep.error
                      ? `Failed: ${sweep.error.message}`
                      : sweepResult == null
                        ? "Not scanned"
                        : sweepResult.dry_run
                          ? `${sweepCandidates.length} candidate${sweepCandidates.length === 1 ? "" : "s"}`
                          : `${cleanedCount} blocked`}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void runSweep(true)}
                disabled={sweep.isPending}
                className="flex size-8 items-center justify-center rounded-full text-ink-muted hover:bg-surface-2 hover:text-ink-soft disabled:opacity-40"
                aria-label="Scan stale cases"
              >
                <RefreshCw className={cn("size-4", sweep.isPending && "animate-spin")} />
              </button>
            </div>

            {sweepCandidates.length > 0 && sweepResult?.dry_run && (
              <div className="mt-3 max-h-32 space-y-1 overflow-y-auto rounded-lg bg-surface-0/60 p-2">
                {sweepCandidates.slice(0, 6).map((c) => (
                  <div key={c.case_id} className="flex items-center gap-2 text-[11px]">
                    <span className="min-w-0 flex-1 truncate font-mono text-ink-soft">
                      {c.case_id}
                    </span>
                    <span className="shrink-0 text-ink-muted">
                      {c.manager_status ?? c.reason}
                    </span>
                  </div>
                ))}
                {sweepCandidates.length > 6 && (
                  <p className="text-[11px] text-ink-muted">+{sweepCandidates.length - 6} more</p>
                )}
              </div>
            )}

            <div className="mt-3 flex items-center justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSweepOpen(false)}
                disabled={sweep.isPending}
              >
                Hide
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => void runSweep(false)}
                disabled={
                  sweep.isPending ||
                  Boolean(sweep.error) ||
                  sweepCandidates.length === 0 ||
                  sweepResult?.dry_run !== true
                }
              >
                Block stale
              </Button>
            </div>
          </div>
        </div>
      )}

      {isLoading && (
        <div className="space-y-3 px-4 pt-4">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {error != null && (
        <p className="px-4 py-10 text-center text-sm text-bad">Couldn't load work.</p>
      )}

      {!isLoading && !error && (
        <>
          {BUCKET_ORDER.map((bucket) => {
            const cases = grouped[bucket];
            if (cases.length === 0) return null;
            const meta = bucketMeta(bucket);

            // Closed is collapsed by default — it's history, not attention.
            if (bucket === "closed") {
              return (
                <div key={bucket}>
                  <SectionHeader
                    label={meta.section}
                    count={cases.length}
                    action={
                      <button
                        onClick={() => setClosedExpanded((v) => !v)}
                        aria-expanded={closedExpanded}
                        className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink-soft"
                      >
                        {closedExpanded ? "Hide" : "Show"}
                        <ChevronDown
                          className={cn(
                            "size-3.5 transition-transform",
                            closedExpanded && "rotate-180",
                          )}
                        />
                      </button>
                    }
                  />
                  {closedExpanded && <CaseList cases={cases} />}
                </div>
              );
            }

            return (
              <div key={bucket}>
                <SectionHeader
                  label={meta.section}
                  count={cases.length}
                  accent={
                    bucket === "needs_decision" || bucket === "blocked"
                      ? "warn"
                      : "default"
                  }
                />
                <CaseList cases={cases} />
              </div>
            );
          })}
        </>
      )}

      {empty && (
        <div className="flex flex-col items-center gap-3 px-4 py-20 text-center">
          <div className="flex size-14 items-center justify-center rounded-2xl bg-surface-1 ring-1 ring-hairline">
            <Inbox className="size-7 text-ink-muted" />
          </div>
          <div>
            <p className="text-[15px] font-medium text-ink-soft">No cases yet</p>
            <p className="mt-1 text-sm text-ink-muted">
              Cases appear here once the work substrate records them. Runtime
              sessions live in the Sessions tab.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
