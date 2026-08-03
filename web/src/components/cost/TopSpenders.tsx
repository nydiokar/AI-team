/**
 * TopSpenders — the "big-company top spenders" list: sessions ranked by USD (or
 * tokens) within the window. Each row shows the backend, known USD, coverage and
 * dominant model(s). A row that belongs to a case is tappable → drills into that
 * case's manager-vs-workers breakdown; standalone sessions are informational.
 */
import { ChevronRight, UserRound } from "lucide-react";
import { CoverageChip } from "./CoverageChip";
import { formatTokens, formatUsd } from "../../lib/costPresentation";
import type { TopSession } from "../../domain/cost";
import type { SessionAffiliation } from "../../domain/work";

export function TopSpenders({
  rows,
  affiliations,
  onPickCase,
}: {
  rows: TopSession[];
  affiliations: Map<string, SessionAffiliation>;
  onPickCase: (flowRunId: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <p className="px-4 py-3 text-center text-[13px] text-ink-muted">
        No session spend in this window.
      </p>
    );
  }
  return (
    <div className="space-y-2 px-4">
      {rows.map((row) => {
        const aff = affiliations.get(row.sessionId);
        const dominant = row.models.find((m) => m.known) ?? row.models[0];
        const tapTarget = aff?.flowRunId ? () => onPickCase(aff.flowRunId!) : undefined;
        return (
          <button
            key={row.sessionId}
            type="button"
            onClick={tapTarget}
            disabled={!tapTarget}
            className="card-elev block w-full rounded-xl px-4 py-3 text-left transition-transform enabled:active:scale-[0.99] disabled:opacity-90"
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink-soft">
                {row.sessionId.slice(0, 12)}
              </span>
              <span className="shrink-0 rounded-full bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium text-ink-muted">
                {row.backend ?? "—"}
              </span>
              <CoverageChip coveragePct={row.usd.coveragePct} unpricedTokens={row.usd.unpricedTokens} />
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <div className="min-w-0 flex-1 text-[15px] font-semibold tabular-nums text-ink">
                {formatUsd(row.usd.known)}
              </div>
              <div className="shrink-0 text-[11px] tabular-nums text-ink-muted">
                {formatTokens(row.tokens.total)} tokens
              </div>
            </div>
            <div className="mt-1 flex items-center gap-2 text-[11px] text-ink-muted">
              {aff ? (
                <span className="inline-flex min-w-0 items-center gap-1 truncate">
                  <UserRound className="size-3 shrink-0 text-accent/80" />
                  <span className="truncate">{aff.caseTitle}</span>
                  <ChevronRight className="size-3 shrink-0" />
                </span>
              ) : (
                <span>standalone session</span>
              )}
              {dominant && (
                <>
                  <span className="text-ink-muted/40">·</span>
                  <span className="truncate font-mono text-accent/90">{dominant.model}</span>
                </>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}
