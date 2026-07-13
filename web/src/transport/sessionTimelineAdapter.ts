import type {
  ContextFill,
  SessionActivityItem,
  SessionActivityTimeline,
} from "../domain/models";
import type {
  RawContextFill,
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
    detail: raw.detail ?? {},
    rawRefs: raw.raw_refs ?? {},
  };
}

const UNKNOWN_CONTEXT_FILL: ContextFill = {
  contextUsedRatio: null,
  contextWindowTokens: null,
  contextRemainingTokens: null,
  contextWindowSource: "unknown",
  reason: "no_turns_observed",
};

export function toContextFill(raw: RawContextFill | null | undefined): ContextFill {
  if (!raw) return UNKNOWN_CONTEXT_FILL;
  return {
    contextUsedRatio: raw.context_used_ratio,
    contextWindowTokens: raw.context_window_tokens,
    contextRemainingTokens: raw.context_remaining_tokens,
    contextWindowSource: raw.context_window_source,
    reason: raw.reason,
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
    contextFill: toContextFill(raw.context_fill),
  };
}
