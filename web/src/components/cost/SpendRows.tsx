/**
 * SpendRows — a generic ranked list of cost buckets (spend-by-project,
 * spend-by-model). Each row: the dimension label, known USD with a proportional
 * bar, coverage, and the dominant model(s) that drove the spend. Pure display.
 */
import { CoverageChip } from "./CoverageChip";
import { formatTokens } from "../../lib/costPresentation";
import type { CostSeries } from "../../domain/cost";
import { COST_COLORS, isFilterableProject, formatCostUsd as formatUsd } from "../../lib/costAnalytics";

export function SpendRows({
  rows,
  maxUsd,
  label,
  onPickProject,
}: {
  rows: CostSeries[];
  maxUsd: number;
  label: (dim: string) => string;
  onPickProject?: (dim: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <p className="py-10 text-center text-xs text-ink-muted">No project usage recorded in this window.</p>
    );
  }
  return (
    <div className="mt-5 space-y-5">
      {rows.map((row, index) => {
        const usd = row.usd.known;
        const pct = maxUsd > 0 ? (usd / maxUsd) * 100 : 0;
        return (
          <div key={`${row.bucket}-${row.dim}`}>
            <div className="flex items-center gap-2">
              {onPickProject && isFilterableProject(row.dim)
                ? <button type="button" onClick={() => onPickProject(row.dim)} className="min-h-11 min-w-0 flex-1 truncate text-left text-[13px] font-medium text-ink hover:text-accent" title={row.dim}>{label(row.dim)}</button>
                : <span className="flex min-h-11 min-w-0 flex-1 items-center truncate text-[13px] font-medium text-ink" title={row.dim}>{label(row.dim)}</span>}
              <span className="shrink-0 font-mono text-[13px] tabular-nums text-ink">{formatUsd(usd)}</span>
              <CoverageChip
                coveragePct={row.usd.coveragePct}
                unpricedTokens={row.usd.unpricedTokens}
              />
            </div>
            <div className="flex items-center gap-2">
              <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
                <div
                  className="h-full rounded-full"
                  style={{ width: `${Math.min(100, Math.max(pct, 0))}%`, background: COST_COLORS[index % COST_COLORS.length] }}
                />
              </div>
            </div>
            <div className="mt-2 flex items-center gap-2 text-[11px] text-ink-muted">
              <span className="tabular-nums">{formatTokens(row.tokens.total)} tokens</span>
              {row.usd.unpricedTokens > 0 && <span className="text-warn">· {formatTokens(row.usd.unpricedTokens)} unpriced</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
