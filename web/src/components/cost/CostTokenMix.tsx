import type { TokenBuckets } from "../../domain/cost";
import { tokenComposition } from "../../lib/costAnalytics";
import { formatTokens } from "../../lib/costPresentation";

export function CostTokenMix({ tokens }: { tokens: TokenBuckets }) {
  const segments = tokenComposition(tokens);
  return (
    <div className="mt-6">
      <p className="text-3xl font-semibold tracking-tight tabular-nums text-ink">{formatTokens(tokens.total)}<span className="ml-2 text-xs font-normal text-ink-muted">recorded tokens</span></p>
      <div className="mt-6 flex h-5 overflow-hidden rounded-md bg-surface-2" role="img" aria-label="Token composition; exact amounts and shares listed below">
        {segments.map((segment) => <div key={segment.label} style={{ width: `${segment.pct}%`, background: segment.color }} />)}
      </div>
      <dl className="mt-5 space-y-4">
        {segments.map((segment) => <div key={segment.label} className="flex items-center gap-2 text-xs">
          <dt className="flex min-w-0 flex-1 items-center gap-2.5 text-ink-soft"><span className="size-2 rounded-full" style={{ background: segment.color }} aria-hidden="true" />{segment.label}</dt>
          <dd className="font-mono tabular-nums text-ink" title={segment.value.toLocaleString("en-US")}>{formatTokens(segment.value)}</dd>
          <dd className="w-12 text-right font-mono tabular-nums text-ink-muted">{segment.pct.toFixed(1)}%</dd>
        </div>)}
      </dl>
      <p className="mt-6 border-t border-hairline/60 pt-4 text-[11px] leading-relaxed text-ink-muted">Share of recorded tokens. Cache reads and writes have different prices; volume share does not measure money saved.</p>
    </div>
  );
}
