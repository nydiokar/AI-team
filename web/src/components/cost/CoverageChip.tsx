/**
 * CoverageChip — honest pricing coverage pill for a USD figure. Low coverage
 * (an unpriced model bucket) renders a warning: the number is real but partial,
 * and the pill says so instead of pretending.
 */
import { cn } from "../../lib/cn";

export function CoverageChip({
  coveragePct,
  unpricedTokens,
}: {
  coveragePct: number;
  unpricedTokens: number;
}) {
  const partial = coveragePct < 99.9;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium tabular-nums",
        partial ? "bg-warm-dim/70 text-warn" : "bg-ok/12 text-ok",
      )}
      title={
        partial
          ? `${unpricedTokens.toLocaleString()} unpriced tokens (no known price for the model)`
          : "all tokens priced"
      }
    >
      {partial ? `${coveragePct}% priced` : "fully priced"}
    </span>
  );
}
