/**
 * CostRangePicker — the Cost tab's day-range filter (24h / 48h / 7d / 30d,
 * default 7d). A four-way segmented control; the selected range drives the
 * `from`/`to` window every cost query in the tab is bound to.
 */
import { COST_RANGES, type CostRangeKey } from "../../lib/costPresentation";
import { cn } from "../../lib/cn";

export function CostRangePicker({
  value,
  onChange,
}: {
  value: CostRangeKey;
  onChange: (key: CostRangeKey) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Cost time range"
      className="cost-segment grid grid-cols-4"
    >
      {COST_RANGES.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          aria-pressed={value === key}
          className={cn(
            "min-h-11 rounded-lg px-4 text-xs font-medium tabular-nums transition-colors",
            value === key
              ? "bg-surface-1 text-ink ring-1 ring-hairline"
              : "text-ink-muted hover:text-ink",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
