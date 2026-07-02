import type { SessionActivityItem } from "../domain/models";

export type ActivityTone = "running" | "ok" | "warn" | "bad" | "idle";

export interface ActivityStatusView {
  label: string;
  tone: ActivityTone;
}

const STATE_VIEW: Record<string, ActivityStatusView> = {
  accepted: { label: "Accepted", tone: "idle" },
  queued: { label: "Queued", tone: "idle" },
  claimed: { label: "Claimed", tone: "running" },
  worker_running: { label: "Worker running", tone: "running" },
  backend_running: { label: "Backend running", tone: "running" },
  waiting_for_input: { label: "Waiting", tone: "warn" },
  waiting_for_approval: { label: "Needs approval", tone: "warn" },
  cancel_requested: { label: "Cancelling", tone: "warn" },
  cancelled: { label: "Cancelled", tone: "idle" },
  completed: { label: "Completed", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  detached: { label: "Detached", tone: "warn" },
  stale_claim: { label: "Stale claim", tone: "warn" },
  worker_unknown: { label: "Worker unknown", tone: "warn" },
  recovered: { label: "Recovered", tone: "ok" },
  running: { label: "Running", tone: "running" },
  done: { label: "Done", tone: "ok" },
  lost: { label: "Lost", tone: "warn" },
  pending: { label: "Pending", tone: "idle" },
};

export function activityStatusView(item: SessionActivityItem): ActivityStatusView {
  const status: string | null = item.status;
  if (status && STATE_VIEW[status]) return STATE_VIEW[status];
  if (item.staleness === "stale" || item.confidence === "low") {
    return { label: status ? humanize(status) : "Uncertain", tone: "warn" };
  }
  return { label: status ? humanize(status) : humanize(item.kind), tone: "idle" };
}

export function activityKindLabel(kind: string): string {
  switch (kind) {
    case "task_state":
      return "Task";
    case "worker_state":
      return "Worker";
    case "turn_event":
      return "Turn";
    case "artifact":
      return "Artifact";
    case "file_change":
      return "File";
    case "job_state":
      return "Job";
    case "approval":
      return "Approval";
    case "recovery":
      return "Recovery";
    case "system_notice":
      return "System";
    default:
      return humanize(kind);
  }
}

function humanize(value: string): string {
  return value
    .replace(/[_.-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^./, (c) => c.toUpperCase());
}
