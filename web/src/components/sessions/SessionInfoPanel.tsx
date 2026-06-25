/**
 * Session info panel — parity with Telegram /session_status.
 * Shows model, machine, and project dirs. Expandable; dirs are lazy-fetched
 * on first expand via POST /api/sessions/{id}/inspect (list_dirs).
 */
import { useState } from "react";
import { ChevronDown, FolderOpen } from "lucide-react";
import { useInspectSession } from "../../hooks/useSessionActions";
import type { Session } from "../../domain/models";
import { cn } from "../../lib/cn";

interface Props {
  session: Session;
  sessionId: string;
}

interface DirsResult {
  dirs?: string[];
  path?: string;
  error?: string;
}

export function SessionInfoPanel({ session, sessionId }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [dirsPath, setDirsPath] = useState<string>("");
  const inspect = useInspectSession();

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && dirs === null) {
      inspect.mutate(
        { sessionId, op: "list_dirs", params: { limit: 12, sort_by_recent: true } },
        {
          onSuccess: (r) => {
            const res = r as DirsResult;
            setDirs(res.dirs ?? []);
            setDirsPath(res.path ?? "");
          },
        },
      );
    }
  };

  return (
    <div className="mx-4 mb-2">
      <button
        onClick={toggle}
        className="flex w-full items-center gap-1.5 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted hover:text-ink-soft"
        aria-expanded={expanded}
      >
        <ChevronDown
          className={cn("size-3.5 transition-transform", expanded && "rotate-180")}
        />
        Session info
      </button>

      {expanded && (
        <div className="rounded-xl border border-hairline bg-surface-1 px-4 py-3 text-[13px] space-y-1.5">
          <InfoRow label="Backend" value={session.backend} mono />
          <InfoRow label="Model" value={session.model ?? "(backend default)"} mono />
          <InfoRow label="Machine" value={session.workspace.targetId} mono />
          <InfoRow label="Path" value={session.workspace.path} mono />
          {session.lastTaskId && (
            <InfoRow label="Last task" value={session.lastTaskId} mono />
          )}

          {/* Dirs */}
          <div className="pt-1">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
              Dirs {dirsPath ? `in ${dirsPath.split("/").pop() ?? dirsPath}` : ""}
            </span>
            {inspect.isPending && dirs === null && (
              <p className="mt-1 text-ink-muted">Loading…</p>
            )}
            {dirs !== null && dirs.length === 0 && (
              <p className="mt-1 text-ink-muted">No subdirectories found.</p>
            )}
            {dirs !== null && dirs.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {dirs.map((d) => (
                  <li key={d} className="flex items-center gap-1.5 font-mono text-[11px] text-ink-soft">
                    <FolderOpen className="size-3 text-ink-muted" />
                    {d.split("/").pop() ?? d}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function InfoRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-20 shrink-0 text-[11px] text-ink-muted">{label}</span>
      <span className={cn("min-w-0 flex-1 truncate text-ink", mono && "font-mono text-[12px]")}>
        {value}
      </span>
    </div>
  );
}
