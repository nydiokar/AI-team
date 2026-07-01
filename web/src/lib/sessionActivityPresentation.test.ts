import { describe, expect, it } from "vitest";
import {
  activityKindLabel,
  activityStatusView,
} from "./sessionActivityPresentation";
import type { SessionActivityItem } from "../domain/models";

function item(status: string | null, overrides: Partial<SessionActivityItem> = {}): SessionActivityItem {
  return {
    id: "i1",
    kind: "task_state",
    source: "mesh_tasks",
    durability: "durable",
    timestamp: "2026-07-01T10:00:00Z",
    sessionId: "sess_1",
    taskId: "task_1",
    turnId: null,
    jobId: null,
    nodeId: null,
    backend: "codex",
    status,
    confidence: "high",
    staleness: "fresh",
    summary: "summary",
    detail: null,
    rawRefs: {},
    ...overrides,
  };
}

describe("sessionActivityPresentation", () => {
  it("labels uncertain task states distinctly instead of as running or failed", () => {
    expect(activityStatusView(item("stale_claim"))).toEqual({
      label: "Stale claim",
      tone: "warn",
    });
    expect(activityStatusView(item("worker_unknown"))).toEqual({
      label: "Worker unknown",
      tone: "warn",
    });
    expect(activityStatusView(item("detached"))).toEqual({
      label: "Detached",
      tone: "warn",
    });
    expect(activityStatusView(item("recovered"))).toEqual({
      label: "Recovered",
      tone: "ok",
    });
  });

  it("treats stale or low-confidence unknown statuses as warnings", () => {
    expect(
      activityStatusView(item("future_state", { confidence: "low" })),
    ).toEqual({ label: "Future state", tone: "warn" });
    expect(
      activityStatusView(item(null, { staleness: "stale" })),
    ).toEqual({ label: "Uncertain", tone: "warn" });
  });

  it("keeps durable timeline kinds readable", () => {
    expect(activityKindLabel("job_state")).toBe("Job");
    expect(activityKindLabel("future_kind")).toBe("Future kind");
  });
});
