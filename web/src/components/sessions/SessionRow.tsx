import { Link } from "react-router-dom";
import { Briefcase, Clock, FileCode2, GitBranch, Pencil } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import type { Session } from "../../domain/models";
import type { SessionAffiliation } from "../../domain/work";
import { SessionStatusChip } from "../ui/StatusChip";
import { api } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";
import { useDraftStore } from "../../stores/draftStore";
import { isClosedCaseStatus, roleLabel } from "../../lib/workPresentation";
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

export function SessionRow({
  session,
  affiliation,
}: {
  session: Session;
  /** Authoritative case membership from the Work read model; undefined ⇒ shown
   *  as standalone (no chip). Never inferred client-side. */
  affiliation?: SessionAffiliation;
}) {
  const closed = session.lifecycle === "closed";
  const proj = projectName(session.workspace.path);
  const age = relativeTime(session.updatedAt);
  const model = session.model ?? session.defaultModel;
  const fileCount = session.lastFilesModified.length;
  const caseClosed = affiliation ? isClosedCaseStatus(affiliation.caseStatus) : false;

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
        "card-elev block rounded-xl px-3.5 py-3 transition-transform active:scale-[0.99]",
        session.needsAttention && "attention-glow",
        closed && "opacity-55",
      )}
    >
      {/* Project and operational state are the primary scan targets. */}
      <div className="flex items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-tight text-ink">
          {proj}
        </h3>
        <SessionStatusChip state={session.opState} closed={closed} />
      </div>

      {/* Compact runtime context: backend, selected model, target, and recency. */}
      <div className="mt-1.5 flex min-w-0 items-center gap-1.5 text-[11px] text-ink-muted">
        <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-accent">
          {session.backend}
        </span>
        {model && <span className="shrink-0 truncate font-mono text-ink-soft">{model}</span>}
        <span className="min-w-0 truncate text-ink-muted">·</span>
        <GitBranch className="size-3 shrink-0 opacity-50" />
        <span className="min-w-0 truncate font-mono">{session.workspace.targetId}</span>
        {age && (
          <span className="ml-auto inline-flex shrink-0 items-center gap-1 pl-1">
            <Clock className="size-3 opacity-50" />
            {age}
          </span>
        )}
      </div>

      {/* The useful human context gets the remaining visual weight. */}
      {draft ? (
        <p className="mt-2 flex min-w-0 items-center gap-1 text-[13px] leading-snug text-ink-soft">
          <Pencil className="size-3 shrink-0 text-accent" />
          <span className="shrink-0 font-medium text-accent">Draft:</span>
          <span className="truncate">{draft}</span>
        </p>
      ) : session.lastSummary ? (
        <p className="mt-2 overflow-hidden text-[14px] leading-5 text-ink [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:3]">
          {session.lastSummary}
        </p>
      ) : (
        <p className="mt-2 text-[13px] leading-snug text-ink-muted italic">No activity yet</p>
      )}

      {/* Case context and changed-file count are useful only when they exist. */}
      {(affiliation || fileCount > 0) && (
        <div className="mt-1.5 flex min-w-0 items-center gap-2 text-[11px]">
          {affiliation && (
            <span
              className={cn(
                "inline-flex min-w-0 items-center gap-1 rounded-full px-1.5 py-0.5",
                caseClosed ? "bg-surface-3/70 text-ink-soft" : "bg-accent-dim/50 text-accent",
              )}
            >
              <Briefcase className="size-3 shrink-0" />
              <span className="shrink-0 font-medium">{roleLabel(affiliation.role)}</span>
              <span className="min-w-0 truncate">· {affiliation.caseTitle}</span>
            </span>
          )}
          {fileCount > 0 && (
            <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-ink-muted">
              <FileCode2 className="size-3" />
              {fileCount} {fileCount === 1 ? "file" : "files"}
            </span>
          )}
        </div>
      )}
    </Link>
  );
}
