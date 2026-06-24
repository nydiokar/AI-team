/**
 * Tasks screen — LIVE sectioned (Move G′). Bound to /api/tasks?sectioned=true via
 * useTaskSections; the backend owns the supervised lifecycle bucketing
 * (attention/running/queued/recent) by overlaying each task's owning-session
 * status onto the mesh status. So `waiting_for_input` now correctly lands in
 * `attention` — the bucket the flat mesh status couldn't reach in UI-2.
 *
 * Approvals (`waiting_for_approval`) remain gated on Move H — the backend names
 * the state but has no live source for it yet.
 */
import { Link } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { TaskStatusChip } from "../components/ui/StatusChip";
import { useTaskSections } from "../hooks/useLiveData";
import type { Task } from "../domain/models";

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
  const { data, isLoading, isError } = useTaskSections();
  const total = data
    ? data.attention.length + data.running.length + data.queued.length + data.recent.length
    : 0;
  const empty = !isLoading && !isError && total === 0;

  return (
    <div className="pb-8">
      <CompactTopBar title="Tasks" subtitle="Live · supervised lifecycle" />
      {isLoading && <p className="px-4 py-8 text-center text-sm text-ink-muted">Loading tasks…</p>}
      {isError && <p className="px-4 py-8 text-center text-sm text-bad">Couldn’t load tasks.</p>}
      {empty && <p className="px-4 py-8 text-center text-sm text-ink-muted">No tasks yet.</p>}
      <Section label="Needs attention" accent="warn" tasks={data?.attention ?? []} />
      <Section label="Running" tasks={data?.running ?? []} />
      <Section label="Queued" tasks={data?.queued ?? []} />
      <Section label="Recently completed" tasks={data?.recent ?? []} />
    </div>
  );
}
