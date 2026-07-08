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
  ChevronUp,
  MessagesSquare,
  FolderOpen,
} from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SessionStatusChip } from "../components/ui/StatusChip";
import { SessionAffiliationLink } from "../components/work/SessionAffiliationLabel";
import { SessionTimeline, userAnchorId } from "../components/timeline/SessionTimeline";
import { SessionTurns } from "../components/timeline/SessionTurns";
import { Composer } from "../components/timeline/Composer";
import { JobRow } from "../components/system/JobsPanel";
import { ModelPickerSheet } from "../components/sessions/ModelPickerSheet";
import { GitPanelSheet } from "../components/sessions/GitPanelSheet";
import { useSessions, useApprovals, useSessionMessages, useArtifacts, useArtifact, useSessionTurns, useSessionActivity, useJobs } from "../hooks/useLiveData";
import { useSessionAffiliations } from "../hooks/useWork";
import { useSessionTimeline } from "../hooks/useSessionTimeline";
import { useTaskActivity } from "../hooks/useTaskActivity";
import {
  useStopSession,
  useCloseSession,
  useRestoreSession,
  useCompactSession,
  useInspectSession,
} from "../hooks/useSessionActions";
import { cn } from "../lib/cn";
import { clockLabel } from "../lib/time";
import {
  activityKindLabel,
  activityStatusView,
  type ActivityTone,
} from "../lib/sessionActivityPresentation";
import type { Artifact, RemoteFile, SessionActivityItem } from "../domain/models";
import type { RawJob } from "../transport/rawApi";

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

const ACTIVITY_TONE_CLASS: Record<ActivityTone, string> = {
  running: "bg-running text-running",
  ok: "bg-ok text-ok",
  warn: "bg-warn text-warn",
  bad: "bg-bad text-bad",
  idle: "bg-ink-muted text-ink-muted",
};

function ActivityStatusPill({ item }: { item: SessionActivityItem }) {
  const view = activityStatusView(item);
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-surface-3/70 py-1 pl-2 pr-2.5 text-[11px] font-medium text-ink-soft">
      <span className={cn("size-1.5 rounded-full", ACTIVITY_TONE_CLASS[view.tone])} />
      {view.label}
    </span>
  );
}

function SessionStateRow({
  item,
  onOpenFiles,
}: {
  item: SessionActivityItem;
  onOpenFiles?: () => void;
}) {
  const label = activityKindLabel(item.kind);
  const identifier = item.taskId ?? item.jobId ?? item.turnId ?? item.nodeId;
  const detail =
    typeof item.detail.reason === "string"
      ? item.detail.reason
      : typeof item.detail.label === "string"
        ? item.detail.label
        : item.source;
  const canOpenFiles = item.kind === "artifact" || item.kind === "file_change";
  return (
    <div className="flex items-center gap-2.5 px-4 py-2.5">
      <span
        className={cn(
          "size-1.5 shrink-0 rounded-full",
          item.confidence === "low" || item.staleness === "stale"
            ? "bg-warn"
            : item.durability === "durable"
              ? "bg-ok"
              : "bg-accent",
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-[11px] font-medium text-ink-muted">{label}</span>
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-soft">{item.summary}</span>
        </div>
        {(identifier || detail) && (
          <div className="mt-0.5 truncate font-mono text-[10.5px] text-ink-muted">
            {[identifier, detail].filter(Boolean).join(" / ")}
          </div>
        )}
      </div>
      <ActivityStatusPill item={item} />
      {canOpenFiles && onOpenFiles && (
        <button
          type="button"
          onClick={onOpenFiles}
          className="flex size-7 shrink-0 items-center justify-center rounded-full text-ink-muted hover:bg-surface-2 hover:text-ink"
          aria-label="Open files"
        >
          <FolderGit2 className="size-3.5" />
        </button>
      )}
      <span className="shrink-0 text-[10.5px] tabular-nums text-ink-muted">{clockLabel(item.timestamp)}</span>
    </div>
  );
}

function SessionStateSequence({
  sessionId,
  onOpenFiles,
}: {
  sessionId: string;
  onOpenFiles?: () => void;
}) {
  const { data, isLoading, isError } = useSessionActivity(sessionId, 30);
  const scoped = (data?.items ?? []).slice(0, 12);

  return (
    <div>
      <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
        <Activity className="size-3" />
        Durable state
      </p>
      <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
        {isLoading ? (
          <div className="flex items-center gap-2 px-4 py-3 text-[12px] text-ink-muted">
            <Loader2 className="size-3.5 animate-spin" />
            Loading durable timeline...
          </div>
        ) : isError ? (
          <div className="px-4 py-3 text-[12px] text-warn">
            Durable timeline unavailable. State is not inferred from live events.
          </div>
        ) : scoped.length === 0 ? (
          <div className="px-4 py-3 text-[12px] text-ink-muted">No durable state yet.</div>
        ) : (
          scoped.map((item) => (
            <SessionStateRow key={item.id} item={item} onOpenFiles={onOpenFiles} />
          ))
        )}
      </div>
      {data && data.coverage.telemetry === "partial" && (
        <p className="mt-2 text-[11px] text-warn">
          Telemetry coverage is partial; uncertain work is shown explicitly.
        </p>
      )}
    </div>
  );
}

function SessionJobsSection({ sessionId }: { sessionId: string }) {
  const { data, isLoading } = useJobs(20, sessionId);
  const running: RawJob[] = data?.running ?? [];
  const recent: RawJob[] = (data?.recent ?? []).filter(
    (j) => j.status === "done" || j.status === "failed" || j.status === "lost",
  );
  const total = running.length + recent.length;

  return (
    <div>
      <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
        <Activity className="size-3" />
        Jobs
      </p>
      <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
        {isLoading ? (
          <div className="flex items-center gap-2 px-4 py-3 text-[12px] text-ink-muted">
            <Loader2 className="size-3.5 animate-spin" />
            Loading jobs...
          </div>
        ) : total === 0 ? (
          <p className="px-4 py-3 text-[12px] text-ink-muted">No watched jobs for this session.</p>
        ) : (
          <>
            {running.map((job) => (
              <JobRow key={job.id} job={job} running />
            ))}
            {recent.map((job) => (
              <JobRow key={job.id} job={job} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function SessionInfoTab({
  sessionId,
  onOpenFiles,
}: {
  sessionId: string;
  onOpenFiles?: () => void;
}) {
  const { data: sessions } = useSessions();
  const session = sessions?.find((s) => s.id === sessionId);
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [dirsPath, setDirsPath] = useState<string>("");
  const [dirsExpanded, setDirsExpanded] = useState(false);
  const inspect = useInspectSession();
  const { data: turns, isLoading: turnsLoading } = useSessionTurns(sessionId);

  // Lazy: only fetch dirs when the section is expanded for the first time,
  // not on every mount. Matches the original SessionInfoPanel pattern.
  const toggleDirs = () => {
    const next = !dirsExpanded;
    setDirsExpanded(next);
    if (next && dirs === null) {
      inspect.mutate(
        { sessionId, op: "list_dirs", params: { limit: 12, sort_by_recent: true } },
        {
          onSuccess: (r) => {
            const res = r as { dirs?: string[]; path?: string };
            setDirs(res.dirs ?? []);
            setDirsPath(res.path ?? "");
          },
        },
      );
    }
  };

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

      <SessionStateSequence sessionId={sessionId} onOpenFiles={onOpenFiles} />

      <SessionJobsSection sessionId={sessionId} />

      <div>
        <button
          onClick={toggleDirs}
          className="flex w-full items-center gap-1.5 py-1 text-[11px] font-semibold uppercase tracking-wide text-ink-muted hover:text-ink-soft"
          aria-expanded={dirsExpanded}
        >
          <ChevronDown
            className={cn("size-3.5 transition-transform", dirsExpanded && "rotate-180")}
          />
          Subdirectories{dirsPath ? ` in ${dirsPath.split("/").pop() ?? dirsPath}` : ""}
        </button>

        {dirsExpanded && (
          <>
            {inspect.isPending && dirs === null && (
              <p className="mt-1 text-sm text-ink-muted">Loading…</p>
            )}
            {dirs !== null && dirs.length === 0 && (
              <p className="mt-1 text-sm text-ink-muted">No subdirectories found.</p>
            )}
            {dirs !== null && dirs.length > 0 && (
              <div className="card-elev mt-2 overflow-hidden rounded-xl divide-y divide-hairline">
                {dirs.map((d) => (
                  <div key={d} className="flex items-center gap-1.5 px-4 py-2.5 font-mono text-[12px] text-ink-soft">
                    <FolderOpen className="size-3 shrink-0 text-ink-muted" />
                    {d.split("/").pop() ?? d}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── Main Screen ───────────────────────────────────────────────────────────────

export function SessionDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data, isLoading: sessionsLoading } = useSessions();
  const session = data?.find((s) => s.id === id);
  // Authoritative case membership for this session (Work substrate). undefined ⇒
  // standalone; never inferred from task adjacency.
  const { index: affiliations } = useSessionAffiliations();
  const affiliation = id ? affiliations.get(id) : undefined;
  const {
    data: turns,
    isLoading: messagesLoading,
    isFetched: messagesFetched,
    isError: messagesError,
    fetchStatus: messagesFetchStatus,
  } = useSessionMessages(id);
  const { data: approvals } = useApprovals();
  const timeline = useSessionTimeline(id, session, turns ?? [], approvals ?? []);
  const liveActivity = useTaskActivity(id, session?.lastTaskId ?? undefined);
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
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
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

    // Jump-to-bottom button: shown once the user has scrolled meaningfully
    // away from the live edge of the conversation.
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowJumpToBottom(distanceFromBottom > 240);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    bottomRef.current?.scrollIntoView({ behavior, block: "end" });
  }, []);

  // Ordered ids of every user message in the current timeline, so the
  // prev/next controls can walk between them regardless of what else is
  // interleaved (approvals, notices, agent replies).
  const userMessageIds = timeline
    .filter((it): it is Extract<typeof it, { kind: "message" }> => it.kind === "message" && it.message.role === "user")
    .map((it) => it.message.id);

  const jumpToUserMessage = useCallback(
    (direction: "prev" | "next") => {
      const container = timelineRef.current;
      if (!container || userMessageIds.length === 0) return;
      const anchors = userMessageIds
        .map((mid) => container.querySelector<HTMLElement>(`#${userAnchorId(mid)}`))
        .filter((el): el is HTMLElement => !!el);
      if (anchors.length === 0) return;

      const containerRect = container.getBoundingClientRect();
      const positions = anchors.map(
        (el) => el.getBoundingClientRect().top - containerRect.top + container.scrollTop,
      );
      const current = container.scrollTop;
      const EPS = 4;

      let target: number | undefined;
      if (direction === "next") {
        target = positions.find((p) => p > current + EPS);
      } else {
        const past = positions.filter((p) => p < current - EPS);
        target = past.at(-1);
      }
      if (target != null) {
        container.scrollTo({ top: target, behavior: "smooth" });
      }
    },
    [userMessageIds],
  );

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
        <div className="relative flex-1 overflow-hidden">
        <div
          ref={timelineRef}
          className="h-full overflow-y-auto overscroll-contain"
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

          {/* Authoritative Work affiliation: which case owns this session, and
              in what role. Absent ⇒ standalone. Links out to the case. */}
          {session && (
            <div className="flex items-center gap-2 border-b border-hairline bg-base/40 px-4 py-2">
              <span className="text-[11px] text-ink-muted">Affiliation</span>
              <SessionAffiliationLink affiliation={affiliation} />
            </div>
          )}

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
                <SessionTimeline items={timeline} liveActivity={liveActivity} />
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

        {/* Floating jump controls — jump between your own messages, and back to
            the live edge of the conversation. Positioned above the composer,
            inset from the trailing edge like the WhatsApp/Slack pattern. */}
        <div className="pointer-events-none absolute bottom-3 right-3 z-10 flex flex-col items-end gap-2">
          {userMessageIds.length > 1 && (
            <div className="pointer-events-auto flex overflow-hidden rounded-full border border-hairline bg-surface-2/95 shadow-lg backdrop-blur-sm">
              <button
                type="button"
                onClick={() => jumpToUserMessage("prev")}
                className="flex size-9 items-center justify-center text-ink-soft hover:bg-surface-3 hover:text-ink"
                aria-label="Jump to previous message"
                title="Jump to previous message"
              >
                <ChevronUp className="size-4" />
              </button>
              <div className="w-px bg-hairline" />
              <button
                type="button"
                onClick={() => jumpToUserMessage("next")}
                className="flex size-9 items-center justify-center text-ink-soft hover:bg-surface-3 hover:text-ink"
                aria-label="Jump to next message"
                title="Jump to next message"
              >
                <ChevronDown className="size-4" />
              </button>
            </div>
          )}
          {showJumpToBottom && (
            <button
              type="button"
              onClick={() => scrollToBottom()}
              className="pointer-events-auto flex size-10 items-center justify-center rounded-full border border-hairline bg-surface-2/95 text-ink-soft shadow-lg backdrop-blur-sm transition-transform hover:scale-105 hover:bg-surface-3 hover:text-ink active:scale-95"
              aria-label="Scroll to latest message"
              title="Scroll to latest message"
            >
              <ChevronDown className="size-5" />
            </button>
          )}
        </div>
        </div>

        {/* Composer pinned outside the scroll container so it always sits at the true bottom */}
        {id && !closed ? (
          <Composer sessionId={id} running={running} />
        ) : id ? (
          // Closed sessions (e.g. a completed one-off opened from a push
          // notification) used to be a dead read-only view. Offer resume inline
          // so you can immediately continue instead of hunting the menu.
          <div
            className="border-t border-hairline bg-surface-1/95 px-3 py-3 backdrop-blur-xl"
            style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
          >
            <button
              onClick={() => restore.mutate(id)}
              disabled={restore.isPending}
              className="flex w-full items-center justify-center gap-2 rounded-2xl bg-accent-dim/60 py-3 text-[14px] font-medium text-accent ring-1 ring-inset ring-accent/30 transition-colors hover:bg-accent-dim disabled:opacity-60"
            >
              {restore.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <RotateCcw className="size-4" />
              )}
              {restore.isPending ? "Resuming…" : "Resume session to reply"}
            </button>
          </div>
        ) : null}
        </div>
      )}

      {tab === "files" && id && (
        <div className="flex-1 overflow-y-auto overscroll-contain">
          <SessionFilesTab sessionId={id} />
        </div>
      )}

      {tab === "info" && id && (
        <div className="flex-1 overflow-y-auto overscroll-contain">
          <SessionInfoTab sessionId={id} onOpenFiles={() => setTab("files")} />
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
