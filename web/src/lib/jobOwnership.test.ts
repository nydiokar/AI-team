import { describe, expect, it } from "vitest";
import { filterJobsByOwnership } from "./jobOwnership";
import type { RawJob } from "../transport/rawApi";

function job(id: string, sessionId: string | null): RawJob {
  return {
    id,
    session_id: sessionId,
    node_id: "node_a",
    label: id,
    status: "running",
    pid: 123,
    last_checked_at: null,
    last_probe_error: null,
    exit_code: null,
    notify: null,
    notify_agent: null,
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-01T10:00:00Z",
  };
}

describe("jobOwnership", () => {
  it("keeps session-owned jobs out of the unowned System list", () => {
    const jobs = [job("owned", "sess_1"), job("unowned", null)];

    expect(filterJobsByOwnership(jobs, "unowned").map((j) => j.id)).toEqual([
      "unowned",
    ]);
  });

  it("preserves backward-compatible all jobs mode", () => {
    const jobs = [job("owned", "sess_1"), job("unowned", null)];

    expect(filterJobsByOwnership(jobs, "all").map((j) => j.id)).toEqual([
      "owned",
      "unowned",
    ]);
  });
});
