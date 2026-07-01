import type {
  SessionActivityItem,
  SessionActivityTimeline,
} from "../domain/models";
import type {
  RawSessionTimelineItem,
  RawSessionTimelineResponse,
} from "./rawApi";

export function toSessionActivityItem(
  raw: RawSessionTimelineItem,
): SessionActivityItem {
  return {
    id: raw.id,
    kind: raw.kind,
    source: raw.source,
    durability: raw.durability,
    timestamp: raw.timestamp,
    sessionId: raw.session_id,
    taskId: raw.task_id,
    turnId: raw.turn_id,
    jobId: raw.job_id,
    nodeId: raw.node_id,
    backend: raw.backend,
    status: raw.status,
    confidence: raw.confidence,
    staleness: raw.staleness,
    summary: raw.summary,
    detail: raw.detail,
    rawRefs: raw.raw_refs ?? {},
  };
}

export function toSessionActivityTimeline(
  raw: RawSessionTimelineResponse,
): SessionActivityTimeline {
  return {
    items: (raw.items ?? []).map(toSessionActivityItem),
    nextCursor: raw.next_cursor,
    generatedAt: raw.generated_at,
    coverage: raw.coverage ?? {},
  };
}
