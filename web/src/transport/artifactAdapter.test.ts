import { describe, it, expect } from "vitest";
import { toArtifacts, toArtifactDetail } from "./artifactAdapter";
import type {
  RawArtifactSummary,
  RawArtifactDetailResponse,
} from "./rawApi";

const summary: RawArtifactSummary = {
  task_id: "task_abc",
  artifact_path: "results/task_abc.json",
  success: true,
  timestamp: "2026-06-24T00:00:00",
  file_count: 2,
  files_modified: ["a.py", "b.py"],
  has_changes: true,
  session_id: "sess_1",
  parent_task_id: null,
};

describe("artifactAdapter — summary → Artifact", () => {
  it("maps a result summary to a canonical Artifact", () => {
    const [a] = toArtifacts([summary]);
    expect(a.id).toBe("task_abc");
    expect(a.taskId).toBe("task_abc");
    expect(a.sessionId).toBe("sess_1");
    expect(a.kind).toBe("result");
    expect(a.path).toBe("results/task_abc.json");
  });
});

describe("artifactAdapter — detail → RemoteFile changes", () => {
  it("narrows backend change strings to the domain union + carries line counts", () => {
    const res: RawArtifactDetailResponse = {
      artifact: {
        task_id: "task_abc",
        success: true,
        timestamp: "2026-06-24T00:00:00",
        execution_time: 1.2,
        errors: [],
        files_modified: ["a.py"],
        file_changes: null,
        session_id: "sess_1",
        parent_task_id: null,
      },
      files: [
        { path: "a.py", change: "added", added: 10, deleted: 0 },
        { path: "b.py", change: "modified", added: 3, deleted: 1 },
        { path: "c.py", change: "deleted", added: 0, deleted: 5 },
        { path: "d.py", change: "weird", added: null, deleted: null },
      ],
    };
    const detail = toArtifactDetail(res);
    expect(detail.files.map((f) => f.change)).toEqual([
      "added",
      "modified",
      "deleted",
      "modified", // unknown → safe default
    ]);
    expect(detail.files.every((f) => f.sessionId === "sess_1")).toBe(true);
    expect(detail.lineCounts[0]).toEqual({ added: 10, deleted: 0 });
    expect(detail.lineCounts[3]).toEqual({ added: null, deleted: null });
    expect(detail.success).toBe(true);
  });
});
