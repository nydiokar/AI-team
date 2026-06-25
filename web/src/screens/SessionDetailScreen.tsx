/**
 * Active session screen (spec §7.2) — compact header with live status + actions,
 * the real conversation timeline, and the live composer.
 *
 * Timeline now binds the server-reconstructed transcript (useSessionMessages) so
 * a Telegram-started session shows its ACTUAL messages, not "No activity yet".
 * Header carries the session controls the UI was missing: Stop (while running),
 * Close / Restore — ported from the Telegram command surface.
 */
import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ChevronLeft, MoreVertical, Square, Archive, RotateCcw } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SessionStatusChip } from "../components/ui/StatusChip";
import { SessionTimeline } from "../components/timeline/SessionTimeline";
import { Composer } from "../components/timeline/Composer";
import { useSessions, useApprovals, useSessionMessages } from "../hooks/useLiveData";
import { useSessionTimeline } from "../hooks/useSessionTimeline";
import {
  useStopSession,
  useCloseSession,
  useRestoreSession,
} from "../hooks/useSessionActions";

export function SessionDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data } = useSessions();
  const session = data?.find((s) => s.id === id);
  const { data: turns } = useSessionMessages(id);
  const { data: approvals } = useApprovals();
  const timeline = useSessionTimeline(id, session, turns ?? [], approvals ?? []);
  const running = session?.opState === "running";
  const closed = session?.lifecycle === "closed";

  const [menuOpen, setMenuOpen] = useState(false);
  const stop = useStopSession();
  const close = useCloseSession();
  const restore = useRestoreSession();

  const act = (fn: () => void) => {
    setMenuOpen(false);
    fn();
  };

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
            <div className="flex items-center gap-1.5">
              <SessionStatusChip state={session.opState} closed={closed} />
              <div className="relative">
                <button
                  onClick={() => setMenuOpen((v) => !v)}
                  className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
                  aria-label="Session actions"
                  aria-expanded={menuOpen}
                >
                  <MoreVertical className="size-5" />
                </button>
                {menuOpen && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setMenuOpen(false)} />
                    <div className="card-elev absolute right-0 z-50 mt-1 w-44 overflow-hidden rounded-xl py-1 text-[13px]">
                      {running && (
                        <button
                          onClick={() => act(() => id && stop.mutate(id))}
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-warn hover:bg-surface-2"
                        >
                          <Square className="size-4" /> Stop task
                        </button>
                      )}
                      {!closed ? (
                        <button
                          onClick={() => act(() => id && close.mutate(id))}
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-soft hover:bg-surface-2"
                        >
                          <Archive className="size-4" /> Close session
                        </button>
                      ) : (
                        <button
                          onClick={() => act(() => id && restore.mutate(id))}
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-soft hover:bg-surface-2"
                        >
                          <RotateCcw className="size-4" /> Restore session
                        </button>
                      )}
                    </div>
                  </>
                )}
              </div>
            </div>
          )
        }
      />

      <div className="flex-1 overflow-y-auto overscroll-contain">
        {timeline.length > 0 ? (
          <SessionTimeline items={timeline} />
        ) : (
          <p className="px-4 py-10 text-center text-sm text-ink-muted">
            No messages yet. Send an instruction below to start.
          </p>
        )}
      </div>

      {id && !closed && <Composer sessionId={id} running={running} />}
    </div>
  );
}
