/**
 * Active session screen (spec §7.2) — compact header with live status, the
 * chronological timeline, and the live composer.
 *
 * UI-2: header + timeline + composer all LIVE. Timeline is assembled from the
 * three real sources (optimistic user msgs · this session's SSE notices/task-
 * state · polled turn summary) via useSessionTimeline — no fixtures. Composer
 * sends through Move F's write surface; Stop appears while a task runs.
 */
import { useParams, useNavigate } from "react-router-dom";
import { ChevronLeft } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SessionStatusChip } from "../components/ui/StatusChip";
import { SessionTimeline } from "../components/timeline/SessionTimeline";
import { Composer } from "../components/timeline/Composer";
import { useSessions } from "../hooks/useLiveData";
import { useEventStreamContext } from "../hooks/eventStreamContext";
import { useSessionTimeline } from "../hooks/useSessionTimeline";

export function SessionDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data } = useSessions();
  const session = data?.find((s) => s.id === id);
  const { events } = useEventStreamContext();
  const timeline = useSessionTimeline(id, session, events);
  const running = session?.opState === "running";

  return (
    <div className="mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <CompactTopBar
        title={session?.id ?? id ?? "Session"}
        subtitle={
          session ? (
            <span className="font-mono">
              {session.backend} · {session.workspace.targetId}
            </span>
          ) : (
            "loading…"
          )
        }
        left={
          <button
            onClick={() => navigate("/sessions")}
            className="-ml-1 flex size-9 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Back to sessions"
          >
            <ChevronLeft className="size-5" />
          </button>
        }
        right={
          session && (
            <SessionStatusChip state={session.opState} closed={session.lifecycle === "closed"} />
          )
        }
      />

      <div className="flex-1 overflow-y-auto overscroll-contain">
        {timeline.length > 0 ? (
          <SessionTimeline items={timeline} />
        ) : (
          <p className="px-4 py-10 text-center text-sm text-ink-muted">
            No activity yet. Send an instruction below to start.
          </p>
        )}
      </div>

      {id && session?.lifecycle !== "closed" && (
        <Composer sessionId={id} running={running} />
      )}
    </div>
  );
}
