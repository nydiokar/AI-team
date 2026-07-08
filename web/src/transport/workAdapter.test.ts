import { describe, it, expect } from "vitest";
import {
  caseTitle,
  toCaseSummary,
  toWorkList,
  toCaseDetail,
  toLedger,
  toCaseTimeline,
  toCaseGraph,
  normalizeSessionRole,
} from "./workAdapter";
import type {
  RawCaseSummary,
  RawCaseDetailResponse,
  RawWorkListResponse,
  RawCaseTimelineResponse,
  RawCaseGraphResponse,
} from "./rawApi";

const baseCase: RawCaseSummary = {
  flow_run_id: "flow_a1b2c3d4",
  task_id: "task_1",
  objective_lock: null,
  current_stage: "impl",
  status: "active",
  created_at: "2026-07-08T10:00:00Z",
  updated_at: "2026-07-08T11:00:00Z",
  parent_flow_run_id: null,
  dispatched_by: null,
  dispatch_file: null,
  bucket: "active",
};

describe("caseTitle — honest display from the case's OWN objective", () => {
  it("prefers <real_objective> prose", () => {
    const t = caseTitle(
      "<objective_lock><real_objective>Ship the Work tab</real_objective><literal_request>x</literal_request></objective_lock>",
      "flow_x",
    );
    expect(t).toBe("Ship the Work tab");
  });

  it("falls back through the tag priority (interpreted_task, task_name)", () => {
    expect(caseTitle("<interpreted_task>Do the thing</interpreted_task>", "f")).toBe(
      "Do the thing",
    );
    expect(caseTitle("<task_name>A28-mobile</task_name>", "f")).toBe("A28-mobile");
  });

  it("uses the first meaningful line when there is no known tag", () => {
    expect(caseTitle("Just a plain objective\nsecond line", "f")).toBe(
      "Just a plain objective",
    );
  });

  it("never fabricates a name: empty/null objective → an id-based label", () => {
    const t = caseTitle(null, "flow_a1b2c3d4");
    expect(t.startsWith("case ")).toBe(true);
    expect(t).not.toContain("<");
    expect(caseTitle("   ", "flow_a1b2c3d4")).toBe(t);
  });

  it("caps very long titles so a blob never fills the row", () => {
    const long = "x".repeat(400);
    const t = caseTitle(`<real_objective>${long}</real_objective>`, "f");
    expect(t.length).toBeLessThanOrEqual(120);
    expect(t.endsWith("…")).toBe(true);
  });
});

describe("toCaseSummary / toWorkList", () => {
  it("maps snake_case rows to the domain summary", () => {
    const s = toCaseSummary(baseCase);
    expect(s.flowRunId).toBe("flow_a1b2c3d4");
    expect(s.currentStage).toBe("impl");
    expect(s.bucket).toBe("active");
    expect(s.parentFlowRunId).toBeNull();
  });

  it("normalizes an unexpected bucket value to unknown", () => {
    const s = toCaseSummary({ ...baseCase, bucket: "bogus" as never });
    expect(s.bucket).toBe("unknown");
  });

  it("always fills all six bucket counts (missing → 0)", () => {
    const raw: RawWorkListResponse = {
      cases: [baseCase],
      bucket_counts: { active: 1 } as never,
      total: 1,
    };
    const list = toWorkList(raw);
    expect(list.bucketCounts).toEqual({
      needs_decision: 0,
      blocked: 0,
      review: 0,
      active: 1,
      closed: 0,
      unknown: 0,
    });
    expect(list.cases).toHaveLength(1);
    expect(list.total).toBe(1);
  });

  it("tolerates a totally empty payload (flag OFF → no data)", () => {
    const list = toWorkList({} as never);
    expect(list.cases).toEqual([]);
    expect(list.total).toBe(0);
    expect(list.bucketCounts.unknown).toBe(0);
  });
});

describe("toCaseDetail — coverage + ledger honesty", () => {
  const raw: RawCaseDetailResponse = {
    case: baseCase,
    record: {},
    ledger: {
      tasks: [
        {
          entity_type: "task",
          entity_id: "task_1",
          role: "root_task",
          created_by: "system",
          created_at: "t",
          metadata_json: null,
        },
      ],
      sessions: [],
      approvals: [],
      artifacts: [],
      jobs: [],
      flows: [],
      other: [],
    },
    parent: null,
    children: [{ ...baseCase, flow_run_id: "flow_child", parent_flow_run_id: "flow_a1b2c3d4" }],
    counts: { links: 1, events: 2, children: 1 },
    coverage: { has_links: true, has_events: true, has_parent: false, is_root: true },
  };

  it("groups the ledger and surfaces empty sections explicitly", () => {
    const d = toCaseDetail(raw);
    expect(d.ledger.tasks).toHaveLength(1);
    expect(d.ledger.sessions).toEqual([]);
    expect(d.ledger.approvals).toEqual([]);
  });

  it("passes authoritative coverage/lineage through without inference", () => {
    const d = toCaseDetail(raw);
    expect(d.coverage.isRoot).toBe(true);
    expect(d.coverage.hasParent).toBe(false);
    expect(d.parent).toBeNull();
    expect(d.children).toHaveLength(1);
    expect(d.children[0].flowRunId).toBe("flow_child");
  });

  it("toLedger tolerates missing sections", () => {
    const l = toLedger(undefined);
    expect(l.tasks).toEqual([]);
    expect(l.other).toEqual([]);
  });
});

describe("toCaseTimeline", () => {
  it("maps events + evidence and falls back to a computed count", () => {
    const raw: RawCaseTimelineResponse = {
      flow_run_id: "flow_a1b2c3d4",
      events: [
        {
          id: 7,
          event_type: "flow.created",
          actor: "system",
          from_state: null,
          to_state: "objective_lock",
          entity_type: "task",
          entity_id: "task_1",
          payload_json: null,
          created_at: "t",
        },
      ],
      evidence: [],
      event_count: undefined as never,
    };
    const tl = toCaseTimeline(raw);
    expect(tl.events).toHaveLength(1);
    expect(tl.events[0].id).toBe("7");
    expect(tl.events[0].eventType).toBe("flow.created");
    expect(tl.eventCount).toBe(1);
  });
});

describe("toCaseGraph", () => {
  it("maps nodes (with derived titles) and edges", () => {
    const raw: RawCaseGraphResponse = {
      flow_run_id: "flow_a1b2c3d4",
      nodes: [
        {
          flow_run_id: "flow_a1b2c3d4",
          rel: "self",
          current_stage: "impl",
          status: "active",
          bucket: "active",
          objective_lock: "<real_objective>Root case</real_objective>",
        },
        {
          flow_run_id: "flow_child",
          rel: "child",
          current_stage: null,
          status: null,
          bucket: "unknown",
          objective_lock: null,
        },
      ],
      edges: [{ from: "flow_a1b2c3d4", to: "flow_child", role: "child_flow" }],
    };
    const g = toCaseGraph(raw);
    expect(g.nodes[0].title).toBe("Root case");
    expect(g.nodes[0].rel).toBe("self");
    expect(g.nodes[1].title.startsWith("case ")).toBe(true);
    expect(g.edges[0]).toEqual({ from: "flow_a1b2c3d4", to: "flow_child", role: "child_flow" });
  });
});

describe("normalizeSessionRole — authoritative role, never invented", () => {
  it("keeps the named session roles (case-insensitive)", () => {
    expect(normalizeSessionRole("manager")).toBe("manager");
    expect(normalizeSessionRole("WORKER")).toBe("worker");
    expect(normalizeSessionRole("reviewer")).toBe("reviewer");
    expect(normalizeSessionRole("evidence")).toBe("evidence");
  });

  it("falls back to the generic 'session' for empty/unknown roles", () => {
    expect(normalizeSessionRole(null)).toBe("session");
    expect(normalizeSessionRole("")).toBe("session");
    expect(normalizeSessionRole("root_task")).toBe("session");
  });
});
