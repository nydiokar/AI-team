/**
 * Section header — eyebrow label + count badge. The count is real information
 * (how much work sits in this bucket), so it's part of the header, not decor.
 */
import type { ReactNode } from "react";

export function SectionHeader({
  label,
  count,
  accent,
  action,
}: {
  label: string;
  count?: number;
  accent?: "warn" | "default";
  action?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-2 px-4 pb-2.5 pt-6">
      <h2
        className={`text-[11px] font-semibold uppercase tracking-[0.1em] ${
          accent === "warn" ? "text-warn" : "text-ink-muted"
        }`}
      >
        {label}
      </h2>
      {count != null && (
        <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
          {count}
        </span>
      )}
      <div className="ml-auto">{action}</div>
    </div>
  );
}
