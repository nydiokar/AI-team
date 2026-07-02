import { describe, it, expect } from "vitest";
import { toLogLine, toLogLines } from "./eventLog";
import type { StampedEvent } from "../hooks/useEventStream";
import type { GatewayEvent } from "../domain/events";

function stamp(event: GatewayEvent, rawKey = "k"): StampedEvent {
  return { rawKey, at: "2026-06-24T00:00:00Z", event };
}

describe("eventLog — GatewayEvent → LogLine", () => {
  it("passes system.notice severity/text/correlation through", () => {
    const line = toLogLine(
      stamp({
        type: "system.notice",
        notice: {
          id: "n1",
          sessionId: "s1",
          taskId: "t1",
          kind: "mesh_dispatch",
          text: "mesh dispatch @horse",
          severity: "info",
          timestamp: "2026-06-24T00:00:00Z",
        },
      }),
    );
    expect(line.severity).toBe("info");
    expect(line.kind).toBe("mesh_dispatch");
    expect(line.text).toBe("mesh dispatch @horse");
    expect(line.sessionId).toBe("s1");
    expect(line.taskId).toBe("t1");
  });

  it("maps task.state_changed → error severity on failure, info otherwise", () => {
    expect(
      toLogLine(stamp({ type: "task.state_changed", taskId: "t9", state: "failed" })).severity,
    ).toBe("error");
    const ok = toLogLine(stamp({ type: "task.state_changed", sessionId: "s9", taskId: "t9", state: "running" }));
    expect(ok.severity).toBe("info");
    expect(ok.sessionId).toBe("s9");
    expect(ok.taskId).toBe("t9");
    expect(ok.text).toBe("task running");
  });

  it("maps run.cancelled → warning and approval.resolved by decision", () => {
    expect(toLogLine(stamp({ type: "run.cancelled", runId: "r1" })).severity).toBe("warning");
    expect(
      toLogLine(stamp({ type: "approval.resolved", approvalId: "a1", decision: "granted" })).severity,
    ).toBe("success");
    expect(
      toLogLine(stamp({ type: "approval.resolved", approvalId: "a1", decision: "rejected" })).severity,
    ).toBe("warning");
  });

  it("maps target connect/disconnect to success/warning", () => {
    expect(toLogLine(stamp({ type: "target.connected", targetId: "horse" })).severity).toBe("success");
    expect(
      toLogLine(stamp({ type: "target.disconnected", targetId: "horse" })).severity,
    ).toBe("warning");
  });

  it("preserves session and task correlation for filtered typed events", () => {
    const cancelled = toLogLine(
      stamp({ type: "run.cancelled", runId: "r1", sessionId: "s1", taskId: "t1" }),
    );
    const approval = toLogLine(
      stamp({
        type: "approval.resolved",
        approvalId: "a1",
        decision: "rejected",
        sessionId: "s1",
        taskId: "t1",
      }),
    );

    expect(cancelled.sessionId).toBe("s1");
    expect(cancelled.taskId).toBe("t1");
    expect(approval.sessionId).toBe("s1");
    expect(approval.taskId).toBe("t1");
  });

  it("never produces a blank row for other typed variants (no throw)", () => {
    const line = toLogLine(
      stamp({ type: "connection.state_changed", state: "reconnecting" }),
    );
    expect(line.kind).toBe("connection");
    expect(line.text).toBe("connection state_changed");
    expect(line.severity).toBe("info");
  });

  it("carries the rawKey as the line id (dedupe identity preserved)", () => {
    const lines = toLogLines([
      stamp({ type: "run.cancelled", runId: "r1" }, "key-1"),
      stamp({ type: "run.cancelled", runId: "r2" }, "key-2"),
    ]);
    expect(lines.map((l) => l.id)).toEqual(["key-1", "key-2"]);
  });
});
