/**
 * Tasks screen — LIVE flat in UI-2 (bound to /api/tasks via useTasks + the
 * taskAdapter). Global cross-session inbox (spec §7.4), sectioned Needs attention
 * / Running / Queued / Recently completed.
 *
 * NOTE: the sections derive from the mesh_tasks status subset the adapter can map
 * today (queued/dispatching/running/succeeded/failed/cancelled). The richer
 * supervised lifecycle (waiting_for_input / waiting_for_approval correctness) is
 * still Move G′ (gap-doc §4) — those buckets stay empty until G′ lands.
 */
import { Link } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { TaskStatusChip } from "../components/ui/StatusChip";
import { useTasks } from "../hooks/useLiveData";
import type { Task } from "../domain/models";
import type { TaskState } from "../domain/status";

const ATTENTION: TaskState[] = ["waiting_for_input", "waiting_for_approval", "failed", "connection_unknown"];
const RUNNING: TaskState[] = ["running", "dispatching"];

function TaskCard({ task }: { task: Task }) {
  const body = (
    <div className="card-elev rounded-xl px-4 py-3.5">
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 text-[14px] text-ink">{task.objective}</p>
        <TaskStatusChip state={task.state} />
      </div>
      <div className="mt-2 flex items-center gap-2 text-xs text-ink-muted">
        <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-accent/90">
          {task.backend}
        </span>
        {task.targetId && (
          <>
            <span className="opacity-40">·</span>
            <span>{task.targetId}</span>
          </>
        )}
        {task.sessionId && (
          <span className="ml-auto inline-flex items-center gap-1 text-ink-muted">
            <ExternalLink className="size-3" /> session
          </span>
        )}
      </div>
      {task.error && <p className="mt-2 truncate text-xs text-bad">{task.error}</p>}
    </div>
  );
  return task.sessionId ? <Link to={`/sessions/${task.sessionId}`}>{body}</Link> : body;
}

function Section({ label, tasks, accent }: { label: string; tasks: Task[]; accent?: "warn" }) {
  if (tasks.length === 0) return null;
  return (
    <>
      <SectionHeader label={label} count={tasks.length} accent={accent} />
      <div className="space-y-2.5 px-4">
        {tasks.map((t) => (
          <TaskCard key={t.id} task={t} />
        ))}
      </div>
    </>
  );
}

export function TasksScreen() {
  const { data, isLoading, isError } = useTasks();
  const tasks = data ?? [];
  const attention = tasks.filter((t) => ATTENTION.includes(t.state));
  const running = tasks.filter((t) => RUNNING.includes(t.state));
  const queued = tasks.filter((t) => t.state === "queued");
  const recent = tasks.filter((t) => t.state === "succeeded" || t.state === "cancelled");
  const empty = !isLoading && !isError && tasks.length === 0;

  return (
    <div className="pb-8">
      <CompactTopBar title="Tasks" subtitle="Live · richer states with Move G′" />
      {isLoading && <p className="px-4 py-8 text-center text-sm text-ink-muted">Loading tasks…</p>}
      {isError && <p className="px-4 py-8 text-center text-sm text-bad">Couldn’t load tasks.</p>}
      {empty && <p className="px-4 py-8 text-center text-sm text-ink-muted">No tasks yet.</p>}
      <Section label="Needs attention" accent="warn" tasks={attention} />
      <Section label="Running" tasks={running} />
      <Section label="Queued" tasks={queued} />
      <Section label="Recently completed" tasks={recent} />
    </div>
  );
}
