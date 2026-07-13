import { describe, expect, it } from "vitest";
import { toContextFill, toSessionActivityTimeline } from "./sessionTimelineAdapter";
import type { RawSessionTimelineResponse } from "./rawApi";

describe("sessionTimelineAdapter - durable read model", () => {
  it("maps backend snake_case fields without dropping authority metadata", () => {
    const raw: RawSessionTimelineResponse = {
      generated_at: "2026-07-01T10:00:00Z",
      next_cursor: "2",
      coverage: { tasks: "ok", telemetry: "partial" },
      context_fill: {
        context_used_ratio: 0.42,
        context_window_tokens: 200000,
        context_remaining_tokens: 116000,
        context_window_source: "known",
      },
      items: [
        {
          id: "task:t1",
          kind: "task_state",
          source: "mesh_tasks",
          durability: "durable",
          timestamp: "2026-07-01T09:59:00Z",
          session_id: "sess_1",
          task_id: "t1",
          turn_id: null,
          job_id: null,
          node_id: "node_a",
          backend: "codex",
          status: "stale_claim",
          confidence: "medium",
          staleness: "stale",
          summary: "Task claim is stale",
          detail: { reason: "No fresh worker proof" },
          raw_refs: { task_id: "t1", heartbeat_age_sec: 999 },
        },
      ],
    };

    const out = toSessionActivityTimeline(raw);

    expect(out.nextCursor).toBe("2");
    expect(out.generatedAt).toBe("2026-07-01T10:00:00Z");
    expect(out.coverage.telemetry).toBe("partial");
    expect(out.contextFill).toEqual({
      contextUsedRatio: 0.42,
      contextWindowTokens: 200000,
      contextRemainingTokens: 116000,
      contextWindowSource: "known",
      reason: undefined,
    });
    expect(out.items[0]).toMatchObject({
      id: "task:t1",
      kind: "task_state",
      source: "mesh_tasks",
      durability: "durable",
      sessionId: "sess_1",
      taskId: "t1",
      nodeId: "node_a",
      status: "stale_claim",
      confidence: "medium",
      staleness: "stale",
      rawRefs: { task_id: "t1", heartbeat_age_sec: 999 },
    });
  });

  it("preserves unknown kinds and states for degraded rendering", () => {
    const raw: RawSessionTimelineResponse = {
      generated_at: "2026-07-01T10:00:00Z",
      next_cursor: null,
      coverage: {},
      context_fill: {
        context_used_ratio: null,
        context_window_tokens: null,
        context_remaining_tokens: null,
        context_window_source: "unknown",
        reason: "no_turns_observed",
      },
      items: [
        {
          id: "future:1",
          kind: "future_kind",
          source: "future_source",
          durability: "diagnostic",
          timestamp: "2026-07-01T10:00:00Z",
          session_id: null,
          task_id: null,
          turn_id: null,
          job_id: null,
          node_id: null,
          backend: null,
          status: "future_status",
          confidence: "low",
          staleness: "unknown",
          summary: "Future event",
          detail: {},
          raw_refs: {},
        },
      ],
    };

    const out = toSessionActivityTimeline(raw);

    expect(out.items[0].kind).toBe("future_kind");
    expect(out.items[0].status).toBe("future_status");
    expect(out.items[0].durability).toBe("diagnostic");
  });

  it("toContextFill never fabricates a fill percentage when the source is unknown", () => {
    expect(
      toContextFill({
        context_used_ratio: null,
        context_window_tokens: null,
        context_remaining_tokens: null,
        context_window_source: "unknown",
        reason: "context_window_unknown_for_backend_model",
      }),
    ).toEqual({
      contextUsedRatio: null,
      contextWindowTokens: null,
      contextRemainingTokens: null,
      contextWindowSource: "unknown",
      reason: "context_window_unknown_for_backend_model",
    });

    expect(toContextFill(null)).toEqual({
      contextUsedRatio: null,
      contextWindowTokens: null,
      contextRemainingTokens: null,
      contextWindowSource: "unknown",
      reason: "no_turns_observed",
    });
  });
});
