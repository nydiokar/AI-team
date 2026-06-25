/**
 * Active session screen — compact header with live status + full action menu,
 * session info panel, real conversation timeline, and the live composer.
 *
 * Action menu covers: Stop · Close/Restore · Compact · Change model · Git
 * All parity with the Telegram command surface on a single session.
 */
import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  ChevronLeft,
  MoreVertical,
  Square,
  Archive,
  RotateCcw,
  Minimize2,
  Sliders,
  GitBranch,
} from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SessionStatusChip } from "../components/ui/StatusChip";
import { SessionTimeline } from "../components/timeline/SessionTimeline";
import { Composer } from "../components/timeline/Composer";
import { SessionInfoPanel } from "../components/sessions/SessionInfoPanel";
import { ModelPickerSheet } from "../components/sessions/ModelPickerSheet";
import { GitPanelSheet } from "../components/sessions/GitPanelSheet";
import { useSessions, useApprovals, useSessionMessages } from "../hooks/useLiveData";
import { useSessionTimeline } from "../hooks/useSessionTimeline";
import {
  useStopSession,
  useCloseSession,
  useRestoreSession,
  useCompactSession,
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
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [gitPanelOpen, setGitPanelOpen] = useState(false);
  const [compactBanner, setCompactBanner] = useState<string | null>(null);

  const stop = useStopSession();
  const close = useCloseSession();
  const restore = useRestoreSession();
  const compact = useCompactSession();

  // Auto-dismiss compact result banner after 4 s.
  useEffect(() => {
    if (!compactBanner) return;
    const t = setTimeout(() => setCompactBanner(null), 4000);
    return () => clearTimeout(t);
  }, [compactBanner]);

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
                    <div className="card-elev absolute right-0 z-50 mt-1 w-48 overflow-hidden rounded-xl py-1 text-[13px]">
                      {running && (
                        <button
                          onClick={() => act(() => id && stop.mutate(id))}
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-warn hover:bg-surface-2"
                        >
                          <Square className="size-4" /> Stop task
                        </button>
                      )}
                      {!closed && !running && (
                        <button
                          onClick={() =>
                            act(() =>
                              id &&
                              compact.mutate(id, {
                                onSuccess: (r) =>
                                  setCompactBanner(
                                    r.ok
                                      ? "Context compacted."
                                      : `Compaction failed: ${r.errors?.[0] ?? "unknown"}`,
                                  ),
                                onError: (e) =>
                                  setCompactBanner(`Compaction failed: ${String(e.message)}`),
                              }),
                            )
                          }
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-soft hover:bg-surface-2"
                        >
                          <Minimize2 className="size-4" /> Compact context
                        </button>
                      )}
                      {!closed && (
                        <button
                          onClick={() => act(() => setModelPickerOpen(true))}
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-soft hover:bg-surface-2"
                        >
                          <Sliders className="size-4" /> Change model
                        </button>
                      )}
                      {!closed && (
                        <button
                          onClick={() => act(() => setGitPanelOpen(true))}
                          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-soft hover:bg-surface-2"
                        >
                          <GitBranch className="size-4" /> Git
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

      {/* Compact result banner */}
      {compactBanner && (
        <div className="border-b border-hairline bg-surface-1 px-4 py-2 text-[12px] text-ink-soft">
          {compactBanner}
        </div>
      )}

      {/* Session info panel (expandable, lazy dirs fetch) */}
      {session && id && <SessionInfoPanel session={session} sessionId={id} />}

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

      {/* Sheets */}
      {modelPickerOpen && session && id && (
        <ModelPickerSheet
          sessionId={id}
          currentModel={session.model}
          backend={session.backend}
          onClose={() => setModelPickerOpen(false)}
        />
      )}
      {gitPanelOpen && id && (
        <GitPanelSheet sessionId={id} onClose={() => setGitPanelOpen(false)} />
      )}
    </div>
  );
}
