import type { CostSeries } from "../../domain/cost";
import { COST_COLORS, formatCostUsd as formatUsd } from "../../lib/costAnalytics";
import { formatTokens } from "../../lib/costPresentation";
import { CoverageChip } from "./CoverageChip";

export function CostModelMix({ rows }: { rows: CostSeries[] }) {
  const total = rows.reduce((sum, row) => sum + row.usd.known, 0);
  const priced = rows.filter((row) => row.usd.known > 0);
  const segments = priced.slice(0, 5).map((row) => ({ label: row.dim, value: row.usd.known }));
  if (priced.length > 5) segments.push({ label: `Other (${priced.length - 5})`, value: priced.slice(5).reduce((sum, row) => sum + row.usd.known, 0) });
  const unpriced = rows.reduce((sum, row) => sum + row.usd.unpricedTokens, 0);
  const circumference = 2 * Math.PI * 59;
  let offset = 0;

  return (
    <div className="mt-4">
      <div className="relative mx-auto size-44">
        <svg viewBox="0 0 160 160" className="size-full -rotate-90" role="img" aria-label={total > 0 ? `Model mix of ${formatUsd(total)} known USD; values listed below` : "No known USD to distribute"}>
          <circle cx="80" cy="80" r="59" fill="none" stroke="var(--color-surface-2)" strokeWidth="14" />
          {segments.map((segment, index) => {
            const length = total > 0 ? segment.value / total * circumference : 0;
            const currentOffset = offset;
            offset += length;
            return <circle key={segment.label} cx="80" cy="80" r="59" fill="none" stroke={COST_COLORS[index]} strokeWidth="14"
              strokeDasharray={`${length} ${circumference - length}`} strokeDashoffset={-currentOffset}>
              <title>{segment.label}: {formatUsd(segment.value)} ({(segment.value / total * 100).toFixed(1)}%)</title>
            </circle>;
          })}
        </svg>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-[27px] font-semibold tracking-tight tabular-nums text-ink">{priced.length}</span>
          <span className="mt-0.5 text-[11px] text-ink-muted">priced {priced.length === 1 ? "model" : "models"}</span>
        </div>
      </div>
      <ul className="mt-3 space-y-3" aria-label="Known spend by model">
        {segments.map((segment, index) => <li key={segment.label} className="flex items-center gap-2.5 text-xs">
          <span className="size-2 shrink-0 rounded-full" style={{ background: COST_COLORS[index] }} aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-ink-soft" title={segment.label}>{segment.label}</span>
          <span className="font-mono tabular-nums text-ink">{formatUsd(segment.value)}</span>
          <span className="w-11 text-right font-mono tabular-nums text-ink-muted">{(segment.value / total * 100).toFixed(1)}%</span>
        </li>)}
      </ul>
      {total === 0 && <p className="text-center text-xs text-ink-muted">{rows.length ? "Recorded usage has no known USD to distribute." : "No models recorded in this window."}</p>}
      {unpriced > 0 && <p className="mt-4 rounded-lg bg-warm-dim/40 px-3 py-2 text-xs text-warn">{formatTokens(unpriced)} unpriced tokens are excluded from this USD mix.</p>}
      {rows.length > 0 && <details className="cost-details mt-4">
        <summary>View all {rows.length} models</summary>
        <ul className="space-y-3 py-3">{rows.map((row) => <li key={row.dim} className="flex flex-wrap items-center gap-2 text-xs">
          <span className="min-w-0 flex-1 break-all text-ink-soft">{row.dim}</span>
          <span className="font-mono tabular-nums text-ink">{formatUsd(row.usd.known)}</span>
          <CoverageChip coveragePct={row.usd.coveragePct} unpricedTokens={row.usd.unpricedTokens} />
          <span className="w-full text-ink-muted">{row.tokens.total.toLocaleString("en-US")} tokens · {row.usd.unpricedTokens.toLocaleString("en-US")} unpriced</span>
        </li>)}</ul>
      </details>}
    </div>
  );
}
