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
    ...(raw.context_economics ? { contextEconomics: {
      compactionCoverage: raw.context_economics.compaction_coverage,
      recoveryCoverage: raw.context_economics.recovery_coverage,
      recommendation: raw.context_economics.recommendation,
      threshold: raw.context_economics.threshold,
      coldWriteUsd: raw.context_economics.cold_write_usd,
      warmReadUsd: raw.context_economics.warm_read_usd,
      compactions: raw.context_economics.compactions.map(row => ({
        taskId: row.task_id, status: row.status, reclaimedTokens: row.reclaimed_tokens,
        reclaimedRatio: row.reclaimed_ratio, avoidedReadUsdPerRequest: row.avoided_read_usd_per_request,
        observedCompactionUsd: row.observed_compaction_usd, breakEvenWarmRequests: row.break_even_warm_requests,
      })),
      recoveries: raw.context_economics.recoveries.map(row => ({
        taskId: row.task_id, status: row.status, observedUsd: row.observed_usd, costBasis: row.cost_basis,
        priorSessionId: row.prior_session_id, sessionId: row.session_id, coldResumeUsd: row.estimate?.usd,
        inputTokens: row.tokens?.input_tokens, outputTokens: row.tokens?.output_tokens,
        cacheReadTokens: row.tokens?.cache_read_tokens, cacheCreationTokens: row.tokens?.cache_creation_tokens,
        contextUsedRatio: row.tokens?.context_used_ratio,
      })),
      proposals: raw.context_economics.proposals.map(row => ({
        approvalId: row.approval_id, status: row.status, coldResumeUsd: row.estimate?.usd ?? null,
        contextTokens: row.estimate?.cache_creation_tokens, contextUsedRatio: row.estimate?.economics?.context_used_ratio,
      })),
    }} : {}),
  };
}
