/**
 * ToneBadge — a small tinted pill using the same tone vocabulary as StatusChip
 * (running|ok|warn|bad|idle). Used by the Work surface for bucket labels and
 * session affiliation roles. Label always carries meaning; color is secondary.
 */
import { cn } from "../../lib/cn";
import type { Tone } from "../../lib/workPresentation";

const TONE: Record<Tone, { fill: string; text: string; dot: string; pulse: boolean }> = {
  running: { fill: "bg-running/12", text: "text-running", dot: "bg-running", pulse: true },
  ok: { fill: "bg-ok/12", text: "text-ok", dot: "bg-ok", pulse: false },
  warn: { fill: "bg-warm-dim/70", text: "text-warn", dot: "bg-warn", pulse: true },
  bad: { fill: "bg-bad/12", text: "text-bad", dot: "bg-bad", pulse: false },
  idle: { fill: "bg-surface-3/70", text: "text-ink-soft", dot: "bg-ink-muted", pulse: false },
};

export function ToneBadge({
  tone,
  label,
  dot = true,
  className,
}: {
  tone: Tone;
  label: string;
  dot?: boolean;
  className?: string;
}) {
  const t = TONE[tone];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full py-1 pl-2 pr-2.5 text-[11px] font-medium",
        t.fill,
        t.text,
        className,
      )}
    >
      {dot && (
        <span className={cn("size-1.5 rounded-full", t.dot, t.pulse && "pulse-dot")} />
      )}
      {label}
    </span>
  );
}
