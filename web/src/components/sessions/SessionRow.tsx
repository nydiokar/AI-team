import { Link } from "react-router-dom";
import { ChevronRight, GitBranch, Clock, Pencil } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import type { Session } from "../../domain/models";
import { SessionStatusChip } from "../ui/StatusChip";
import { api } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";
import { useDraftStore } from "../../stores/draftStore";
import { cn } from "../../lib/cn";

/** Extract just the project/repo name from any path. */
function projectName(p: string): string {
  const parts = p.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || p;
}

/** Relative time label — "just now", "5m ago", "2h ago", "Jun 24". */
function relativeTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function SessionRow({ session }: { session: Session }) {
  const closed = session.lifecycle === "closed";
  const proj = projectName(session.workspace.path);
  const age = relativeTime(session.updatedAt);

  const qc = useQueryClient();
  const token = useAuthStore((s) => s.token);
  // An unsent instruction still sitting in this session's composer. Surfaced in
  // the overview (Telegram-style) so you know there's text waiting before you
  // open it — the whole point of persisting the draft.
  const draft = useDraftStore((s) => s.bySession[session.id]);

  // Warm the conversation before navigation so the detail screen renders the
  // chat immediately instead of flashing a spinner. Fires on pointerdown (before
  // the click that navigates); matches the useSessionMessages query key exactly.
  const prefetch = () => {
    if (!token) return;
    qc.prefetchQuery({
      queryKey: ["session-messages", session.id],
      queryFn: () => api.sessionMessages(token, session.id),
    });
  };

  return (
    <Link
      to={`/sessions/${session.id}`}
      onPointerDown={prefetch}
      className={cn(
        "card-elev group block rounded-2xl px-4 py-4 transition-transform active:scale-[0.99]",
        session.needsAttention && "attention-glow",
        closed && "opacity-55",
      )}
    >
      {/* Row 1: project name + status chip */}
      <div className="flex items-center gap-2.5">
        <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-tight text-ink">
          {proj}
        </h3>
        <SessionStatusChip state={session.opState} closed={closed} />
      </div>

      {/* Row 2: backend · target · timestamp */}
      <div className="mt-2 flex items-center gap-2 text-xs text-ink-muted">
        <span className="rounded-md bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-accent">
          {session.backend}
        </span>
        <GitBranch className="size-3 opacity-50" />
        <span className="truncate font-mono text-[11px]">{session.workspace.targetId}</span>
        {age && (
          <>
            <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px]">
              <Clock className="size-3 opacity-50" />
              {age}
            </span>
          </>
        )}
      </div>

      {/* Row 3: last activity summary. The session hash is detail for the opened
          view, not the overview — keeping it here was machine noise. */}
      <div className="mt-2.5 flex items-start gap-1.5">
        <div className="min-w-0 flex-1">
          {draft ? (
            <p className="flex min-w-0 items-center gap-1 text-[13px] leading-snug text-ink-soft">
              <Pencil className="size-3 shrink-0 text-accent" />
              <span className="shrink-0 font-medium text-accent">Draft:</span>
              <span className="truncate">{draft}</span>
            </p>
          ) : session.lastSummary ? (
            <p className="truncate text-[13px] leading-snug text-ink-soft">
              {session.lastSummary}
            </p>
          ) : (
            <p className="text-[13px] leading-snug text-ink-muted italic">No activity yet</p>
          )}
        </div>
        <ChevronRight className="mt-0.5 size-4 shrink-0 text-ink-muted transition-transform group-hover:translate-x-0.5" />
      </div>
    </Link>
  );
}
