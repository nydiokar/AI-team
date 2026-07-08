/**
 * WorkDetailScreen — one case, read-only (A28). Full-screen (outside the
 * bottom-nav shell) like SessionDetail. Shows the case header (title + bucket +
 * authoritative stage/status), a compact lineage tree, the case↔entity ledger,
 * and the append-only audit timeline. No actions beyond navigation/drill-down.
 *
 * This is deliberately NOT a second SessionDetail: it renders CASE truth from
 * the Work substrate and links OUT to sessions/artifacts for runtime detail.
 */
import type { ReactNode } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ChevronLeft, AlertCircle } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { ToneBadge } from "../components/work/ToneBadge";
import { CaseLineage } from "../components/work/CaseLineage";
import { CaseLedgerView } from "../components/work/CaseLedgerView";
import { CaseTimelineView } from "../components/work/CaseTimelineView";
import { useWorkDetail, useWorkGraph, useWorkTimeline } from "../hooks/useWork";
import { bucketMeta } from "../lib/workPresentation";
import { ApiError } from "../transport/apiClient";

function Section({ title, count, children }: {
  title: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section className="px-4 pt-6">
      <div className="mb-2.5 flex items-center gap-2">
        <h2 className="text-[13px] font-semibold tracking-tight text-ink-soft">{title}</h2>
        {count != null && (
          <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
            {count}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

export function WorkDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const detail = useWorkDetail(id);
  const graph = useWorkGraph(id);
  const timeline = useWorkTimeline(id);

  const notFound = detail.error instanceof ApiError && detail.error.status === 404;

  const back = (
    <button
      onClick={() => navigate(-1)}
      className="flex size-9 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
      aria-label="Back"
    >
      <ChevronLeft className="size-5" />
    </button>
  );

  if (notFound) {
    return (
      <div className="mx-auto flex h-full max-w-[480px] flex-col bg-base">
        <CompactTopBar title="Case" left={back} />
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-4 text-center">
          <AlertCircle className="size-8 text-ink-muted" />
          <p className="text-[15px] font-medium text-ink-soft">Case not found</p>
          <p className="text-sm text-ink-muted">
            This flow id has no recorded case in the work substrate.
          </p>
        </div>
      </div>
    );
  }

  const summary = detail.data?.summary;
  const meta = summary ? bucketMeta(summary.bucket) : null;

  return (
    <div className="mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <CompactTopBar
        title={summary?.title ?? "Case"}
        subtitle={summary?.currentStage ?? (detail.isLoading ? "Loading…" : "no stage")}
        left={back}
      />

      <main className="flex-1 overflow-y-auto overscroll-contain pb-10">
        {detail.isLoading && !summary && (
          <div className="space-y-3 px-4 pt-6">
            <div className="h-20 animate-pulse rounded-2xl bg-surface-1" />
            <div className="h-32 animate-pulse rounded-2xl bg-surface-1" />
          </div>
        )}

        {summary && meta && (
          <>
            {/* Header card: bucket + authoritative status/stage + flow id */}
            <div className="px-4 pt-4">
              <div className="card-elev rounded-2xl px-4 py-4">
                <div className="flex items-center gap-2">
                  <ToneBadge tone={meta.tone} label={meta.section} />
                  {summary.status && (
                    <span className="font-mono text-[11px] text-ink-muted">
                      {summary.status}
                    </span>
                  )}
                </div>
                <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
                  <dt className="text-ink-muted">Flow</dt>
                  <dd className="truncate font-mono text-ink-soft">{summary.flowRunId}</dd>
                  {summary.taskId && (
                    <>
                      <dt className="text-ink-muted">Root task</dt>
                      <dd className="truncate font-mono text-ink-soft">{summary.taskId}</dd>
                    </>
                  )}
                  {summary.dispatchedBy && (
                    <>
                      <dt className="text-ink-muted">Dispatched by</dt>
                      <dd className="truncate font-mono text-ink-soft">
                        {summary.dispatchedBy}
                      </dd>
                    </>
                  )}
                  {summary.dispatchFile && (
                    <>
                      <dt className="text-ink-muted">Dispatch</dt>
                      <dd className="truncate font-mono text-ink-soft">
                        {summary.dispatchFile}
                      </dd>
                    </>
                  )}
                </dl>
              </div>
            </div>

            {/* Lineage */}
            <Section title="Lineage">
              {graph.data ? (
                <CaseLineage graph={graph.data} />
              ) : (
                <p className="text-[12px] text-ink-muted">Loading lineage…</p>
              )}
            </Section>

            {/* Ledger */}
            <Section title="Ledger" count={detail.data?.counts.links}>
              <CaseLedgerView ledger={detail.data!.ledger} />
            </Section>

            {/* Timeline */}
            <Section title="Timeline" count={timeline.data?.eventCount}>
              {timeline.data ? (
                <CaseTimelineView timeline={timeline.data} />
              ) : (
                <p className="text-[12px] text-ink-muted">Loading timeline…</p>
              )}
            </Section>
          </>
        )}
      </main>
    </div>
  );
}
