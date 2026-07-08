/**
 * WorkCaseRow — one case in the operations inbox. Read-only navigation card:
 * title (from the case's own objective), bucket chip, and the authoritative
 * stage/status facts. No actions — tapping drills into the case detail.
 */
import { Link } from "react-router-dom";
import { ChevronRight, GitBranch, Clock } from "lucide-react";
import type { CaseSummary } from "../../domain/work";
import { ToneBadge } from "./ToneBadge";
import { bucketMeta } from "../../lib/workPresentation";
import { cn } from "../../lib/cn";

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function WorkCaseRow({ item }: { item: CaseSummary }) {
  const meta = bucketMeta(item.bucket);
  const age = relativeTime(item.updatedAt);
  const closed = item.bucket === "closed";

  return (
    <Link
      to={`/work/${encodeURIComponent(item.flowRunId)}`}
      className={cn(
        "card-elev group block rounded-2xl px-4 py-4 transition-transform active:scale-[0.99]",
        closed && "opacity-60",
      )}
    >
      {/* Row 1: title + bucket chip */}
      <div className="flex items-center gap-2.5">
        <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-tight text-ink">
          {item.title}
        </h3>
        <ToneBadge tone={meta.tone} label={meta.label} />
      </div>

      {/* Row 2: authoritative stage/status + lineage marker + timestamp */}
      <div className="mt-2 flex items-center gap-2 text-xs text-ink-muted">
        {item.currentStage ? (
          <span className="rounded-md bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-accent">
            {item.currentStage}
          </span>
        ) : (
          <span className="rounded-md bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-ink-muted">
            no stage
          </span>
        )}
        {item.status && (
          <span className="truncate font-mono text-[11px]">{item.status}</span>
        )}
        {item.parentFlowRunId && (
          <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted">
            <GitBranch className="size-3 opacity-50" />
            child
          </span>
        )}
        {age && (
          <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px]">
            <Clock className="size-3 opacity-50" />
            {age}
          </span>
        )}
      </div>

      {/* Row 3: flow id (the machine handle) + drill-in affordance */}
      <div className="mt-2.5 flex items-center gap-1.5">
        <p className="min-w-0 flex-1 truncate font-mono text-[11px] leading-snug text-ink-muted">
          {item.flowRunId}
        </p>
        <ChevronRight className="size-4 shrink-0 text-ink-muted transition-transform group-hover:translate-x-0.5" />
      </div>
    </Link>
  );
}
