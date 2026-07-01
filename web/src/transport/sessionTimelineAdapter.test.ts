import { describe, expect, it } from "vitest";
import { toSessionActivityTimeline } from "./sessionTimelineAdapter";
import type { RawSessionTimelineResponse } from "./rawApi";

describe("sessionTimelineAdapter - durable read model", () => {
  it("maps backend snake_case fields without dropping authority metadata", () => {
    const raw: RawSessionTimelineResponse = {
      generated_at: "2026-07-01T10:00:00Z",
      next_cursor: "2",
      coverage: { tasks: "ok", telemetry: "partial" },
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
});
