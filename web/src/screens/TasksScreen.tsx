/**
 * Tasks screen — FIXTURES in UI-1 (🔵 MOCK-OK). Global cross-session inbox
 * (spec §7.4), sectioned Needs attention / Running / Queued / Recently completed.
 * Live sectioning over real lifecycle data is Move G′ (gap-doc §4).
 */
import { Link } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { TaskStatusChip } from "../components/ui/StatusChip";
import { taskFixtures } from "../fixtures/tasks";
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
  const tasks = taskFixtures;
  return (
    <div className="pb-8">
      <CompactTopBar title="Tasks" subtitle="Mocked · live with Move G′" />
      <Section label="Needs attention" accent="warn" tasks={tasks.filter((t) => ATTENTION.includes(t.state))} />
      <Section label="Running" tasks={tasks.filter((t) => RUNNING.includes(t.state))} />
      <Section label="Queued" tasks={tasks.filter((t) => t.state === "queued")} />
      <Section
        label="Recently completed"
        tasks={tasks.filter((t) => t.state === "succeeded" || t.state === "cancelled")}
      />
    </div>
  );
}
