import type { ContextFill } from "../../domain/models";
import { cn } from "../../lib/cn";
import { compactTokens } from "./SessionTurns";

/**
 * Session-level context-fill gauge (Feature #41) — shown before the operator
 * sends, so they can see how full the window is. Honesty-first: an unknown
 * source (no turns yet, or the backend model has no known window) renders an
 * explicit neutral "ctx —" state, never a fabricated percentage.
 */
export function ContextFillGauge({ contextFill }: { contextFill: ContextFill }) {
  if (contextFill.contextWindowSource === "unknown" || contextFill.contextUsedRatio == null) {
    return (
      <div
        className="flex items-center gap-2 px-1 text-[11px] text-ink-muted"
        title={contextFill.reason ?? "Context fill unknown"}
      >
        <span className="h-1.5 w-16 overflow-hidden rounded-full bg-surface-2" />
        <span>ctx —</span>
      </div>
    );
  }

  const pct = Math.min(1, Math.max(0, contextFill.contextUsedRatio)) * 100;
  const fillColor = pct >= 90 ? "bg-rose-400" : pct >= 70 ? "bg-amber-400" : "bg-accent";

  return (
    <div className="flex items-center gap-2 px-1 text-[11px] text-ink-muted">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-surface-2">
        <span
          className={cn("block h-full rounded-full", fillColor)}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className="font-mono">
        {pct.toFixed(pct < 10 ? 1 : 0)}%
        {contextFill.contextWindowTokens != null && (
          <>
            {" "}
            ({compactTokens(contextFill.contextUsedRatio * contextFill.contextWindowTokens)}/
            {compactTokens(contextFill.contextWindowTokens)})
          </>
        )}
      </span>
    </div>
  );
}
