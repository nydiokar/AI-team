/**
 * TopSpenders — the "big-company top spenders" list: sessions ranked by USD (or
 * tokens) within the window. Each row shows the backend, known USD, coverage and
 * dominant model(s). A row that belongs to a case is tappable → drills into that
 * case's manager-vs-workers breakdown; standalone sessions are informational.
 */
import { ArrowUpRight, Layers3 } from "lucide-react";
import { Link } from "react-router-dom";
import { CoverageChip } from "./CoverageChip";
import { formatTokens, projectLabel } from "../../lib/costPresentation";
import { formatCostUsd as formatUsd } from "../../lib/costAnalytics";
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
    <div className="mt-5">
      <div className="cost-session-grid border-b border-hairline/60 pb-3 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-muted" aria-hidden="true">
        <span>#</span><span>Session / project</span><span className="text-right">Known USD</span><span className="cost-session-tokens text-right">Tokens</span><span className="sr-only">Case</span>
      </div>
      <ol className="divide-y divide-hairline/50">
      {rows.map((row, index) => {
        const aff = affiliations.get(row.sessionId);
        const dominant = row.models.find((m) => m.known) ?? row.models[0];
        return (
          <li key={row.sessionId} className="cost-session-grid py-4">
            <span className="font-mono text-[11px] tabular-nums text-ink-muted">{String(index + 1).padStart(2, "0")}</span>
            <div className="min-w-0">
              <Link to={`/sessions/${encodeURIComponent(row.sessionId)}`} className="inline-flex min-h-11 max-w-full items-center gap-1 text-[13px] font-medium text-ink hover:text-accent" title={row.sessionId}>
                <span className="truncate">{row.repoPath ? projectLabel(row.repoPath) : "Unassigned project"}</span><ArrowUpRight className="size-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
                <span className="sr-only">Open session {row.sessionId}</span>
              </Link>
              <div className="truncate font-mono text-[10px] text-ink-muted" title={row.sessionId}>{row.sessionId.slice(0, 12)} · {row.backend ?? "unknown backend"}{row.role ? ` · ${row.role}` : ""}</div>
              {dominant && <div className="mt-1 truncate text-[11px] text-ink-muted" title={dominant.model}>{dominant.model}</div>}
            </div>
            <div className="text-right">
              <p className="font-mono text-sm tabular-nums text-ink">{formatUsd(row.usd.known)}</p>
              <div className="mt-1"><CoverageChip coveragePct={row.usd.coveragePct} unpricedTokens={row.usd.unpricedTokens} /></div>
              <p className="cost-session-mobile-tokens mt-1 text-[10px] text-ink-muted">{formatTokens(row.tokens.total)} tokens</p>
            </div>
            <div className="cost-session-tokens text-right font-mono text-xs tabular-nums text-ink-soft" title={row.tokens.total.toLocaleString("en-US")}>{formatTokens(row.tokens.total)}</div>
            {aff?.flowRunId ? <button type="button" className="flex size-11 shrink-0 items-center justify-center rounded-xl text-ink-muted hover:bg-surface-2 hover:text-accent" aria-label={`View case lifetime cost: ${aff.caseTitle}`} title={`Case lifetime · ${aff.caseTitle}`} onClick={() => onPickCase(aff.flowRunId!)}><Layers3 className="size-4" aria-hidden="true" /></button> : <span />}
          </li>
        );
      })}
      </ol>
    </div>
  );
}
