import { describe, it, expect } from "vitest";
import { toSessions, deriveLifecycle, deriveOpState } from "./sessionAdapter";
import { toTargets, deriveHealth } from "./nodeAdapter";
import { toTasks, toTaskSections } from "./taskAdapter";
import { adaptEvents, adaptEvent } from "./eventAdapter";
import { toApproval } from "./approvalAdapter";
import {
  rawSessions,
  rawNodes,
  rawTasks,
  rawEvents,
} from "../fixtures/rawFixtures";

describe("sessionAdapter — lifecycle/op split (gap-doc §3)", () => {
  it("splits the flat backend status into lifecycle + op state", () => {
    const sessions = toSessions(rawSessions);
    const busy = sessions.find((s) => s.id === "sess_gateway_ui")!;
    expect(busy.lifecycle).toBe("open");
    expect(busy.opState).toBe("running"); // busy → running
    const closed = sessions.find((s) => s.id === "sess_closed_migration")!;
    expect(closed.lifecycle).toBe("closed");
  });

  it("derives needsAttention only for open sessions needing a human", () => {
    const sessions = toSessions(rawSessions);
    expect(sessions.find((s) => s.id === "sess_review_build")!.needsAttention).toBe(true); // awaiting_input
    expect(sessions.find((s) => s.id === "sess_deploy_failed")!.needsAttention).toBe(true); // error
    expect(sessions.find((s) => s.id === "sess_gateway_ui")!.needsAttention).toBe(false); // busy
  });

  it("maps each backend status correctly", () => {
    expect(deriveOpState({ status: "awaiting_input" } as never)).toBe("waiting_for_input");
    expect(deriveOpState({ status: "error" } as never)).toBe("failed_attention");
    expect(deriveLifecycle({ status: "cancelled" } as never)).toBe("open");
  });
});

describe("nodeAdapter — trust derived live, not stale status (gap-doc §2)", () => {
  it("maps live/age to health, ignoring the stale status column", () => {
    const targets = toTargets(rawNodes);
    expect(targets.find((t) => t.id === "main-pc")!.health).toBe("online");
    // pi5: status column says "online" but live=false by heartbeat age → offline
    expect(targets.find((t) => t.id === "pi5")!.health).toBe("offline");
    // laptop: never heard from (age null) → unknown
    expect(targets.find((t) => t.id === "laptop")!.health).toBe("unknown");
  });

  it("parses the JSON-encoded backends string", () => {
    const targets = toTargets(rawNodes);
    expect(targets.find((t) => t.id === "main-pc")!.backends).toEqual(["claude", "codex"]);
  });

  it("deriveHealth boundary cases", () => {
    expect(deriveHealth({ live: true } as never)).toBe("online");
    expect(deriveHealth({ live: false, heartbeat_age_sec: null } as never)).toBe("unknown");
    expect(deriveHealth({ live: false, heartbeat_age_sec: 200 } as never)).toBe("offline");
  });
});

describe("taskAdapter — mesh status → TaskState (gap-doc §4)", () => {
  it("maps mesh statuses to the canonical lifecycle subset", () => {
    const tasks = toTasks(rawTasks);
    const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
    expect(byId["task_a1"].state).toBe("dispatching"); // claimed
    expect(byId["task_b2"].state).toBe("queued"); // pending
    expect(byId["task_c3"].state).toBe("failed");
    expect(byId["task_d4"].state).toBe("succeeded"); // completed
  });

  it("carries no progress field (⛔ task.progress dropped)", () => {
    const t = toTasks(rawTasks)[0] as unknown as Record<string, unknown>;
    expect("progressPct" in t).toBe(false);
    expect("progress" in t).toBe(false);
  });
});

describe("taskAdapter — sectioned (Move G′): trust backend ui_state", () => {
  it("maps each section and overrides state with the backend ui_state", () => {
    const res = {
      sections: {
        attention: [
          // mesh status 'claimed' would map to 'dispatching', but the backend
          // overlaid the session → ui_state waiting_for_input. We must TRUST it.
          {
            ...rawTasks[0],
            id: "t_attn",
            status: "claimed",
            ui_state: "waiting_for_input",
            section: "attention",
          },
        ],
        running: [
          { ...rawTasks[0], id: "t_run", status: "processing", ui_state: "running", section: "running" },
        ],
        queued: [],
        recent: [
          { ...rawTasks[0], id: "t_done", status: "completed", ui_state: "succeeded", section: "recent" },
        ],
      },
    };
    const out = toTaskSections(res as never);
    expect(out.attention[0].id).toBe("t_attn");
    expect(out.attention[0].state).toBe("waiting_for_input"); // backend wins over mesh map
    expect(out.running[0].state).toBe("running");
    expect(out.queued).toHaveLength(0);
    expect(out.recent[0].state).toBe("succeeded");
  });

  it("tolerates missing section arrays", () => {
    const out = toTaskSections({ sections: {} } as never);
    expect(out.attention).toEqual([]);
    expect(out.recent).toEqual([]);
  });
});

describe("approvalAdapter — RawApproval → ApprovalRequest (Move H)", () => {
  const raw = {
    id: "appr_1", session_id: "s1", task_id: null, action: "deploy to prod",
    risk: "high", reversible: 0, status: "pending", requested_by: "agent",
    resolved_by: null, payload: null, created_at: "2026-06-24T10:00:00Z",
    resolved_at: null, expires_at: null,
  };

  it("maps the int reversible to a bool and narrows risk", () => {
    const a = toApproval(raw as never);
    expect(a.reversible).toBe(false);
    expect(a.risk).toBe("high");
    expect(a.sessionId).toBe("s1");
    expect(a.action).toBe("deploy to prod");
  });

  it("defaults an unknown risk to medium", () => {
    expect(toApproval({ ...raw, risk: "weird" } as never).risk).toBe("medium");
  });

  it("treats reversible=1 as true", () => {
    expect(toApproval({ ...raw, reversible: 1 } as never).reversible).toBe(true);
  });
});

describe("eventAdapter — snake→dotted translation (gap-doc §6)", () => {
  it("swallows heartbeat", () => {
    expect(adaptEvent({ event: "heartbeat", timestamp: "t", node_id: "main-pc" })).toBeNull();
  });

  it("collapses task_received into a task.state_changed", () => {
    const ev = adaptEvent({ event: "task_received", timestamp: "t", task_id: "task_a1" });
    expect(ev).toEqual({ type: "task.state_changed", taskId: "task_a1", state: "running" });
  });

  it("keeps task lifecycle out of system.notice", () => {
    const ev = adaptEvent({ event: "mesh_dispatch", timestamp: "t", task_id: "task_a1", node_id: "main-pc" });
    expect(ev).toEqual({ type: "task.state_changed", taskId: "task_a1", state: "dispatching" });
  });

  it("treats mesh health transitions as visible operator states", () => {
    const degraded = adaptEvent({ event: "mesh_degraded", timestamp: "t" });
    const restored = adaptEvent({ event: "mesh_restored", timestamp: "t" });
    expect(degraded).toMatchObject({ type: "system.notice", notice: { severity: "warning" } });
    expect(restored).toMatchObject({ type: "system.notice", notice: { severity: "success" } });
  });

  it("drops the heartbeat from a batch but keeps the rest", () => {
    const out = adaptEvents(rawEvents);
    expect(out.some((e) => e.type === "system.notice")).toBe(true);
    expect(out.some((e) => e.type === "task.state_changed")).toBe(true);
    // 8 raw events, 1 heartbeat and 1 redundant summarized event swallowed → 6 out
    expect(out).toHaveLength(6);
  });

  it("emits NO tool.* or task.progress types", () => {
    const out = adaptEvents(rawEvents);
    for (const e of out) {
      expect(e.type.startsWith("tool.")).toBe(false);
      expect(e.type).not.toBe("task.progress");
    }
  });
});
