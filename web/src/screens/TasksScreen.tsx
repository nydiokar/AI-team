import { useState, useMemo } from "react";
import { useNavigate, Link } from "react-router-dom";
import {
  ExternalLink,
  Clock,
  MessageSquare,
  ShieldQuestion,
  ChevronDown,
  Loader2,
  CheckCircle2,
  XCircle,
  CircleDot,
  AlertTriangle,
  Square,
  RotateCcw,
  Inbox,
  X,
} from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { TaskStatusChip } from "../components/ui/StatusChip";
import { useTaskSections, useSessions } from "../hooks/useLiveData";
import { useStopSession, useSubmitInstruction } from "../hooks/useSessionActions";
import { useDismissedStore } from "../stores/dismissedStore";
import type { Task, Session } from "../domain/models";
import { cn } from "../lib/cn";

const RECENT_SHOW = 8;

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  const days = Math.floor(sec / 86400);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function elapsedTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function projectName(session: Session | undefined): string {
  if (!session) return "";
  const parts = session.workspace.path.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || "";
}

function StateIcon({ state, className }: { state: Task["state"]; className?: string }) {
  const base = cn("size-3.5 shrink-0", className);
  switch (state) {
    case "running":
    case "dispatching":
      return <Loader2 className={cn(base, "animate-spin text-accent")} />;
    case "succeeded":
      return <CheckCircle2 className={cn(base, "text-ok")} />;
    case "failed":
    case "cancelled":
      return <XCircle className={cn(base, "text-bad")} />;
    case "waiting_for_input":
    case "waiting_for_approval":
      return <MessageSquare className={cn(base, "text-warn")} />;
    default:
      return <CircleDot className={cn(base, "text-ink-muted")} />;
  }
}

// ── Primary action ──────────────────────────────────────────────────────────
// Spec §7.4 "Task card → primary action". The action depends on lifecycle state.
// We wire the two that the live control API supports today (Stop a running task,
// Answer a waiting one → opens the session composer). Review/Retry deep-link to
// the session where the affordance lives.

function PrimaryAction({ task }: { task: Task }) {
  const navigate = useNavigate();
  const stop = useStopSession();
  const submit = useSubmitInstruction();
  const sid = task.sessionId;

  if (!sid) return null;

  const open = () => navigate(`/sessions/${sid}`);

  const btn =
    "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50";

  switch (task.state) {
    case "running":
    case "dispatching":
      return (
        <button
          onClick={(e) => {
            e.preventDefault();
            stop.mutate(sid);
          }}
          disabled={stop.isPending}
          className={cn(btn, "bg-bad/10 text-bad hover:bg-bad/20")}
        >
          <Square className="size-3 fill-current" />
          Stop
        </button>
      );
    case "waiting_for_input":
    case "waiting_for_approval":
      return (
        <button
          onClick={(e) => {
            e.preventDefault();
            open();
          }}
          className={cn(btn, "bg-warn/15 text-warn hover:bg-warn/25")}
        >
          <MessageSquare className="size-3" />
          {task.state === "waiting_for_approval" ? "Review" : "Answer"}
        </button>
      );
    case "failed":
      return (
        <button
          onClick={(e) => {
            e.preventDefault();
            // Re-issue the same objective into the parent session.
            if (task.objective) submit.mutate({ description: task.objective, sessionId: sid });
            open();
          }}
          disabled={submit.isPending}
          className={cn(btn, "bg-accent/10 text-accent hover:bg-accent/20")}
        >
          <RotateCcw className="size-3" />
          Retry
        </button>
      );
    default:
      return null;
  }
}

// ── Rich card (attention / running / queued) ────────────────────────────────

function TaskCard({
  task,
  session,
  onDismiss,
}: {
  task: Task;
  session: Session | undefined;
  /** When set, shows a dismiss (×) affordance — used by the Failed section. */
  onDismiss?: (taskId: string) => void;
}) {
  const isWaiting =
    task.state === "waiting_for_input" || task.state === "waiting_for_approval";
  const isRunning = task.state === "running" || task.state === "dispatching";
  const isFailed = task.state === "failed";
  const proj = projectName(session);
  const ts = isRunning
    ? elapsedTime(task.createdAt)
    : relativeTime(task.completedAt ?? task.updatedAt);
  // Spec §7.4 "latest meaningful event": prefer the session's last turn summary.
  const latest = session?.lastSummary?.trim();

  return (
    <div
      className={cn(
        "card-elev rounded-xl px-4 py-3 transition-transform active:scale-[0.99]",
        isWaiting && "attention-glow",
        isFailed && "ring-1 ring-inset ring-bad/30",
      )}
    >
      <div className="flex items-start gap-2.5">
        <StateIcon state={task.state} />
        <div className="min-w-0 flex-1">
          <Link to={task.sessionId ? `/sessions/${task.sessionId}` : "#"}>
            <p className="text-[13.5px] leading-snug text-ink line-clamp-2">
              {task.objective}
            </p>
          </Link>

          {/* Latest meaningful event */}
          {latest && !isFailed && (
            <p className="mt-1 line-clamp-1 text-[11.5px] text-ink-soft">{latest}</p>
          )}

          {/* Meta: project · backend · time */}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-ink-muted">
            {proj && <span className="font-medium text-ink-soft">{proj}</span>}
            <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-accent/80">
              {task.backend}
            </span>
            {ts && (
              <span className="inline-flex items-center gap-1">
                <Clock className="size-2.5 opacity-50" />
                {isRunning ? `running ${ts}` : ts}
              </span>
            )}
          </div>

          {/* Error */}
          {task.error && (
            <p className="mt-1 flex items-start gap-1 text-[11px] text-bad">
              <AlertTriangle className="mt-px size-3 shrink-0" />
              <span className="line-clamp-1">{task.error}</span>
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-2">
          <div className="flex items-center gap-1">
            <TaskStatusChip state={task.state} />
            {onDismiss && (
              <button
                onClick={(e) => {
                  e.preventDefault();
                  onDismiss(task.id);
                }}
                aria-label="Dismiss"
                title="Dismiss — hide this failure"
                className="rounded-md p-1 text-ink-muted transition-colors hover:bg-surface-2 hover:text-ink"
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
          <PrimaryAction task={task} />
        </div>
      </div>
    </div>
  );
}

// ── Dense row (recently completed) ──────────────────────────────────────────
// 100+ finished tasks must not be 100 full cards. One scannable line each.

function TaskLogRow({
  task,
  session,
}: {
  task: Task;
  session: Session | undefined;
}) {
  const proj = projectName(session);
  const ts = relativeTime(task.completedAt ?? task.updatedAt);
  const isFailed = task.state === "failed";

  const row = (
    <div className="group flex items-center gap-2.5 rounded-lg px-3 py-1.5 transition-colors hover:bg-surface-1">
      <StateIcon state={task.state} />
      <span
        className={cn(
          "min-w-0 flex-1 truncate text-[12.5px]",
          isFailed ? "text-ink" : "text-ink-soft",
        )}
      >
        {task.objective}
        {isFailed && task.error && (
          <span className="ml-2 text-[11px] text-bad/80">— {task.error}</span>
        )}
      </span>
      <span className="hidden shrink-0 font-mono text-[10px] text-accent/60 sm:inline">
        {task.backend}
      </span>
      {proj && (
        <span className="hidden shrink-0 text-[10px] text-ink-muted md:inline">
          {proj}
        </span>
      )}
      <span className="shrink-0 text-[10px] tabular-nums text-ink-muted">{ts}</span>
      {task.sessionId && (
        <ExternalLink className="size-3 shrink-0 text-ink-muted opacity-0 transition-opacity group-hover:opacity-60" />
      )}
    </div>
  );

  return task.sessionId ? (
    <Link to={`/sessions/${task.sessionId}`}>{row}</Link>
  ) : (
    row
  );
}

// ── Section header ──────────────────────────────────────────────────────────

function SectionHead({
  label,
  count,
  tone = "muted",
  icon,
}: {
  label: string;
  count: number;
  tone?: "muted" | "warn";
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2 px-4 pb-2 pt-6">
      {icon}
      <h2
        className={cn(
          "text-[11px] font-semibold uppercase tracking-widest", 
          tone === "warn" ? "text-warn" : "text-ink-muted",
        )}
      >
        {label}
      </h2>
      <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
        {count}
      </span>
    </div>
  );
}

export function TasksScreen() {
  const { data: sections, isLoading, isError } = useTaskSections(100);
  const { data: sessions } = useSessions();
  const [recentExpanded, setRecentExpanded] = useState(false);
  const [failedCollapsed, setFailedCollapsed] = useState(false);

  const dismissedIds = useDismissedStore((s) => s.ids);
  const dismiss = useDismissedStore((s) => s.dismiss);
  const clearDismissed = useDismissedStore((s) => s.clear);

  const sessionMap = useMemo(() => {
    const m = new Map<string, Session>();
    for (const s of sessions ?? []) m.set(s.id, s);
    return m;
  }, [sessions]);

  const dismissed = useMemo(() => new Set(dismissedIds), [dismissedIds]);

  const attention = sections?.attention ?? [];
  const running = sections?.running ?? [];
  const queued = sections?.queued ?? [];
  // Failed is its own terminal section; dismissed ones are hidden (per-viewer).
  const allFailed = sections?.failed ?? [];
  const failed = allFailed.filter((t) => !dismissed.has(t.id));
  const dismissedCount = allFailed.length - failed.length;
  const recent = sections?.recent ?? [];

  const liveCount =
    attention.length + running.length + queued.length + failed.length;
  const empty =
    !isLoading && !isError && liveCount === 0 && recent.length === 0;
  const inboxZero = !isLoading && !isError && liveCount === 0 && recent.length > 0;

  const visibleRecent = recentExpanded ? recent : recent.slice(0, RECENT_SHOW);

  const sess = (t: Task) => sessionMap.get(t.sessionId ?? "");

  return (
    <div className="pb-8">
      <CompactTopBar title="Tasks" subtitle="Operational inbox · all sessions" />

      {isLoading && (
        <div className="space-y-2.5 px-4 pt-4">
          {[1, 2, 3].map((n) => (
            <div key={n} className="card-elev animate-pulse rounded-xl px-4 py-3">
              <div className="flex items-center gap-2.5">
                <div className="size-3.5 rounded-full bg-surface-2" />
                <div className="h-4 flex-1 rounded bg-surface-2" />
                <div className="h-5 w-16 rounded-full bg-surface-2" />
              </div>
              <div className="ml-6 mt-2 flex gap-2">
                <div className="h-3 w-16 rounded bg-surface-2" />
                <div className="h-3 w-10 rounded bg-surface-2" />
              </div>
            </div>
          ))}
        </div>
      )}

      {isError && (
        <p className="px-4 py-8 text-center text-sm text-bad">Couldn't load tasks.</p>
      )}

      {empty && (
        <div className="flex flex-col items-center gap-2 px-4 py-20 text-center">
          <Inbox className="size-8 text-ink-muted" />
          <p className="text-[15px] font-medium text-ink-soft">No tasks yet</p>
          <p className="text-sm text-ink-muted">
            Tasks appear here when sessions run work.
          </p>
        </div>
      )}

      {/* Inbox-zero: nothing needs you, but history exists. Reassure, don't blank. */}
      {inboxZero && (
        <div className="mx-4 mt-4 flex items-center gap-3 rounded-xl border border-hairline bg-surface-1/40 px-4 py-3">
          <CheckCircle2 className="size-5 shrink-0 text-ok" />
          <div>
            <p className="text-[13.5px] font-medium text-ink">Nothing needs you</p>
            <p className="text-[11.5px] text-ink-muted">
              No running, blocked, or waiting work. {recent.length} recently completed below.
            </p>
          </div>
        </div>
      )}

      {/* Needs attention — waiting / blocked (NOT failed; that's its own section).
          The reason this page exists. */}
      {attention.length > 0 && (
        <>
          <SectionHead
            label="Needs your input"
            count={attention.length}
            tone="warn"
            icon={<ShieldQuestion className="size-3.5 text-warn" />}
          />
          <div className="space-y-2 px-4">
            {attention.map((t) => (
              <TaskCard key={t.id} task={t} session={sess(t)} />
            ))}
          </div>
        </>
      )}

      {/* Running */}
      {running.length > 0 && (
        <>
          <SectionHead label="Running" count={running.length} />
          <div className="space-y-2 px-4">
            {running.map((t) => (
              <TaskCard key={t.id} task={t} session={sess(t)} />
            ))}
          </div>
        </>
      )}

      {/* Queued */}
      {queued.length > 0 && (
        <>
          <SectionHead label="Queued" count={queued.length} />
          <div className="space-y-2 px-4">
            {queued.map((t) => (
              <TaskCard key={t.id} task={t} session={sess(t)} />
            ))}
          </div>
        </>
      )}

      {/* Failed — terminal, surfaced but OUT of the act-now queue. Dismissible
          per-viewer so it never bloats into an unmanageable graveyard. */}
      {failed.length > 0 && (
        <>
          <div className="flex items-center gap-2 px-4 pb-2 pt-6">
            <XCircle className="size-3.5 text-bad" />
            <h2 className="text-[11px] font-semibold uppercase tracking-widest text-bad">
              Failed
            </h2>
            <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
              {failed.length}
            </span>
            <div className="ml-auto flex items-center gap-3">
              <button
                onClick={() => failed.forEach((t) => dismiss(t.id))}
                className="text-[11px] text-ink-muted hover:text-ink"
              >
                Dismiss all
              </button>
              <button
                onClick={() => setFailedCollapsed((v) => !v)}
                aria-expanded={!failedCollapsed}
                className="text-ink-muted hover:text-ink"
                aria-label={failedCollapsed ? "Expand failed" : "Collapse failed"}
              >
                <ChevronDown
                  className={cn(
                    "size-3.5 transition-transform",
                    failedCollapsed && "-rotate-90",
                  )}
                />
              </button>
            </div>
          </div>
          {!failedCollapsed && (
            <div className="space-y-2 px-4">
              {failed.map((t) => (
                <TaskCard key={t.id} task={t} session={sess(t)} onDismiss={dismiss} />
              ))}
            </div>
          )}
        </>
      )}

      {/* Restore affordance — dismissed failures aren't gone, just hidden here. */}
      {dismissedCount > 0 && (
        <button
          onClick={clearDismissed}
          className="mx-4 mt-3 flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink"
        >
          <RotateCcw className="size-3" />
          Restore {dismissedCount} dismissed
        </button>
      )}

      {/* Recently completed — dense log, quiet, scannable. */}
      {recent.length > 0 && (
        <>
          <SectionHead label="Recently completed" count={recent.length} />
          <div className="px-2">
            {visibleRecent.map((t) => (
              <TaskLogRow key={t.id} task={t} session={sess(t)} />
            ))}
          </div>
          {recent.length > RECENT_SHOW && (
            <button
              onClick={() => setRecentExpanded((v) => !v)}
              className="mx-4 mt-2 flex w-[calc(100%-2rem)] items-center justify-center gap-1.5 rounded-xl border border-hairline py-2.5 text-[12px] text-ink-muted hover:bg-surface-1"
            >
              <ChevronDown
                className={cn(
                  "size-3.5 transition-transform",
                  recentExpanded && "rotate-180",
                )}
              />
              {recentExpanded
                ? "Show fewer"
                : `Show ${recent.length - RECENT_SHOW} more`}
            </button>
          )}
        </>
      )}
    </div>
  );
}
