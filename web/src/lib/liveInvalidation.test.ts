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
    expect([...target.activitySessions]).toEqual([]);
    expect([...target.cases]).toEqual(["c1", "c2"]);
    expect(target.tasks).toBe(true);
    expect(target.approvals).toBe(true);
  });

  it("scopes a task_activity progress ping to activity-only (no transcript churn)", () => {
    const target = collectLiveInvalidationTargets([
      {
        event: "task_activity",
        timestamp: "2026-09-16T00:00:00Z",
        session_id: "s1",
        task_id: "t1",
        label: "Using Bash",
      },
    ]);

    // A progress ping must NOT touch the full session read-models, the task list,
    // cases, or approvals — only the live activity ticker.
    expect([...target.sessions]).toEqual([]);
    expect([...target.activitySessions]).toEqual(["s1"]);
    expect(target.tasks).toBe(false);
    expect([...target.cases]).toEqual([]);
    expect(target.approvals).toBe(false);
  });

  it("invalidateLiveTargets refreshes ONLY session-activity for a progress ping", () => {
    const invalidateQueries = vi.fn();
    const client = { invalidateQueries } as unknown as QueryClient;

    invalidateLiveTargets(client, {
      sessions: new Set(),
      activitySessions: new Set(["s1"]),
      cases: new Set(),
      tasks: false,
      approvals: false,
    });

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["session-activity", "s1"],
    });
    // The expensive transcript reads and the session list must NOT be invalidated.
    expect(invalidateQueries).not.toHaveBeenCalledWith({
      queryKey: ["session-messages", "s1"],
    });
    expect(invalidateQueries).not.toHaveBeenCalledWith({
      queryKey: ["session-turns", "s1"],
    });
    expect(invalidateQueries).not.toHaveBeenCalledWith({
      queryKey: ["session-usage", "s1"],
    });
    expect(invalidateQueries).not.toHaveBeenCalledWith({ queryKey: ["sessions"] });
    expect(invalidateQueries).not.toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("a real event supersedes a same-batch progress ping for the same session", () => {
    // task_activity + mesh_result for s1 in one batch → s1 gets the FULL refresh,
    // not the activity-only path (no double-invalidation, no missed transcript).
    const target = collectLiveInvalidationTargets([
      { event: "task_activity", timestamp: "2026-09-16T00:00:00Z", session_id: "s1" },
      {
        event: "mesh_result",
        timestamp: "2026-09-16T00:00:01Z",
        session_id: "s1",
        task_id: "t1",
      },
    ]);
    expect([...target.sessions]).toEqual(["s1"]);
    expect(target.tasks).toBe(true);

    const invalidateQueries = vi.fn();
    const client = { invalidateQueries } as unknown as QueryClient;
    invalidateLiveTargets(client, target);

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["session-messages", "s1"],
    });
    // session-activity for s1 is invalidated exactly once (via the full path),
    // not again by the activity-only loop.
    const activityCalls = invalidateQueries.mock.calls.filter(
      ([arg]) =>
        Array.isArray(arg?.queryKey) &&
        arg.queryKey[0] === "session-activity" &&
        arg.queryKey[1] === "s1",
    );
    expect(activityCalls).toHaveLength(1);
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
      activitySessions: new Set(),
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
