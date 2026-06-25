import { useState, useMemo } from "react";
import { Link } from "react-router-dom";
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
} from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { TaskStatusChip } from "../components/ui/StatusChip";
import { useTasks, useSessions } from "../hooks/useLiveData";
import type { Task, Session } from "../domain/models";
import { cn } from "../lib/cn";

const COMPLETED_SHOW = 5;

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

function StateIcon({ state }: { state: Task["state"] }) {
  switch (state) {
    case "running":
      return <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" />;
    case "succeeded":
      return <CheckCircle2 className="size-3.5 shrink-0 text-ok" />;
    case "failed":
    case "cancelled":
      return <XCircle className="size-3.5 shrink-0 text-bad" />;
    case "waiting_for_input":
    case "waiting_for_approval":
      return <MessageSquare className="size-3.5 shrink-0 text-warn" />;
    default:
      return <CircleDot className="size-3.5 shrink-0 text-ink-muted" />;
  }
}

function TaskRow({
  task,
  session,
}: {
  task: Task;
  session: Session | undefined;
}) {
  const isWaiting =
    task.state === "waiting_for_input" || task.state === "waiting_for_approval";
  const isRunning = task.state === "running";
  const proj = projectName(session);
  const ts = isRunning
    ? elapsedTime(task.createdAt)
    : relativeTime(task.completedAt ?? task.updatedAt);

  const body = (
    <div
      className={cn(
        "card-elev rounded-xl px-4 py-3 transition-transform active:scale-[0.99]",
        isWaiting && "attention-glow",
      )}
    >
      <div className="flex items-start gap-2.5">
        <StateIcon state={task.state} />
        <div className="min-w-0 flex-1">
          {/* Objective */}
          <p className="text-[13.5px] leading-snug text-ink line-clamp-2">
            {task.objective}
          </p>

          {/* Meta: project · backend · time */}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-ink-muted">
            {proj && (
              <span className="font-medium text-ink-soft">{proj}</span>
            )}
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
          {task.error && !isWaiting && (
            <p className="mt-1 flex items-start gap-1 text-[11px] text-bad">
              <AlertTriangle className="mt-px size-3 shrink-0" />
              <span className="line-clamp-1">{task.error}</span>
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <TaskStatusChip state={task.state} />
          {task.sessionId && (
            <span className="inline-flex items-center gap-0.5 text-[10px] text-accent/60">
              <ExternalLink className="size-2.5" />
              session
            </span>
          )}
        </div>
      </div>
    </div>
  );

  return task.sessionId ? (
    <Link to={`/sessions/${task.sessionId}`}>{body}</Link>
  ) : (
    body
  );
}

export function TasksScreen() {
  const { data: rawTasks, isLoading, isError } = useTasks(100);
  const { data: sessions } = useSessions();
  const [completedExpanded, setCompletedExpanded] = useState(false);

  const sessionMap = useMemo(() => {
    const m = new Map<string, Session>();
    for (const s of sessions ?? []) m.set(s.id, s);
    return m;
  }, [sessions]);

  const { waiting, active, completed } = useMemo(() => {
    const all = rawTasks ?? [];

    const waiting = all.filter(
      (t) => t.state === "waiting_for_input" || t.state === "waiting_for_approval",
    );

    const active = all
      .filter(
        (t) =>
          t.state !== "waiting_for_input" &&
          t.state !== "waiting_for_approval" &&
          t.state !== "succeeded" &&
          t.state !== "failed" &&
          t.state !== "cancelled",
      )
      .sort((a, b) => (b.updatedAt > a.updatedAt ? 1 : -1));

    const completed = all
      .filter(
        (t) =>
          t.state === "succeeded" ||
          t.state === "failed" ||
          t.state === "cancelled",
      )
      .sort((a, b) =>
        (b.completedAt ?? b.updatedAt) > (a.completedAt ?? a.updatedAt) ? 1 : -1,
      );

    return { waiting, active, completed };
  }, [rawTasks]);

  const visibleCompleted = completedExpanded
    ? completed
    : completed.slice(0, COMPLETED_SHOW);

  const empty =
    !isLoading && !isError && (rawTasks ?? []).length === 0;

  return (
    <div className="pb-8">
      <CompactTopBar title="Tasks" subtitle="Live · chronological" />

      {isLoading && (
        <div className="space-y-2.5 px-4 pt-4">
          {[1, 2, 3].map((n) => (
            <div key={n} className="card-elev animate-pulse rounded-xl px-4 py-3">
              <div className="flex items-center gap-2.5">
                <div className="size-3.5 rounded-full bg-surface-2" />
                <div className="h-4 flex-1 rounded bg-surface-2" />
                <div className="h-5 w-16 rounded-full bg-surface-2" />
              </div>
              <div className="mt-2 ml-6 flex gap-2">
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
          <p className="text-[15px] font-medium text-ink-soft">No tasks yet</p>
          <p className="text-sm text-ink-muted">
            Tasks appear here when sessions run work.
          </p>
        </div>
      )}

      {/* Waiting — genuinely needs a human now */}
      {waiting.length > 0 && (
        <>
          <div className="flex items-center gap-2 px-4 pb-2 pt-6">
            <ShieldQuestion className="size-3.5 text-warn" />
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-warn">
              Needs your input
            </h2>
            <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
              {waiting.length}
            </span>
          </div>
          <div className="space-y-2 px-4">
            {waiting.map((t) => (
              <TaskRow key={t.id} task={t} session={sessionMap.get(t.sessionId ?? "")} />
            ))}
          </div>
        </>
      )}

      {/* Active: running + queued — chronological */}
      {active.length > 0 && (
        <>
          <div className="flex items-center gap-2 px-4 pb-2 pt-6">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-ink-muted">
              In progress
            </h2>
            <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
              {active.length}
            </span>
          </div>
          <div className="space-y-2 px-4">
            {active.map((t) => (
              <TaskRow key={t.id} task={t} session={sessionMap.get(t.sessionId ?? "")} />
            ))}
          </div>
        </>
      )}

      {/* Completed: newest first, collapsed after N */}
      {completed.length > 0 && (
        <>
          <div className="flex items-center gap-2 px-4 pb-2 pt-6">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-ink-muted">
              Completed
            </h2>
            <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-medium text-ink-soft">
              {completed.length}
            </span>
          </div>
          <div className="space-y-2 px-4">
            {visibleCompleted.map((t) => (
              <TaskRow key={t.id} task={t} session={sessionMap.get(t.sessionId ?? "")} />
            ))}
          </div>
          {completed.length > COMPLETED_SHOW && (
            <button
              onClick={() => setCompletedExpanded((v) => !v)}
              className="mx-4 mt-2 flex w-[calc(100%-2rem)] items-center justify-center gap-1.5 rounded-xl border border-hairline py-2.5 text-[12px] text-ink-muted hover:bg-surface-1"
            >
              <ChevronDown
                className={cn(
                  "size-3.5 transition-transform",
                  completedExpanded && "rotate-180",
                )}
              />
              {completedExpanded
                ? "Show fewer"
                : `Show ${completed.length - COMPLETED_SHOW} more`}
            </button>
          )}
        </>
      )}
    </div>
  );
}
