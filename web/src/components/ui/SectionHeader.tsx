/**
 * Section header — eyebrow label + count badge. The count is real information
 * (how much work sits in this bucket), so it's part of the header, not decor.
 *
 * Pass `onToggle` to make it a collapsible section header (chevron on the
 * right, whole row is the button). This lets a panel like Jobs own ONE header
 * instead of stacking a static SectionHeader above its own collapse toggle.
 */
import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";

export function SectionHeader({
  label,
  count,
  accent,
  action,
  onToggle,
  expanded,
}: {
  label: string;
  count?: number;
  accent?: "warn" | "default";
  action?: ReactNode;
  onToggle?: () => void;
  expanded?: boolean;
}) {
  const labelEl = (
    <h2
      className={`text-[11px] font-semibold uppercase tracking-[0.1em] ${
        accent === "warn" ? "text-warn" : "text-ink-muted"
      }`}
    >
      {label}
    </h2>
  );

  const badge = count != null && (
    <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
      {count}
    </span>
  );

  if (onToggle) {
    return (
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="group flex w-full items-center gap-2 px-4 pb-2.5 pt-6 text-left transition-colors"
      >
        {labelEl}
        {badge}
        <div className="ml-auto flex items-center gap-2">
          {action}
          <ChevronDown
            className={`size-3.5 text-ink-muted transition-transform duration-200 group-hover:text-ink-soft ${
              expanded ? "rotate-180" : ""
            }`}
          />
        </div>
      </button>
    );
  }

  return (
    <div className="flex items-center gap-2 px-4 pb-2.5 pt-6">
      {labelEl}
      {badge}
      <div className="ml-auto">{action}</div>
    </div>
  );
}
