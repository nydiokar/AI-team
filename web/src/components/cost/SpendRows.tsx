/**
 * SpendRows — a generic ranked list of cost buckets (spend-by-project,
 * spend-by-model). Each row: the dimension label, known USD with a proportional
 * bar, coverage, and the dominant model(s) that drove the spend. Pure display.
 */
import { CoverageChip } from "./CoverageChip";
import { formatTokens, formatUsd } from "../../lib/costPresentation";
import type { CostSeries } from "../../domain/cost";

export function SpendRows({
  rows,
  maxUsd,
  label,
}: {
  rows: CostSeries[];
  maxUsd: number;
  label: (dim: string) => string;
}) {
  if (rows.length === 0) {
    return (
      <p className="px-4 py-3 text-center text-[13px] text-ink-muted">No spend in this window.</p>
    );
  }
  return (
    <div className="space-y-1 px-4">
      {rows.map((row) => {
        const usd = row.usd.known;
        const pct = maxUsd > 0 ? (usd / maxUsd) * 100 : 0;
        const dominant = row.models.find((m) => m.known) ?? row.models[0];
        return (
          <div key={`${row.bucket}-${row.dim}`} className="rounded-xl px-2 py-2">
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
                {label(row.dim)}
              </div>
              <CoverageChip
                coveragePct={row.usd.coveragePct}
                unpricedTokens={row.usd.unpricedTokens}
              />
            </div>
            <div className="mt-1 flex items-center gap-2">
              <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-2">
                <div
                  className="h-full rounded-full bg-accent/70 transition-all"
                  style={{ width: `${Math.max(pct, 0)}%` }}
                />
              </div>
              <div className="shrink-0 text-[13px] font-semibold tabular-nums text-ink">
                {formatUsd(usd)}
              </div>
            </div>
            <div className="mt-1 flex items-center gap-2 text-[11px] text-ink-muted">
              <span className="tabular-nums">{formatTokens(row.tokens.total)} tokens</span>
              {dominant && (
                <>
                  <span className="text-ink-muted/50">·</span>
                  <span className="truncate font-mono text-accent/90">
                    {dominant.model}
                    {dominant.known && dominant.usdTotal != null
                      ? ` ${formatUsd(dominant.usdTotal)}`
                      : " (unpriced)"}
                  </span>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
