import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import {
  ChevronLeft,
  MoreVertical,
  Square,
  Archive,
  RotateCcw,
  Minimize2,
  Sliders,
  GitBranch,
  Bot,
  Loader2,
  FolderGit2,
  Info,
  Activity,
  FilePlus2,
  FilePen,
  FileMinus2,
  ChevronDown,
  MessagesSquare,
} from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SessionStatusChip, TaskStatusChip } from "../components/ui/StatusChip";
import { SessionTimeline } from "../components/timeline/SessionTimeline";
import { SessionTurns } from "../components/timeline/SessionTurns";
import { Composer } from "../components/timeline/Composer";
import { ModelPickerSheet } from "../components/sessions/ModelPickerSheet";
import { GitPanelSheet } from "../components/sessions/GitPanelSheet";
import { useSessions, useApprovals, useSessionMessages, useArtifacts, useArtifact, useSessionTurns } from "../hooks/useLiveData";
import { useSessionTimeline } from "../hooks/useSessionTimeline";
import {
  useStopSession,
  useCloseSession,
  useRestoreSession,
  useCompactSession,
  useInspectSession,
} from "../hooks/useSessionActions";
import { useActivityLog } from "../hooks/useActivityLog";
import { cn } from "../lib/cn";
import { clockLabel } from "../lib/time";
import type { Artifact, RemoteFile } from "../domain/models";
import type { TaskState } from "../domain/status";
import type { LogLine } from "../transport/eventLog";

type SessionTab = "chat" | "files" | "info";

function projectName(p: string): string {
  const parts = p.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || p;
}

/** Display the running model — the explicit one, or which model is the default. */
function modelLabel(model: string | null, defaultModel: string | null): string {
  if (model) return model;
  if (defaultModel) return `${defaultModel} (default)`;
  return "(backend default)";
}

// ── Session-scoped Files tab ──────────────────────────────────────────────────

const CHANGE_ICON = {
  added: FilePlus2,
  modified: FilePen,
  deleted: FileMinus2,
} as const;

const CHANGE_COLOR = {
  added: "text-ok",
  modified: "text-warn",
  deleted: "text-bad",
} as const;

function ArtifactRow({ artifact }: { artifact: Artifact }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useArtifact(open ? (artifact.taskId ?? artifact.id) : null);

  const dateStr = artifact.createdAt
    ? new Date(artifact.createdAt).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  const taskShort = artifact.name.startsWith("task_")
    ? `#${artifact.name.slice(5, 13)}`
    : artifact.name;

  return (
    <div className="border-b border-hairline last:border-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <ChevronDown
          className={cn("size-3.5 shrink-0 text-ink-muted transition-transform", !open && "-rotate-90")}
        />
        <div className="min-w-0 flex-1">
          <p className="font-mono text-[12.5px] font-medium text-ink">{taskShort}</p>
          {dateStr && <p className="text-[11px] text-ink-muted">{dateStr}</p>}
        </div>
      </button>
      {open && (
        <div className="px-4 pb-3">
          {isLoading && <p className="text-xs text-ink-muted">Loading…</p>}
          {data && data.files.length === 0 && (
            <p className="text-xs text-ink-muted">No files changed.</p>
          )}
          {data && data.files.map((f: RemoteFile, i: number) => {
            const Icon = CHANGE_ICON[f.change];
            const col = CHANGE_COLOR[f.change];
            const filename = f.path.split(/[/\\]/).pop() ?? f.path;
            const dir = f.path.slice(0, f.path.length - filename.length).replace(/[/\\]$/, "");
            const lc = data.lineCounts[i];
            return (
              <div key={f.path} className="flex items-start gap-2 py-1.5">
                <Icon className={cn("mt-0.5 size-3.5 shrink-0", col)} />
                <div className="min-w-0 flex-1">
                  <span className="font-mono text-[12px] text-ink">{filename}</span>
                  {dir && <p className="truncate font-mono text-[10px] text-ink-muted">{dir}</p>}
                </div>
                {(lc?.added != null || lc?.deleted != null) && (
                  <span className="shrink-0 font-mono text-[11px] tabular-nums">
                    {lc.added != null && <span className="text-ok">+{lc.added}</span>}
                    {lc.deleted != null && <span className="ml-1 text-bad">−{lc.deleted}</span>}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SessionFilesTab({ sessionId }: { sessionId: string }) {
  const { data: allArtifacts, isLoading } = useArtifacts(50);
  const sessionArtifacts = (allArtifacts ?? []).filter(
    (a) => a.sessionId === sessionId,
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-ink-muted">
        <Loader2 className="size-5 animate-spin" />
      </div>
    );
  }

  if (sessionArtifacts.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 px-6 py-20 text-center">
        <div className="flex size-12 items-center justify-center rounded-2xl bg-surface-1 ring-1 ring-hairline">
          <FolderGit2 className="size-6 text-ink-muted" />
        </div>
        <div>
          <p className="text-[14px] font-medium text-ink-soft">No file changes yet</p>
          <p className="mt-1 text-sm text-ink-muted">Files modified by tasks will appear here.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="card-elev mx-4 my-4 overflow-hidden rounded-xl divide-y divide-hairline">
      {sessionArtifacts.map((a) => (
        <ArtifactRow key={a.id} artifact={a} />
      ))}
    </div>
  );
}

// ── Session Info tab ──────────────────────────────────────────────────────────

const STATE_KIND_LABEL: Record<string, string> = {
  task: "Task",
  run: "Run",
  approval: "Approval",
  artifact_written: "Artifact",
  approval_requested: "Approval",
};
const TASK_STATES: ReadonlySet<string> = new Set([
  "queued",
  "dispatching",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
  "succeeded",
  "failed",
  "cancelled",
  "connection_unknown",
]);

function SessionStateRow({ line }: { line: LogLine }) {
  const label = STATE_KIND_LABEL[line.kind] ?? line.kind.replace(/[_.]+/g, " ");
  const isTask = line.kind === "task";
  const rawState = isTask ? line.text.replace(/^task\s+/, "").replace(/\s+/g, "_") : "";
  const state = TASK_STATES.has(rawState) ? (rawState as TaskState) : null;
  return (
    <div className="flex items-center gap-2.5 px-4 py-2.5">
      <span
        className={cn(
          "size-1.5 shrink-0 rounded-full",
          line.severity === "error"
            ? "bg-bad"
            : line.severity === "warning"
              ? "bg-warn"
              : line.severity === "success"
                ? "bg-ok"
                : "bg-accent",
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-[11px] font-medium text-ink-muted">{label}</span>
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-soft">{line.text}</span>
        </div>
        {line.taskId && (
          <div className="mt-0.5 truncate font-mono text-[10.5px] text-ink-muted">{line.taskId}</div>
        )}
      </div>
      {state ? (
        <TaskStatusChip state={state} />
      ) : (
        <span className="shrink-0 text-[10.5px] tabular-nums text-ink-muted">{clockLabel(line.at)}</span>
      )}
    </div>
  );
}

function SessionStateSequence({ sessionId }: { sessionId: string }) {
  const { lines } = useActivityLog({ sessionId, includeSessionActivity: true });
  const scoped = lines.slice(0, 12).reverse();

  return (
    <div>
      <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
        <Activity className="size-3" />
        State
      </p>
      <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
        {scoped.length === 0 ? (
          <div className="px-4 py-3 text-[12px] text-ink-muted">No live state events yet.</div>
        ) : (
          scoped.map((line) => <SessionStateRow key={line.id} line={line} />)
        )}
      </div>
    </div>
  );
}

function SessionInfoTab({ sessionId }: { sessionId: string }) {
  const { data: sessions } = useSessions();
  const session = sessions?.find((s) => s.id === sessionId);
  const [dirs, setDirs] = useState<string[] | null>(null);
  const inspect = useInspectSession();
  const { data: turns, isLoading: turnsLoading } = useSessionTurns(sessionId);

  useEffect(() => {
    inspect.mutate(
      { sessionId, op: "list_dirs", params: { limit: 12, sort_by_recent: true } },
      {
        onSuccess: (r) => {
          const res = r as { dirs?: string[] };
          setDirs(res.dirs ?? []);
        },
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  if (!session) return null;

  const rows = [
    { label: "Session ID", value: session.id },
    { label: "Backend session", value: session.backendSessionId ?? "(not yet captured)" },
    { label: "Backend", value: session.backend },
    { label: "Model", value: modelLabel(session.model, session.defaultModel) },
    { label: "Machine", value: session.workspace.targetId },
    { label: "Path", value: session.workspace.path },
    ...(session.lastTaskId ? [{ label: "Last task", value: session.lastTaskId }] : []),
  ];

  return (
    <div className="px-4 py-4 space-y-4">
      <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
        {rows.map(({ label, value }) => (
          <div key={label} className="flex items-start gap-3 px-4 py-3">
            <span className="w-20 shrink-0 text-[11px] text-ink-muted pt-0.5">{label}</span>
            <span className="min-w-0 flex-1 break-all font-mono text-[12px] text-ink">{value}</span>
          </div>
        ))}
      </div>

      <SessionTurns turns={turns ?? []} loading={turnsLoading} />

      <SessionStateSequence sessionId={sessionId} />

      {dirs !== null && dirs.length > 0 && (
        <div>
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
            Subdirectories
          </p>
          <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
            {dirs.map((d) => (
              <div key={d} className="px-4 py-2.5 font-mono text-[12px] text-ink-soft">
                {d.split("/").pop() ?? d}
              </div>
            ))}
          </div>
        </div>
      )}

      {inspect.isPending && dirs === null && (
        <p className="text-center text-sm text-ink-muted">Loading directories…</p>
      )}
    </div>
  );
}

// ── Main Screen ───────────────────────────────────────────────────────────────

export function SessionDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data, isLoading: sessionsLoading } = useSessions();
  const session = data?.find((s) => s.id === id);
  const {
    data: turns,
    isLoading: messagesLoading,
    isFetched: messagesFetched,
    isError: messagesError,
    fetchStatus: messagesFetchStatus,
  } = useSessionMessages(id);
  const { data: approvals } = useApprovals();
  const timeline = useSessionTimeline(id, session, turns ?? [], approvals ?? []);
  const running = session?.opState === "running";
  const closed = session?.lifecycle === "closed";
  // Only treat as "loading" on the very first fetch — subsequent polls use
  // placeholderData so they never wipe the existing conversation.
  const loading = sessionsLoading || (messagesLoading && !messagesFetched);
  // We're showing turns but the live poll can't reach the backend (error, or
  // paused while offline). Warn that what's on screen may be stale rather than
  // letting persisted/cached turns look confidently current.
  const messagesStale =
    messagesFetched &&
    (messagesError || messagesFetchStatus === "paused") &&
    !loading;

  // Deep-link target: the Activity feed sends ?tab=files for an artifact event,
  // so the tap lands where that event's content actually lives (not an empty
  // Chat). Defaults to chat.
  const [searchParams] = useSearchParams();
  const initialTab = ((): SessionTab => {
    const t = searchParams.get("tab");
    return t === "files" || t === "info" ? t : "chat";
  })();
  const [tab, setTab] = useState<SessionTab>(initialTab);
  const [menuOpen, setMenuOpen] = useState(false);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [gitPanelOpen, setGitPanelOpen] = useState(false);
  const [compactConfirm, setCompactConfirm] = useState(false);
  const [compactBanner, setCompactBanner] = useState<string | null>(null);

  const timelineRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLDivElement>(null);
  const [headerHidden, setHeaderHidden] = useState(false);
  const lastScrollYRef = useRef(0);

  const handleHeaderScroll = useCallback(() => {
    const el = timelineRef.current;
    if (!el) return;
    const scrollY = el.scrollTop;
    if (scrollY > 40 && scrollY > lastScrollYRef.current + 8) {
      setHeaderHidden(true);
    } else if (scrollY < lastScrollYRef.current - 8 || scrollY < 20) {
      setHeaderHidden(false);
    }
    lastScrollYRef.current = scrollY;
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    bottomRef.current?.scrollIntoView({ behavior, block: "end" });
  }, []);

  const prevLengthRef = useRef(0);
  useEffect(() => {
    const el = timelineRef.current;
    if (!el || tab !== "chat") return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    const grew = timeline.length > prevLengthRef.current;
    prevLengthRef.current = timeline.length;
    if (grew && nearBottom) scrollToBottom();
  }, [timeline.length, scrollToBottom, tab]);

  useEffect(() => {
    if (!loading && timeline.length > 0 && tab === "chat") {
      scrollToBottom("instant" as ScrollBehavior);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading]);

  const stop = useStopSession();
  const close = useCloseSession();
  const restore = useRestoreSession();
  const compact = useCompactSession();

  useEffect(() => {
    if (!compactBanner) return;
    const t = setTimeout(() => setCompactBanner(null), 4000);
    return () => clearTimeout(t);
  }, [compactBanner]);

  const act = (fn: () => void) => { setMenuOpen(false); fn(); };

  const proj = session ? projectName(session.workspace.path) : null;

  const TABS: { key: SessionTab; label: string; Icon: React.ElementType }[] = [
    { key: "chat", label: "Chat", Icon: MessagesSquare },
    { key: "files", label: "Files", Icon: FolderGit2 },
    { key: "info", label: "Info", Icon: Info },
  ];

  return (
    <div className="mx-auto flex h-full max-w-[480px] flex-col bg-base">
      {/* ── On non-chat tabs, header is outside scroll ── */}
      {tab !== "chat" && (
        <>
          <CompactTopBar
            title={proj ?? session?.id ?? id ?? "Session"}
            subtitle={
              session ? (
                <span className="font-mono text-[11px] text-ink-muted">
                  {session.backend} · {modelLabel(session.model, session.defaultModel)}
                </span>
              ) : loading ? (
                <span className="text-[11px] text-ink-muted">loading…</span>
              ) : undefined
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
              session ? (
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
                        <div className="card-elev absolute right-0 z-50 mt-1 w-52 overflow-hidden rounded-xl py-1 text-[13px] shadow-xl">
                          {TABS.map(({ key, label, Icon }) => (
                            <button
                              key={key}
                              onClick={() => act(() => setTab(key))}
                              className={cn(
                                "flex w-full items-center gap-2.5 px-3.5 py-2.5 hover:bg-surface-2",
                                tab === key ? "text-accent" : "text-ink-soft",
                              )}
                            >
                              <Icon className="size-4" /> {label}
                              {tab === key && <span className="ml-auto text-[11px]">●</span>}
                            </button>
                          ))}
                          <div className="my-1 border-t border-hairline" />
                          {running && (
                            <button
                              onClick={() => act(() => id && stop.mutate(id))}
                              className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-bad hover:bg-surface-2"
                            >
                              <Square className="size-4" /> Stop task
                            </button>
                          )}
                          {!closed && !running && (
                            <button
                              onClick={() => act(() => setCompactConfirm(true))}
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
                          <div className="my-1 border-t border-hairline" />
                          {!closed ? (
                            <button
                              onClick={() => act(() => id && close.mutate(id))}
                              className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-muted hover:bg-surface-2"
                            >
                              <Archive className="size-4" /> Close session
                            </button>
                          ) : (
                            <button
                              onClick={() => act(() => id && restore.mutate(id))}
                              className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-muted hover:bg-surface-2"
                            >
                              <RotateCcw className="size-4" /> Restore session
                            </button>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                </div>
              ) : null
            }
          />
          <button
            onClick={() => setTab("chat")}
            className="flex items-center gap-2 border-b border-hairline bg-base/80 px-4 py-2.5 text-[12px] font-medium text-ink-soft backdrop-blur-sm hover:bg-surface-2"
          >
            <ChevronLeft className="size-4" />
            <span>{TABS.find((t) => t.key === tab)?.label}</span>
            <span className="ml-auto text-[11px] text-ink-muted">Back to chat</span>
          </button>
        </>
      )}

      {/* ── Chat tab: header + timeline in scroll, composer pinned outside ── */}
      {tab === "chat" && (
        <div className="flex flex-1 flex-col overflow-hidden">
        <div
          ref={timelineRef}
          className="flex-1 overflow-y-auto overscroll-contain"
          onScroll={handleHeaderScroll}
        >
          {/* Sticky header inside scroll — hides on scroll down */}
          <div
            ref={headerRef}
            className={`sticky top-0 z-20 transition-transform duration-300 will-change-transform ${headerHidden ? "-translate-y-full" : ""}`}
          >
            <CompactTopBar
              title={proj ?? session?.id ?? id ?? "Session"}
              subtitle={
                session ? (
                  <span className="font-mono text-[11px] text-ink-muted">
                    {session.backend} · {modelLabel(session.model, session.defaultModel)}
                  </span>
                ) : loading ? (
                  <span className="text-[11px] text-ink-muted">loading…</span>
                ) : undefined
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
                session ? (
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
                          <div className="card-elev absolute right-0 z-50 mt-1 w-52 overflow-hidden rounded-xl py-1 text-[13px] shadow-xl">
                            {TABS.map(({ key, label, Icon }) => (
                              <button
                                key={key}
                                onClick={() => act(() => setTab(key))}
                                className={cn(
                                  "flex w-full items-center gap-2.5 px-3.5 py-2.5 hover:bg-surface-2",
                                  tab === key ? "text-accent" : "text-ink-soft",
                                )}
                              >
                                <Icon className="size-4" /> {label}
                                {tab === key && <span className="ml-auto text-[11px]">●</span>}
                              </button>
                            ))}
                            <div className="my-1 border-t border-hairline" />
                            {running && (
                              <button
                                onClick={() => act(() => id && stop.mutate(id))}
                                className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-bad hover:bg-surface-2"
                              >
                                <Square className="size-4" /> Stop task
                              </button>
                            )}
                            {!closed && !running && (
                              <button
                                onClick={() => act(() => setCompactConfirm(true))}
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
                            <div className="my-1 border-t border-hairline" />
                            {!closed ? (
                              <button
                                onClick={() => act(() => id && close.mutate(id))}
                                className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-muted hover:bg-surface-2"
                              >
                                <Archive className="size-4" /> Close session
                              </button>
                            ) : (
                              <button
                                onClick={() => act(() => id && restore.mutate(id))}
                                className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-ink-muted hover:bg-surface-2"
                              >
                                <RotateCcw className="size-4" /> Restore session
                              </button>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                ) : null
              }
            />
          </div>

          {compactBanner && (
            <div className="border-b border-hairline bg-surface-1 px-4 py-2 text-[12px] text-ink-soft">
              {compactBanner}
            </div>
          )}

          {messagesStale && (
            <div className="border-b border-hairline bg-warn-dim/40 px-4 py-2 text-[12px] text-warn">
              Reconnecting… showing the last loaded messages.
            </div>
          )}

          <div>
            {loading && timeline.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-3 py-20 text-ink-muted">
                <Loader2 className="size-6 animate-spin" />
                <p className="text-sm">Loading conversation…</p>
              </div>
            ) : timeline.length > 0 ? (
              <>
                <SessionTimeline items={timeline} />
                <div ref={bottomRef} className="h-px" />
              </>
            ) : (
              <div className="flex flex-col items-center justify-center gap-3 px-6 py-20 text-center">
                <div className="flex size-12 items-center justify-center rounded-2xl bg-surface-1 ring-1 ring-hairline">
                  <Bot className="size-6 text-ink-muted" />
                </div>
                <div>
                  <p className="text-[14px] font-medium text-ink-soft">Session ready</p>
                  <p className="mt-1 text-sm text-ink-muted">Send an instruction to start.</p>
                </div>
              </div>
            )}
          </div>

        </div>

        {/* Composer pinned outside the scroll container so it always sits at the true bottom */}
        {id && !closed ? (
          <Composer sessionId={id} running={running} />
        ) : (
          <div className="border-t border-hairline bg-surface-1/70 px-4 py-3 text-center text-[12px] text-ink-muted">
            Session closed · open the menu to restore
          </div>
        )}
        </div>
      )}

      {tab === "files" && id && (
        <div className="flex-1 overflow-y-auto overscroll-contain">
          <SessionFilesTab sessionId={id} />
        </div>
      )}

      {tab === "info" && id && (
        <div className="flex-1 overflow-y-auto overscroll-contain">
          <SessionInfoTab sessionId={id} />
        </div>
      )}

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

      {compactConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/50"
          onClick={() => setCompactConfirm(false)}
        >
          <div
            className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="mb-2 text-base font-semibold text-ink">Compact context?</h2>
            <p className="mb-5 text-sm text-ink-soft leading-relaxed">
              This trims the conversation history to free up context window space.
              Older turns will be summarized and may lose detail.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setCompactConfirm(false)}
                className="flex-1 rounded-xl border border-hairline bg-surface-1 py-3 text-[14px] font-medium text-ink-soft hover:bg-surface-2"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setCompactConfirm(false);
                  id &&
                    compact.mutate(id, {
                      onSuccess: (r) =>
                        setCompactBanner(r.ok ? "Context compacted." : `Compaction failed: ${r.errors?.[0] ?? "unknown"}`),
                      onError: (e) =>
                        setCompactBanner(`Compaction failed: ${String(e.message)}`),
                    });
                }}
                className="flex-1 rounded-xl bg-warn py-3 text-[14px] font-medium text-white hover:brightness-110"
              >
                Compact
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
