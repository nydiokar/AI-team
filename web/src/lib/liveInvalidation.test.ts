import { describe, expect, it, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import {
  collectLiveInvalidationTargets,
  invalidateAllLive,
  invalidateLiveTargets,
  invalidateRouteTarget,
} from "./liveInvalidation";
import type { RawEvent } from "../transport/rawApi";

describe("liveInvalidation", () => {
  it("collects session, case, task, and approval hints from raw SSE events", () => {
    const target = collectLiveInvalidationTargets([
      {
        event: "mesh_result",
        timestamp: "2026-08-24T00:00:00Z",
        session_id: "s1",
        task_id: "t1",
        case_id: "c1",
      },
      {
        event: "approval_requested",
        timestamp: "2026-08-24T00:00:01Z",
        flow_run_id: "c2",
      },
    ]);

    expect([...target.sessions]).toEqual(["s1"]);
    expect([...target.cases]).toEqual(["c1", "c2"]);
    expect(target.tasks).toBe(true);
    expect(target.approvals).toBe(true);
  });

  it("invalidates the session read model for notification route handoff", () => {
    const invalidateQueries = vi.fn();
    const client = { invalidateQueries } as unknown as QueryClient;

    invalidateRouteTarget(client, "/sessions/s%201");

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["sessions"] });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["session-messages", "s 1"],
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["session-activity", "s 1"],
    });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["approvals"] });
  });

  it("invalidates the case read model for notification route handoff", () => {
    const invalidateQueries = vi.fn();
    const client = { invalidateQueries } as unknown as QueryClient;

    invalidateRouteTarget(client, "/work/case%2F1");

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["work-list"] });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["work-detail", "case/1"],
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["work-roster", "case/1"],
    });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("invalidates the artifacts list when a task-bearing event arrives (A81 coverage)", () => {
    // A new artifact appears exactly when a task produces a result, so a task_id
    // event must refresh the artifacts list — otherwise it goes stale once its
    // aggressive poll drops to the safety net.
    const invalidateQueries = vi.fn();
    const client = { invalidateQueries } as unknown as QueryClient;

    invalidateLiveTargets(client, {
      sessions: new Set(),
      cases: new Set(),
      tasks: true,
      approvals: false,
    });

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["artifacts"] });
  });

  it("invalidateAllLive refreshes every live read-model (reconnect resync)", () => {
    const invalidateQueries = vi.fn();
    const client = { invalidateQueries } as unknown as QueryClient;

    invalidateAllLive(client);

    // A representative spread across the three surfaces + the trap key.
    for (const key of [
      ["sessions"],
      ["session-messages"],
      ["session-turns"],
      ["artifacts"],
      ["approvals"],
      ["work-list"],
      ["work-roster"],
      ["case-resume-state"],
    ]) {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: key });
    }
  });

  it("ignores raw events without useful correlation ids", () => {
    const target = collectLiveInvalidationTargets([
      { event: "heartbeat", timestamp: "2026-08-24T00:00:00Z" },
    ] satisfies RawEvent[]);

    expect(target.sessions.size).toBe(0);
    expect(target.cases.size).toBe(0);
    expect(target.tasks).toBe(false);
    expect(target.approvals).toBe(false);
  });
});
