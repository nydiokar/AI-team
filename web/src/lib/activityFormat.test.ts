import { describe, expect, it } from "vitest";
import {
  enrichLine,
  indexSessions,
  isSystemActivity,
} from "./activityFormat";
import type { Session } from "../domain/models";
import type { LogLine } from "../transport/eventLog";

const session: Session = {
  id: "sess_1",
  backend: "codex",
  workspace: { path: "C:\\repo", targetId: "node_a" },
  backendSessionId: null,
  lifecycle: "open",
  opState: "running",
  needsAttention: false,
  model: null,
  defaultModel: null,
  lastTaskId: "task_1",
  lastSummary: "",
  lastFilesModified: [],
  originChannel: "web",
  originKind: "session",
  updatedAt: "2026-07-01T10:00:00Z",
};

function line(overrides: Partial<LogLine>): LogLine {
  return {
    id: "line_1",
    at: "2026-07-01T10:00:00Z",
    kind: "task",
    text: "task running",
    severity: "info",
    sessionId: null,
    taskId: null,
    ...overrides,
  };
}

describe("activityFormat system ownership", () => {
  it("keeps session-owned activity out of the System feed", () => {
    const idx = indexSessions([session]);

    expect(isSystemActivity(enrichLine(line({ sessionId: "sess_1" }), idx))).toBe(false);
    expect(isSystemActivity(enrichLine(line({ taskId: "task_1" }), idx))).toBe(false);
  });

  it("keeps unowned infrastructure activity in System", () => {
    const idx = indexSessions([session]);
    const enriched = enrichLine(
      line({ kind: "mesh", text: "mesh degraded", severity: "warning" }),
      idx,
    );

    expect(isSystemActivity(enriched)).toBe(true);
  });
});
