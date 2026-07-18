/**
 * WorkScreen — the mobile operations inbox (A28). A read-only, honesty-first
 * view of Work/Cases from the A27 read model, grouped by AUTHORITATIVE attention
 * bucket. It is NOT a workflow editor: no creation, no actions, only drill-down.
 *
 * When the substrate is empty (HARNESS_FLOW_DRIVE off / no cases yet) it says so
 * plainly rather than inventing rows.
 */
import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Inbox, ChevronDown, Zap } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { WorkCaseRow } from "../components/work/WorkCaseRow";
import { InvokeManagerSheet } from "../components/work/InvokeManagerSheet";
import { useWorkList } from "../hooks/useWork";
import { BUCKET_ORDER, bucketMeta } from "../lib/workPresentation";
import type { CaseSummary, WorkBucket } from "../domain/work";
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
    <div className="space-y-3 px-4">
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
  const [closedExpanded, setClosedExpanded] = useState(false);
  const [invokeOpen, setInvokeOpen] = useState(false);

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

  return (
    <div className="pb-8">
      <CompactTopBar
        title="Work"
        subtitle="Cases · invoke the Manager to drive work"
        right={
          <button
            onClick={() => setInvokeOpen(true)}
            className="flex size-9 items-center justify-center rounded-full bg-accent-dim/60 text-accent ring-1 ring-accent/30 hover:bg-accent-dim"
            aria-label="Invoke Manager"
          >
            <Zap className="size-5" />
          </button>
        }
      />
      {invokeOpen && <InvokeManagerSheet onClose={() => setInvokeOpen(false)} />}

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
