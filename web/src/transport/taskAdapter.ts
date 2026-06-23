/**
 * RawTask (mesh_tasks row) → canonical Task.
 *
 * The mesh status set is pending|claimed|completed|failed|failed_node_offline —
 * NOT the 9-state UI lifecycle (that's Move G′, ❌ MISSING). We map onto the
 * subset of TaskState a real turn can reach today; the richer states only appear
 * once G′ lands. `progressPct` is intentionally absent (⛔ task.progress).
 */
import type { Task } from "../domain/models";
import type { TaskState } from "../domain/status";
import type { RawTask } from "./rawApi";

/** mesh_tasks.status → canonical TaskState (gap-doc §4). */
export function deriveTaskState(meshStatus: string): TaskState {
  switch (meshStatus) {
    case "pending":
      return "queued";
    case "claimed":
      return "dispatching";
    case "processing": // TaskStatus enum value, in case a row carries it
      return "running";
    case "completed":
      return "succeeded";
    case "failed":
    case "failed_node_offline":
      return "failed";
    case "cancelled":
      return "cancelled";
    default:
      return "connection_unknown";
  }
}

/** Best-effort objective: action name + parsed payload summary if present. */
function deriveObjective(raw: RawTask): string {
  try {
    const payload = raw.payload ? JSON.parse(raw.payload) : {};
    const prompt = typeof payload?.prompt === "string" ? payload.prompt : "";
    if (prompt) return prompt.length > 120 ? prompt.slice(0, 117) + "…" : prompt;
  } catch {
    /* fall through to action */
  }
  return raw.action;
}

export function toTask(raw: RawTask): Task {
  return {
    id: raw.id,
    sessionId: raw.session_id,
    backend: raw.backend,
    targetId: raw.machine_id ?? raw.claimed_by ?? null,
    state: deriveTaskState(raw.status),
    objective: deriveObjective(raw),
    createdAt: raw.created_at,
    updatedAt: raw.updated_at,
    completedAt: raw.completed_at,
    artifactPath: raw.artifact_path,
    error: raw.error,
  };
}

export function toTasks(raws: RawTask[]): Task[] {
  return raws.map(toTask);
}
