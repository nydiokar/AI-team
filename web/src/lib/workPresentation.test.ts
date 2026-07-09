import { describe, it, expect } from "vitest";
import {
  BUCKET_ORDER,
  bucketMeta,
  roleLabel,
  roleTone,
  eventTypeLabel,
  isClosedCaseStatus,
} from "./workPresentation";
import type { WorkBucket } from "../domain/work";

describe("workPresentation — bucket metadata", () => {
  it("orders buckets by attention priority (decision first, unknown last)", () => {
    expect(BUCKET_ORDER[0]).toBe("needs_decision");
    expect(BUCKET_ORDER[BUCKET_ORDER.length - 1]).toBe("unknown");
    expect(BUCKET_ORDER).toHaveLength(6);
  });

  it("gives every bucket a label, section, and tone", () => {
    for (const b of BUCKET_ORDER) {
      const m = bucketMeta(b);
      expect(m.label.length).toBeGreaterThan(0);
      expect(m.section.length).toBeGreaterThan(0);
      expect(["running", "ok", "warn", "bad", "idle"]).toContain(m.tone);
    }
  });

  it("uses attention tones for decision/blocked, idle for closed/unknown", () => {
    expect(bucketMeta("needs_decision").tone).toBe("warn");
    expect(bucketMeta("blocked").tone).toBe("bad");
    expect(bucketMeta("closed").tone).toBe("idle");
    expect(bucketMeta("unknown").tone).toBe("idle");
  });

  it("falls back to unknown metadata for a bogus bucket", () => {
    expect(bucketMeta("bogus" as WorkBucket).section).toBe(
      bucketMeta("unknown").section,
    );
  });
});

describe("workPresentation — role labels + tones", () => {
  it("labels the authoritative session roles", () => {
    expect(roleLabel("manager")).toBe("Manager");
    expect(roleLabel("worker")).toBe("Worker");
    expect(roleLabel("reviewer")).toBe("Reviewer");
    expect(roleLabel("evidence")).toBe("Evidence");
    expect(roleLabel("session")).toBe("Session");
  });

  it("tones active roles as running, review as warn, passive as idle", () => {
    expect(roleTone("manager")).toBe("running");
    expect(roleTone("reviewer")).toBe("warn");
    expect(roleTone("evidence")).toBe("idle");
  });
});

describe("workPresentation — event type humanization", () => {
  it("drops the namespace and title-cases the tail", () => {
    expect(eventTypeLabel("review.rework_requested")).toBe("Rework requested");
    expect(eventTypeLabel("flow.created")).toBe("Created");
    expect(eventTypeLabel("task.dispatched")).toBe("Dispatched");
  });

  it("handles a bare (no-namespace) type and null", () => {
    expect(eventTypeLabel("blocked")).toBe("Blocked");
    expect(eventTypeLabel(null)).toBe("Event");
  });
});

describe("workPresentation — closed case status (mirrors backend authority)", () => {
  it("matches every terminal status in work_read_model._CLOSED_STATUSES", () => {
    for (const s of ["closed", "superseded", "done", "complete", "completed"]) {
      expect(isClosedCaseStatus(s)).toBe(true);
    }
  });

  it("is case/whitespace-insensitive", () => {
    expect(isClosedCaseStatus("  CLOSED ")).toBe(true);
    expect(isClosedCaseStatus("Completed")).toBe(true);
  });

  it("treats active/blocked/absent statuses as NOT closed (never inferred)", () => {
    for (const s of ["active", "blocked", "review", "needs_decision", "", null, undefined]) {
      expect(isClosedCaseStatus(s)).toBe(false);
    }
  });
});
