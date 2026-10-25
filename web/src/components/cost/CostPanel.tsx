import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";

export function CostPanel({ title, subtitle, action, children, className = "" }: {
  title: string;
  subtitle: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section aria-label={title} className={`cost-panel min-w-0 ${className}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold tracking-tight text-ink">{title}</h2>
          <p className="mt-1 text-xs text-ink-muted">{subtitle}</p>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function CostQueryNotice({ loading, error, hasData, onRetry }: {
  loading: boolean;
  error: unknown;
  hasData: boolean;
  onRetry: () => void;
}) {
  if (error) return (
    <div role="status" className="mt-4 flex flex-wrap items-center gap-2 rounded-xl bg-warm-dim/50 px-3 py-2 text-xs text-warn">
      <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
      <span className="flex-1">{hasData ? "Refresh failed. Showing last available data." : "This data is unavailable."}</span>
      <button type="button" className="cost-text-button" onClick={onRetry}>Retry</button>
    </div>
  );
  if (loading && !hasData) return (
    <div role="status" aria-label="Loading cost data" className="mt-5 space-y-3 animate-pulse">
      <div className="h-8 w-28 rounded-lg bg-surface-2" />
      <div className="h-28 rounded-xl bg-surface-2/60" />
      <span className="sr-only">Loading cost data…</span>
    </div>
  );
  return null;
}
