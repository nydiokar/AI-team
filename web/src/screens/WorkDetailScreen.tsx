/**
 * WorkDetailScreen — one case. Full-screen (outside the bottom-nav shell) like
 * SessionDetail. Shows the case header (title + bucket + authoritative
 * stage/status), a compact lineage tree, the case↔entity ledger, and the
 * append-only audit timeline. The only write is explicit operator state control
 * for non-terminal Cases.
 *
 * This is deliberately NOT a second SessionDetail: it renders CASE truth from
 * the Work substrate and links OUT to sessions/artifacts for runtime detail.
 */
import type { ReactNode } from "react";
import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, AlertCircle, RefreshCw } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { SystemAlertBanner } from "../components/shell/SystemAlertBanner";
import { Button } from "../components/ui/Button";
import { ToneBadge } from "../components/work/ToneBadge";
import { CaseLineage } from "../components/work/CaseLineage";
import { CaseLedgerView } from "../components/work/CaseLedgerView";
import { CaseTimelineView } from "../components/work/CaseTimelineView";
import { CaseRosterView } from "../components/work/CaseRosterView";
import {
  useCloseCaseManually,
  useSetCaseState,
  useWorkDetail,
  useWorkGraph,
  useWorkTimeline,
  useWorkRoster,
} from "../hooks/useWork";
import { bucketMeta } from "../lib/workPresentation";
import { ApiError } from "../transport/apiClient";
import { invalidateRouteTarget } from "../lib/liveInvalidation";
import { cn } from "../lib/cn";

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

function PageControls({ label, onFirst, onNext }: {
  label: string;
  onFirst?: () => void;
  onNext?: () => void;
}) {
  if (!onFirst && !onNext) return null;
  return (
    <nav aria-label={`${label} pages`} className="mt-2 flex gap-2">
      {onFirst && <Button variant="outline" size="sm" onClick={onFirst}>First {label}</Button>}
      {onNext && <Button variant="outline" size="sm" onClick={onNext}>More {label}</Button>}
    </nav>
  );
}

export function WorkDetailScreen() {
  const { id } = useParams<{ id: string }>();
  return <WorkDetail key={id} id={id} />;
}

function WorkDetail({ id }: { id: string | undefined }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);
  const [linkAfter, setLinkAfter] = useState("0");
  const [sessionAfter, setSessionAfter] = useState("0");
  const [jobBefore, setJobBefore] = useState<string>();
  const detail = useWorkDetail(id, linkAfter);
  const graph = useWorkGraph(id);
  const timeline = useWorkTimeline(id);
  const roster = useWorkRoster(id, sessionAfter, jobBefore);
  const caseState = useSetCaseState(id);
  const closeCase = useCloseCaseManually(id);

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

  const refreshNow = async () => {
    if (!id || refreshing) return;
    setRefreshing(true);
    setRefreshMessage("Refreshing…");
    try {
      invalidateRouteTarget(queryClient, `/work/${encodeURIComponent(id)}`);
      await Promise.all(["work-detail", "work-timeline", "work-graph", "work-roster", "case-resume-state"].map(
        (key) => queryClient.refetchQueries(
          { queryKey: [key, id], type: "active" },
          { cancelRefetch: false, throwOnError: true },
        ),
      ));
      setRefreshMessage("Case refreshed.");
    } catch (e) {
      setRefreshMessage(`Refresh failed: ${String((e as Error)?.message ?? "unknown")}`);
    } finally {
      setRefreshing(false);
      window.setTimeout(() => setRefreshMessage(null), 3500);
    }
  };

  if (notFound) {
    return (
      <div className="desktop-detail mx-auto flex h-full max-w-[480px] flex-col bg-base">
        <ConnectionBanner />
        <SystemAlertBanner />
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
  const isTerminal = summary?.status === "closed" || summary?.status === "cancelled";
  const isBlocked = summary?.status === "blocked";

  const setState = async (state: "open" | "blocked") => {
    try {
      await caseState.mutateAsync({
        state,
        reason: state === "open" ? "operator_unblocked" : "operator_blocked",
      });
    } catch {
      // useMutation owns the error state rendered below.
    }
  };

  const closeManually = async () => {
    if (!summary) return;
    const ok = window.confirm(
      "Close this case manually? It will move to closed and leave linked sessions warm.",
    );
    if (!ok) return;
    try {
      await closeCase.mutateAsync({ reason: "operator_manual_close" });
    } catch {
      // useMutation owns the error state rendered below.
    }
  };

  return (
    <div className="desktop-detail mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <ConnectionBanner />
      <SystemAlertBanner />
      <CompactTopBar
        title={summary?.title ?? "Case"}
        subtitle={summary?.currentStage ?? (detail.isLoading ? "Loading…" : "no stage")}
        left={back}
        right={
          <button
            onClick={() => void refreshNow()}
            disabled={refreshing}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2 disabled:opacity-50"
            aria-label="Refresh case"
            title="Refresh case"
          >
            <RefreshCw className={cn("size-4", refreshing && "animate-spin")} />
          </button>
        }
      />
      {refreshMessage && (
        <div className="border-b border-hairline bg-surface-1 px-4 py-2 text-[12px] text-ink-soft">
          {refreshMessage}
        </div>
      )}

      <main className="flex-1 overflow-y-auto overscroll-contain pb-10">
        {detail.isLoading && !summary && (
          <div className="space-y-3 px-4 pt-6">
            <div className="h-20 animate-pulse rounded-2xl bg-surface-1" />
            <div className="h-32 animate-pulse rounded-2xl bg-surface-1" />
          </div>
        )}

        {detail.isError && !summary && (
          <p role="alert" className="px-4 pt-6 text-[12px] text-ink-muted">Case details are unavailable.</p>
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
                {!isTerminal && (
                  <div className="mt-4 flex items-center justify-between gap-3 border-t border-hairline/60 pt-3">
                    <p className="min-w-0 text-[12px] text-ink-muted">
                      {closeCase.error
                        ? `Close failed: ${closeCase.error.message}`
                        : caseState.error
                          ? `State failed: ${caseState.error.message}`
                          : "State"}
                    </p>
                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        variant={isBlocked ? "outline" : "danger"}
                        size="sm"
                        disabled={caseState.isPending || closeCase.isPending}
                        onClick={() => void setState(isBlocked ? "open" : "blocked")}
                      >
                        {isBlocked ? "Unblock" : "Block"}
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        disabled={caseState.isPending || closeCase.isPending}
                        onClick={() => void closeManually()}
                      >
                        Close
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* [A101] Quota resume surfaced in the session window — see
                CaseResumePanel in SessionDetailScreen. */}

            {/* Live roster — the operational head: who is working now + running
                scripts. Placed first because "what's happening right now" is the
                operator's primary question; lineage/ledger/timeline follow. */}
            <Section
              title="Live"
              count={roster.data ? roster.data.counts.sessions + roster.data.counts.jobs : undefined}
            >
              {roster.isError ? (
                <p role="alert" className="text-[12px] text-ink-muted">The live roster is unavailable.</p>
              ) : roster.data ? (
                <>
                  <CaseRosterView roster={roster.data} />
                  <PageControls
                    label="sessions"
                    onFirst={sessionAfter !== "0" ? () => setSessionAfter("0") : undefined}
                    onNext={!roster.isPlaceholderData && roster.data.nextSessionCursor
                      ? () => setSessionAfter(roster.data!.nextSessionCursor!)
                      : undefined}
                  />
                  <PageControls
                    label="scripts"
                    onFirst={jobBefore ? () => setJobBefore(undefined) : undefined}
                    onNext={!roster.isPlaceholderData && roster.data.nextJobCursor
                      ? () => setJobBefore(roster.data!.nextJobCursor!)
                      : undefined}
                  />
                </>
              ) : (
                <p className="text-[12px] text-ink-muted">Loading roster…</p>
              )}
            </Section>

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
              {detail.isError ? (
                <p role="alert" className="text-[12px] text-ink-muted">This ledger page is unavailable.</p>
              ) : (
                <CaseLedgerView ledger={detail.data!.ledger} />
              )}
              <PageControls
                label="links"
                onFirst={linkAfter !== "0" ? () => setLinkAfter("0") : undefined}
                onNext={!detail.isPlaceholderData && detail.data?.nextLinkCursor
                  ? () => setLinkAfter(detail.data!.nextLinkCursor!)
                  : undefined}
              />
            </Section>

            {/* Timeline */}
            <Section title="Timeline" count={timeline.data?.totalEventCount ?? timeline.data?.eventCount}>
              {timeline.isError ? (
                <p role="alert" className="text-[12px] text-ink-muted">The timeline is unavailable.</p>
              ) : timeline.data ? (
                <>
                  {(timeline.data.totalEventCount ?? 0) > timeline.data.eventCount && (
                    <p className="mb-2 text-[12px] text-ink-muted">Showing the latest {timeline.data.eventCount} events.</p>
                  )}
                  <CaseTimelineView timeline={timeline.data} />
                </>
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
