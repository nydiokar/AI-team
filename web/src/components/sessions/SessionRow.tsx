/**
 * SessionRow — an elevated card (spec §7.1, matching the mock). Title + live
 * status pill, mono backend·target·repo line, last-activity summary. Attention
 * sessions get the signature amber edge-glow. Closed rows are dimmed. Taps to
 * the session timeline.
 */
import { Link } from "react-router-dom";
import { ChevronRight, GitBranch } from "lucide-react";
import type { Session } from "../../domain/models";
import { SessionStatusChip } from "../ui/StatusChip";
import { cn } from "../../lib/cn";

function shortPath(p: string): string {
  const parts = p.split(/[/\\]/).filter(Boolean);
  return parts.slice(-2).join("/") || p;
}

export function SessionRow({ session }: { session: Session }) {
  const closed = session.lifecycle === "closed";
  return (
    <Link
      to={`/sessions/${session.id}`}
      className={cn(
        "card-elev group block rounded-2xl px-4 py-4 transition-transform active:scale-[0.99]",
        session.needsAttention && "attention-glow",
        closed && "opacity-55",
      )}
    >
      <div className="flex items-center gap-2.5">
        <h3 className="min-w-0 flex-1 truncate text-[16px] font-semibold tracking-tight text-ink">
          {session.id}
        </h3>
        <SessionStatusChip state={session.opState} closed={closed} />
      </div>

      <div className="mt-2.5 flex items-center gap-2 text-xs text-ink-muted">
        <span className="rounded-md bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-accent">
          {session.backend}
        </span>
        <GitBranch className="size-3.5 opacity-70" />
        <span className="truncate font-mono">{shortPath(session.workspace.path)}</span>
        <span className="opacity-40">·</span>
        <span className="shrink-0">{session.workspace.targetId}</span>
      </div>

      {session.lastSummary && (
        <div className="mt-2.5 flex items-center gap-1.5">
          <p className="min-w-0 flex-1 truncate text-[13.5px] leading-snug text-ink-soft">
            {session.lastSummary}
          </p>
          <ChevronRight className="size-4 shrink-0 text-ink-muted transition-transform group-hover:translate-x-0.5" />
        </div>
      )}
    </Link>
  );
}
