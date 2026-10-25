/**
 * CostTotalCard — the tab's headline number: known USD within the window, the
 * pricing coverage %, and the token volume (with cache read broken out, since
 * that is what inflates codex spend). Includes the honest unattributed note when
 * no-session turns exist.
 */
import { Wallet, Layers3, Database, ScanLine } from "lucide-react";
import { CoverageChip } from "./CoverageChip";
import { formatTokens } from "../../lib/costPresentation";
import { formatCostUsd as formatUsd } from "../../lib/costAnalytics";
import type { CostBucket } from "../../domain/cost";

export function CostTotalCard({
  totals,
  unattributedTokens,
}: {
  totals: CostBucket;
  unattributedTokens: number;
}) {
  const { tokens, usd } = totals;
  const cacheShare = tokens.total > 0 ? tokens.cacheRead / tokens.total * 100 : null;
  return (
    <div className="cost-summary">
      <div className="cost-stat cost-stat-primary">
        <div className="cost-stat-label"><Wallet aria-hidden="true" />Estimated spend</div>
        <div className="cost-stat-value">{formatUsd(usd.known)}</div>
        <div className="cost-stat-note">Known USD · session-attributed</div>
      </div>
      <div className="cost-stat">
        <div className="cost-stat-label"><Layers3 aria-hidden="true" />Token volume</div>
        <div className="cost-stat-value" title={tokens.total.toLocaleString("en-US")}>{formatTokens(tokens.total)}</div>
        <div className="cost-stat-note">Across all four token buckets</div>
      </div>
      <div className="cost-stat">
        <div className="cost-stat-label"><Database aria-hidden="true" />Cache-read share</div>
        <div className="cost-stat-value">{cacheShare == null ? "—" : `${cacheShare.toFixed(1)}%`}</div>
        <div className="cost-stat-note">{formatTokens(tokens.cacheRead)} tokens read from cache</div>
      </div>
      <div className="cost-stat">
        <div className="cost-stat-label"><ScanLine aria-hidden="true" />Pricing coverage</div>
        <div className="cost-stat-value">{tokens.total > 0 ? usd.unpricedTokens > 0 && usd.coveragePct >= 100 ? ">99.9%" : `${usd.coveragePct.toFixed(1)}%` : "—"}</div>
        <div className="mt-2">
          {tokens.total > 0 ? <CoverageChip coveragePct={usd.coveragePct} unpricedTokens={usd.unpricedTokens} /> : <span className="cost-stat-note">No recorded usage</span>}
        </div>
      </div>
      {usd.unpricedTokens > 0 && <p className="col-span-full text-xs text-warn">{formatTokens(usd.unpricedTokens)} unpriced tokens are excluded from estimated spend.</p>}
      {unattributedTokens > 0 && <span className="sr-only">Additional usage without a session is listed separately below.</span>}
    </div>
  );
}
