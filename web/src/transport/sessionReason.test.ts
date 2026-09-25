import { describe, it, expect } from "vitest";
import { deriveReason, toSession } from "./sessionAdapter";
import type { RawSessionView } from "./rawApi";

const base: RawSessionView = {
  session_id: "s1",
  backend: "claude",
  repo_path: "/tmp/repo",
  status: "awaiting_input",
  machine_id: "node_a",
  backend_session_id: "",
  model: null,
  effort: null,
  default_model: null,
  last_task_id: "",
  last_summary: "",
  last_files_modified: [],
  needs_input: true,
  is_active: true,
  origin_channel: "web",
  origin_kind: "user",
  updated_at: "2026-09-25T00:00:00Z",
};

describe("A83 sessionAdapter — secondary reason", () => {
  it("maps a known reason kind through to the domain shape", () => {
    const s = toSession({
      ...base,
      reason: { kind: "waiting_workers", confidence: "high" },
    });
    expect(s.reason).toEqual({ kind: "waiting_workers", confidence: "high", detail: null });
  });

  it("carries node_offline detail", () => {
    const r = deriveReason({
      ...base,
      status: "pinned_node_offline",
      reason: { kind: "node_offline", confidence: "high", detail: "Horse" },
    });
    expect(r).toEqual({ kind: "node_offline", confidence: "high", detail: "Horse" });
  });

  it("normalizes an unknown/absent kind to null (forward-compatible)", () => {
    expect(deriveReason(base)).toBeNull();
    expect(deriveReason({ ...base, reason: null })).toBeNull();
    expect(
      deriveReason({ ...base, reason: { kind: "some_future_kind", confidence: "high" } }),
    ).toBeNull();
  });

  it("defaults an unexpected confidence to high", () => {
    const r = deriveReason({
      ...base,
      reason: { kind: "open_case_idle", confidence: "weird" },
    });
    expect(r?.confidence).toBe("high");
  });

  it("open_case_idle keeps its medium confidence", () => {
    const r = deriveReason({
      ...base,
      reason: { kind: "open_case_idle", confidence: "medium" },
    });
    expect(r).toEqual({ kind: "open_case_idle", confidence: "medium", detail: null });
  });
});
