/**
 * CostTotalCard — the tab's headline number: known USD within the window, the
 * pricing coverage %, and the token volume (with cache read broken out, since
 * that is what inflates codex spend). Includes the honest unattributed note when
 * no-session turns exist.
 */
import { Wallet } from "lucide-react";
import { CoverageChip } from "./CoverageChip";
import { formatTokens, formatUsd } from "../../lib/costPresentation";
import type { CostBucket } from "../../domain/cost";

export function CostTotalCard({
  totals,
  unattributedTokens,
}: {
  totals: CostBucket;
  unattributedTokens: number;
}) {
  const { tokens, usd } = totals;
  return (
    <div className="card-elev rounded-xl px-4 py-3.5">
      <div className="flex items-center gap-2 text-[12px] text-ink-muted">
        <Wallet className="size-3.5" />
        <span className="min-w-0 flex-1 truncate">estimated spend (window)</span>
        <CoverageChip coveragePct={usd.coveragePct} unpricedTokens={usd.unpricedTokens} />
      </div>
      <div className="mt-2 flex items-end justify-between gap-3">
        <div className="text-[26px] font-semibold leading-none tracking-tight tabular-nums text-ink">
          {formatUsd(usd.known)}
        </div>
        <div className="text-right text-[12px] leading-tight tabular-nums text-ink-muted">
          <div>{formatTokens(tokens.total)} tokens</div>
          <div className="text-[11px]">
            {formatTokens(tokens.cacheRead)} cached read
          </div>
        </div>
      </div>
      {unattributedTokens > 0 && (
        <div className="mt-2.5 border-t border-hairline/60 pt-2 text-[11px] text-ink-muted">
          {formatTokens(unattributedTokens)} tokens can't be tied to a session/project.
        </div>
      )}
    </div>
  );
}
